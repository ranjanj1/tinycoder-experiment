"""MCP server 2: `shell` (SPEC section 3)."""

import json
import os
import selectors
import shutil
import signal
import time

from .mcpserve import ToolError, serve
from .policy import argv_problem

TIMEOUT_S = 30
TAIL_CHARS = 8000
ENV_KEYS = ("PATH", "HOME", "LANG")

TOOLS = [
    {
        "name": "run_tests",
        "description": (
            "Run the task's test command in the workspace. Returns exit_code, passed (exit_code == 0), "
            "the combined stdout+stderr (last 8000 characters) and duration_ms. 30 s timeout."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_command",
        "description": (
            "Run one command in the workspace without a shell (argv is passed directly). Allowed: ls, "
            "cat, head, tail, wc, diff, python3, and git status|diff|log|show. Returns exit_code, "
            "stdout and stderr (last 8000 characters each) and duration_ms. 30 s timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Program and arguments, e.g. [\"git\", \"diff\"].",
                }
            },
            "required": ["argv"],
        },
    },
]


def _tail(data):
    text = data.decode("utf-8", "replace") if data else ""
    return text[-TAIL_CHARS:]


def _find_executable(name, path):
    """Resolve argv[0] the way exec does: names with a slash are used as given (relative
    to the cwd, which is the workspace), others are looked up on the child's PATH."""
    if "/" in name:
        return name
    return shutil.which(name, path=path)


class _Tail:
    """Collects a stream but keeps only its end: callers use the last 8,000 characters, and
    64 kB of bytes always decodes to more than that, so memory stays bounded."""

    KEEP = 1 << 16

    def __init__(self):
        self.chunks = []
        self.size = 0

    def add(self, chunk):
        self.chunks.append(chunk)
        self.size += len(chunk)
        if self.size > 4 * self.KEEP:
            data = b"".join(self.chunks)[-self.KEEP:]
            self.chunks, self.size = [data], len(data)

    def bytes(self):
        return b"".join(self.chunks)


def run_process(argv, combined):
    """Run argv in the current directory (the workspace).

    Return (exit_code, stdout bytes, stderr bytes, duration_ms); with `combined`, stderr is
    sent into the stdout pipe and the stderr result is empty. posix_spawn is used instead of
    subprocess (fork + exec of a ~50 MB server): ~0.7 ms less per command. The child gets its
    own session, so a timeout SIGKILLs the whole process group.
    """
    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    start = time.monotonic()
    exe = _find_executable(argv[0], env.get("PATH", os.defpath))
    if exe is None:
        raise ToolError(f"cannot run {argv[0]}: No such file or directory")
    out_r, out_w = os.pipe()  # non-inheritable; dup2 in the child makes the copies inheritable
    err_r, err_w = (None, out_w) if combined else os.pipe()
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
        (os.POSIX_SPAWN_DUP2, out_w, 1),
        (os.POSIX_SPAWN_DUP2, err_w, 2),
    ]
    try:
        pid = os.posix_spawn(exe, argv, env, file_actions=actions, setsid=True)
    except OSError as exc:
        os.close(out_r)
        if err_r is not None:
            os.close(err_r)
        raise ToolError(f"cannot run {argv[0]}: {exc.strerror or exc}")
    finally:
        os.close(out_w)
        if not combined:
            os.close(err_w)

    tails = {out_r: _Tail()}
    if err_r is not None:
        tails[err_r] = _Tail()
    timed_out = False
    deadline = start + TIMEOUT_S
    with selectors.DefaultSelector() as sel:
        for fd in tails:
            sel.register(fd, selectors.EVENT_READ)
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if timed_out:
                    break  # a process that left the group still holds a pipe: give up on it
                timed_out = True
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
                deadline = time.monotonic() + 5  # short grace period to drain the pipes
                continue
            for key, _ in sel.select(remaining):
                chunk = os.read(key.fd, 1 << 16)
                if chunk:
                    tails[key.fd].add(chunk)
                else:
                    sel.unregister(key.fd)
    for fd in tails:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    code = -1 if timed_out else os.waitstatus_to_exitcode(status)
    duration_ms = int(round((time.monotonic() - start) * 1000))
    out = tails[out_r].bytes()
    err = tails[err_r].bytes() if err_r is not None else b""
    return code, out, err, duration_ms


class Shell:
    def __init__(self, workspace, task_path):
        self.root = os.path.realpath(workspace)
        if not os.path.isdir(self.root):
            raise SystemExit(f"tinycoder: workspace is not a directory: {workspace}")
        self.task_path = os.path.abspath(task_path)
        # Every command runs in the workspace; posix_spawn has no cwd option, so the server
        # itself works there and children inherit it.
        os.chdir(self.root)
        self._test_command = None

    def test_command(self):
        if self._test_command is None:
            try:
                with open(self.task_path, encoding="utf-8") as f:
                    cmd = json.load(f).get("test_command")
            except (OSError, ValueError) as exc:
                raise ToolError(f"cannot read task file {self.task_path}: {exc}")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
                raise ToolError("task file has no valid test_command")
            self._test_command = cmd
        return self._test_command

    async def run_tests(self, args):
        import anyio.to_thread

        cmd = self.test_command()
        code, out, _, ms = await anyio.to_thread.run_sync(run_process, cmd, True)
        return {"exit_code": code, "passed": code == 0, "output": _tail(out), "duration_ms": ms}

    async def run_command(self, args):
        import anyio.to_thread

        argv = args.get("argv")
        why = argv_problem(argv)
        if why:
            raise ToolError(why)
        code, out, err, ms = await anyio.to_thread.run_sync(run_process, argv, False)
        return {"exit_code": code, "stdout": _tail(out), "stderr": _tail(err), "duration_ms": ms}


def main(workspace, task_path):
    shell = Shell(workspace, task_path)
    serve("shell", TOOLS, {"run_tests": shell.run_tests, "run_command": shell.run_command})
