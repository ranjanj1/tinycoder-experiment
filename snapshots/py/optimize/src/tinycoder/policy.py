"""Rules shared by the shell server and the agent's pre-call hook.

Kept free of heavy imports so every process can load it cheaply.
"""

import re

COMMAND_ALLOWLIST = frozenset({"ls", "cat", "head", "tail", "wc", "diff", "python3"})
GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show"})
MAX_WRITE_BYTES = 100_000

_SEP = re.compile(r"[/\\]")


def argv_problem(argv):
    """Return why `argv` may not run under section 3, or None when it may."""
    if not isinstance(argv, list) or not argv:
        return "argv must be a non-empty array of strings"
    if not all(isinstance(a, str) for a in argv):
        return "argv must be a non-empty array of strings"
    prog = argv[0]
    if prog == "git":
        if len(argv) < 2 or argv[1] not in GIT_SUBCOMMANDS:
            return "git is only allowed with status, diff, log or show"
        return None
    if prog not in COMMAND_ALLOWLIST:
        return f"command not allowed: {prog}"
    return None


def path_problem(path):
    """Agent-side path check: absolute paths and `..` parts are refused."""
    if not isinstance(path, str):
        return None  # type errors are the server's to report
    if path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
        return f"absolute path not allowed: {path}"
    if ".." in _SEP.split(path):
        return f"path with '..' not allowed: {path}"
    return None


def hook_problem(name, args, known_tools):
    """Pre-call hook. Return the block reason, or None to let the call through."""
    if name not in known_tools:
        return f"unknown tool: {name}"
    if not isinstance(args, dict):
        return "arguments must be an object"
    if "path" in args:
        why = path_problem(args["path"])
        if why:
            return why
    if name == "write_file":
        content = args.get("content")
        if isinstance(content, str) and len(content.encode("utf-8", "surrogatepass")) > MAX_WRITE_BYTES:
            return f"write_file content is over {MAX_WRITE_BYTES} bytes"
    if name == "run_command":
        why = argv_problem(args.get("argv"))
        if why:
            return why
    return None
