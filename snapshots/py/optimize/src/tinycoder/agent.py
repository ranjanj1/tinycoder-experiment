"""`tinycoder run`: the agent loop (SPEC section 4)."""

import json
import os
import sys
import time

from . import fastimport
from .policy import hook_problem

MODEL = "claude-sonnet-5-5"
API_VERSION = "2026-10-21"
MAX_TOKENS = 16000
MAX_STEPS = 20
RETRY_STATUS = frozenset({429, 500, 502, 503, 529})
RETRY_DELAYS = (1.0, 2.0, 4.0)
TOOL_READ_TIMEOUT_S = 120.0

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(HERE, "main.py")
SYSTEM_PROMPT_PATH = os.path.join(HERE, "..", "..", "shared", "system.txt")


def _ms(seconds):
    return round(seconds * 1000.0, 3)


def _read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def _compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class LLMError(Exception):
    pass


class RunLog:
    def __init__(self, runs_dir, run_id):
        os.makedirs(runs_dir, exist_ok=True)
        self.path = os.path.join(runs_dir, f"{run_id}.jsonl")
        self._f = open(self.path, "a", encoding="utf-8")

    def write(self, record):
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self):
        self._f.close()


class Agent:
    def __init__(self, task_dir, workspace, runs_dir, t0):
        self.task_dir = task_dir
        self.workspace = os.path.abspath(workspace)
        self.t0 = t0
        run_id = os.environ.get("TINYCODER_RUN_ID") or str(int(time.time() * 1000))
        self.log = RunLog(runs_dir, run_id)
        self.steps = 0
        self.tool_calls = 0
        self.blocked_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_s = 0.0
        self.tool_s = 0.0
        self.tools = []  # API tool definitions, files first then shell
        self.route = {}  # tool name -> ClientSession

    # -- MCP servers --------------------------------------------------------------------

    @staticmethod
    async def _connection(args, stop, task_status):
        """Own one server for the whole run: spawn, initialize, list tools, wait for `stop`.

        Each connection lives in its own task so both servers start, and later shut
        down, concurrently.
        """
        from mcp import types
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=sys.executable, args=args, env=dict(os.environ))
        async with stdio_client(params, errlog=sys.stderr) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools, cursor = [], None
                while True:
                    page = types.PaginatedRequestParams(cursor=cursor) if cursor else None
                    result = await session.list_tools(params=page)
                    tools.extend(result.tools)
                    cursor = result.next_cursor
                    if not cursor:
                        break
                task_status.started((session, tools))
                await stop.wait()

    async def _start_servers(self, tg, stop):
        import anyio
        import anyio.to_thread

        base = ["-I", MAIN_SCRIPT, "serve"]  # same program, same interpreter
        task_json = os.path.join(self.task_dir, "task.json")
        specs = [
            base + ["files", "--workspace", self.workspace],
            base + ["shell", "--workspace", self.workspace, "--task", task_json],
        ]
        ready = [None, None]

        async def start(i):
            ready[i] = await tg.start(self._connection, specs[i], stop)

        def warm_imports():
            # loaded while the servers boot; `messages` is the client's lazy resource, which
            # the first model call would otherwise import (~30 ms)
            import anthropic._client  # noqa: F401
            import anthropic.resources.messages  # noqa: F401

        async with anyio.create_task_group() as boot:
            boot.start_soon(start, 0)
            boot.start_soon(start, 1)
            boot.start_soon(anyio.to_thread.run_sync, warm_imports)

        for session, tools in ready:
            for tool in tools:
                spec = {"name": tool.name}
                if tool.description is not None:
                    spec["description"] = tool.description
                spec["input_schema"] = tool.input_schema
                self.tools.append(spec)
                self.route.setdefault(tool.name, session)

    # -- model --------------------------------------------------------------------------

    def _make_client(self):
        from anthropic._client import AsyncAnthropic

        return AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY") or "unset",
            base_url=os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
            max_retries=0,  # retries are done below, exactly as SPEC 4.1 says
            default_headers={"anthropic-version": API_VERSION},
            open_telemetry=False,
        )

    async def _call_model(self, client, system, messages):
        import anyio
        from anthropic._exceptions import APIConnectionError, APIStatusError

        attempt = 0
        while True:
            try:
                raw = await client.messages.with_raw_response.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=messages,
                    tools=self.tools,
                    output_config={"effort": "medium"},
                )
                try:
                    return json.loads(raw.http_response.text)
                except ValueError as exc:
                    raise LLMError(f"invalid JSON from the Messages API: {exc}")
            except APIStatusError as exc:
                if exc.status_code in RETRY_STATUS and attempt < len(RETRY_DELAYS):
                    print(f"tinycoder: HTTP {exc.status_code}, retrying", file=sys.stderr)
                    await anyio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise LLMError(f"api error: HTTP {exc.status_code}: {_error_message(exc)}")
            except APIConnectionError as exc:
                if attempt < len(RETRY_DELAYS):
                    print(f"tinycoder: connection error ({exc}), retrying", file=sys.stderr)
                    await anyio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise LLMError(f"api connection error: {exc}")

    # -- tools --------------------------------------------------------------------------

    async def _run_tool(self, step, block):
        name = block.get("name")
        args = block.get("input")
        start = time.perf_counter()
        why = hook_problem(name, args, self.route)
        blocked = why is not None
        if blocked:
            text, is_error = f"blocked by hook: {why}", True
        else:
            try:
                result = await self.route[name].call_tool(name, args, read_timeout_seconds=TOOL_READ_TIMEOUT_S)
                text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
                is_error = bool(result.is_error)
            except Exception as exc:
                text, is_error = f"error: {type(exc).__name__}: {exc}", True
        elapsed = time.perf_counter() - start
        self.tool_s += elapsed
        self.tool_calls += 1
        if blocked:
            self.blocked_calls += 1
        self.log.write({
            "type": "tool",
            "step": step,
            "tool": name if isinstance(name, str) else str(name),
            "blocked": blocked,
            "ok": not is_error,
            "duration_ms": _ms(elapsed),
            "args_bytes": len(_compact(args).encode("utf-8", "surrogatepass")),
            "result_bytes": len(text.encode("utf-8", "surrogatepass")),
        })
        out = {"type": "tool_result", "tool_use_id": block.get("id"), "content": text}
        if is_error:
            out["is_error"] = True
        return out

    # -- loop ---------------------------------------------------------------------------

    async def _loop(self):
        import anyio

        system = _read_text(SYSTEM_PROMPT_PATH)
        messages = [{"role": "user", "content": _read_text(os.path.join(self.task_dir, "TASK.md"))}]
        stop = anyio.Event()
        async with anyio.create_task_group() as tg:
            client = None
            try:
                await self._start_servers(tg, stop)
                client = self._make_client()
                fastimport.gc_resume()  # start-up is over: collector back on
                return await self._steps(client, system, messages)
            finally:
                stop.set()  # both connection tasks now shut their servers down in parallel
                if client is not None:
                    await client.close()

    async def _steps(self, client, system, messages):
        while self.steps < MAX_STEPS:
            self.steps += 1
            step = self.steps
            start = time.perf_counter()
            try:
                data = await self._call_model(client, system, messages)
            except LLMError as exc:  # returned, not raised, so no task group wraps it
                return "error", str(exc), ""
            finally:
                self.llm_s += time.perf_counter() - start
            elapsed = time.perf_counter() - start
            usage = data.get("usage") or {}
            stop = data.get("stop_reason")
            self.input_tokens += usage.get("input_tokens") or 0
            self.output_tokens += usage.get("output_tokens") or 0
            self.log.write({
                "type": "llm",
                "step": step,
                "duration_ms": _ms(elapsed),
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
                "stop_reason": stop if isinstance(stop, str) else str(stop),
            })
            content = data.get("content") or []
            messages.append({"role": "assistant", "content": content})
            if stop == "tool_use":
                calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                if not calls:
                    return "error", "stop_reason tool_use without tool_use blocks", ""
                results = [await self._run_tool(step, b) for b in calls]
                messages.append({"role": "user", "content": results})
                continue
            if stop == "end_turn":
                text = "\n".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
                return "done", None, text
            return "error", stop if isinstance(stop, str) else str(stop), ""
        return "step_limit", None, ""

    async def run(self):
        try:
            status, reason, final_text = await self._loop()
        except Exception as exc:
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                exc = exc.exceptions[0]  # report the real cause, not the task-group wrapper
            reason = str(exc) if isinstance(exc, LLMError) else f"{type(exc).__name__}: {exc}"
            status, final_text = "error", ""
        final = {
            "status": status,
            "reason": reason,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "blocked_calls": self.blocked_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "wall_ms": _ms(time.perf_counter() - self.t0),
            "llm_ms": _ms(self.llm_s),
            "tool_ms": _ms(self.tool_s),
            "final_text": final_text,
        }
        self.log.write({"type": "final", **final})
        self.log.close()
        sys.stdout.write(json.dumps(final, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return {"done": 0, "step_limit": 2}.get(status, 1)


def _error_message(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(exc, "message", exc))


def main(task_dir, workspace, runs_dir, t0):
    # Skip SDK re-export __init__ files we do not need (see fastimport.py); the Anthropic
    # client and errors are imported from their defining modules for the same reason. The
    # anthropic.types tree holds only pure re-export packages of type models (no optional
    # dependencies, so laziness cannot hide an import error).
    fastimport.shim(
        "mcp", "mcp.client", "anthropic", "anthropic.lib", "anthropic.resources",
        *fastimport.subpackages("anthropic", "types"),
    )

    import anyio

    try:
        agent = Agent(task_dir, workspace, runs_dir, t0)
    except Exception as exc:  # e.g. the runs directory cannot be created
        final = {
            "status": "error", "reason": f"{type(exc).__name__}: {exc}", "steps": 0, "tool_calls": 0,
            "blocked_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "wall_ms": _ms(time.perf_counter() - t0), "llm_ms": 0.0, "tool_ms": 0.0, "final_text": "",
        }
        sys.stdout.write(json.dumps(final) + "\n")
        return 1
    return anyio.run(agent.run)
