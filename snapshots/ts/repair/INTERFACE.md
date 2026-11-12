# Interface between your build and the harness

## What you get (the bundle, copied into your workspace before the first step)
- `RULES.md`, `SPEC.md`, `INTERFACE.md`, `CLAUDE.md`
- `shared/system.txt`: the agent's system prompt (use it unchanged)
- `tests/check.py`: visible checks; `tests/fake_api.py`: a fake Messages API that replays scripted replies;
  `tests/tasks/`: two practice tasks; `tests/bench_dev.py`: the development benchmark (optimize step only)
- The offline package kit for your lane is already on PATH (pip / npm / cargo use it; there is no internet).

## What you deliver (in the workspace root)
- `bin/tinycoder`: the entry point. For compiled lanes, the release binary or a two-line script that runs it.
- `BUILD.md`: the exact commands that rebuild `bin/tinycoder` from source in a clean copy.
- `NOTES.md`: what you did, choices where SPEC.md was silent, known limitations.
- Source in `src/` (any layout inside it), the package manifest and lockfile at the root.

## How the harness uses it
- It copies your workspace, runs `BUILD.md`'s commands (timed separately, never counted as runtime), then runs
  `bin/tinycoder` exactly as SPEC.md section 1 says.
- Each task runs in a fresh copy of the task repo. `ANTHROPIC_BASE_URL` points at the fake API in checks and at
  the real API in agent runs. `TINYCODER_RUN_ID` is always set.
- It measures from outside: process start to the MCP `initialize` reply, peak memory of every process the program
  starts, the time each tool call takes through a plain MCP client, and the whole agent run.
