"""tinycoder command line (SPEC section 1). Run as a script: python3 -I src/tinycoder/main.py ..."""

import time

T0 = time.perf_counter()

import argparse  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse(argv):
    p = argparse.ArgumentParser(prog="tinycoder", description="A small coding agent built on MCP.")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run one of the MCP servers on stdio")
    which = serve.add_subparsers(dest="server", required=True)
    files = which.add_parser("files", help="MCP server 1: workspace files")
    files.add_argument("--workspace", required=True)
    shell = which.add_parser("shell", help="MCP server 2: tests and commands")
    shell.add_argument("--workspace", required=True)
    shell.add_argument("--task", required=True, help="path to the task's task.json")

    run = sub.add_parser("run", help="run the agent on one task")
    run.add_argument("--task", required=True, help="task directory (TASK.md, task.json)")
    run.add_argument("--workspace", required=True)
    run.add_argument("--runs", required=True, help="directory for <run-id>.jsonl logs")
    return p.parse_args(argv)


def main(argv):
    args = parse(argv)
    if args.command == "serve":
        if args.server == "files":
            from tinycoder.files_server import main as serve_files

            serve_files(args.workspace)
        else:
            from tinycoder.shell_server import main as serve_shell

            serve_shell(args.workspace, args.task)
        return 0
    from tinycoder.agent import main as run_agent

    return run_agent(args.task, args.workspace, args.runs, T0)


if __name__ == "__main__":
    code = main(sys.argv[1:])
    # All output is written and flushed and the servers are stopped; skip the slow
    # interpreter teardown (module GC of the SDKs costs ~50 ms per process).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
