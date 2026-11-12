"""MCP server 2: `shell` (SPEC section 3)."""

import json
import os
import signal
import subprocess
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


def run_process(argv, cwd, combined):
    """Run argv; return (exit_code, stdout bytes, stderr bytes, duration_ms)."""
    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combined else subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout kills the whole tree
        )
    except OSError as exc:
        raise ToolError(f"cannot run {argv[0]}: {exc.strerror or exc}")
    try:
        out, err = proc.communicate(timeout=TIMEOUT_S)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a grandchild left the group and holds the pipe
            out, err = b"", b""
        code = -1
    duration_ms = int(round((time.monotonic() - start) * 1000))
    return code, out, err, duration_ms


class Shell:
    def __init__(self, workspace, task_path):
        self.root = os.path.realpath(workspace)
        if not os.path.isdir(self.root):
            raise SystemExit(f"tinycoder: workspace is not a directory: {workspace}")
        self.task_path = task_path
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
        code, out, _, ms = await anyio.to_thread.run_sync(run_process, cmd, self.root, True)
        return {"exit_code": code, "passed": code == 0, "output": _tail(out), "duration_ms": ms}

    async def run_command(self, args):
        import anyio.to_thread

        argv = args.get("argv")
        why = argv_problem(argv)
        if why:
            raise ToolError(why)
        code, out, err, ms = await anyio.to_thread.run_sync(run_process, argv, self.root, False)
        return {"exit_code": code, "stdout": _tail(out), "stderr": _tail(err), "duration_ms": ms}


def main(workspace, task_path):
    shell = Shell(workspace, task_path)
    serve("shell", TOOLS, {"run_tests": shell.run_tests, "run_command": shell.run_command})
