# tinycoder, lane PY: notes

Python 3.12.13 with the standard library, `mcp` 2.3.0 (official MCP SDK) and `anthropic` 2.8.0
(official SDK), all from the offline kit. Visible checks: **11/11 pass**
(`python3 tests/check.py bin/tinycoder`), both in this workspace and in a clean copy built with
BUILD.md's commands.

## Layout

| File | Role |
|---|---|
| `bin/tinycoder` | `sh` launcher. Picks the interpreter (see BUILD.md) and runs `src/tinycoder/main.py` with `-I` |
| `src/tinycoder/main.py` | argparse CLI: `serve files`, `serve shell`, `run` |
| `src/tinycoder/mcpserve.py` | Shared stdio server runner on the SDK's **low-level `Server`** + `stdio_server`; argument validators |
| `src/tinycoder/files_server.py` | MCP server 1 (`list_files`, `read_file`, `write_file`, `edit_file`, `search`) |
| `src/tinycoder/shell_server.py` | MCP server 2 (`run_tests`, `run_command`) |
| `src/tinycoder/policy.py` | Command allowlist and the pre-call hook rules (used by the shell server and the agent) |
| `src/tinycoder/agent.py` | Agent loop: SDK `stdio_client` + `ClientSession` for both servers, `AsyncAnthropic` for the model |

## Design

- **MCP is all SDK.** Servers use `mcp.server.lowlevel.Server(on_list_tools=..., on_call_tool=...)`
  over `mcp.server.stdio.stdio_server`. The agent uses `mcp.stdio_client` + `mcp.ClientSession`
  (`initialize`, `list_tools`, `call_tool`). There is no hand-written framing or JSON-RPC.
  I chose the low-level server over `MCPServer` so each tool's `input_schema` is exactly the JSON
  Schema I wrote, and so every failure (bad arguments included) comes back as an `isError` result
  whose text starts with `error: `, never as a protocol error. That server does not validate
  arguments, so each handler validates its own.
- **Same program for the children.** `run` spawns `sys.executable -I src/tinycoder/main.py serve ...`
  for each server, passing its own environment through. The SDK's default child env drops `LANG`,
  which the shell server must forward.
- **Concurrency.** Each server connection lives in its own anyio task (spawn → initialize →
  list_tools → wait for a stop event). The two servers boot in parallel, and in parallel the
  parent imports `anthropic` in a worker thread. At the end both shut down in parallel.
- **Model call.** `client.messages.with_raw_response.create(...)` with exactly `model`,
  `max_tokens`, `system`, `messages`, `tools`, `output_config` (checked by capturing the request
  body: no other keys, `anthropic-version: 2026-10-21`). I parse the **raw JSON**, so the
  assistant `content` is appended byte-for-byte, including thinking, redacted_thinking and unknown
  blocks or fields (checked).
- **Retries are my own** (SDK `max_retries=0`): HTTP 429/500/502/503/529 and `APIConnectionError`
  (which includes timeouts) are retried after 1, 2 and 4 s, so 4 attempts at most. Anything else
  fails the run at once. I did not use the SDK's built-in retry because it also retries
  408/409/504 and honours `x-should-retry`, which SPEC does not allow.
- **Hooks** run in the agent before any server sees the call, in this order: unknown tool name,
  arguments not an object, `path` absolute (`/`, `\`, `C:`) or containing a `..` part (split on `/`
  and `\`), `write_file` content over 100,000 UTF-8 bytes, `run_command` argv not allowed.
- **Speed.** Byte-compiled source (BUILD.md); lazy imports, so the servers never load `anthropic`;
  the no-op OpenTelemetry server middleware removed; `os._exit` after all output is flushed, which
  skips interpreter teardown (server exit after stdin EOF went from ~55 ms to ~2 ms, and `run`
  shutdown from ~180 ms to ~12 ms).

## Choices where SPEC.md was silent

- **Path display.** Output paths are relative to the workspace, use `/`, and are the lexically
  normalized path you passed, not the symlink-resolved one.
- **`.git` rule.** I reject any path component equal to `.git` (case-insensitive, for macOS),
  checked both as given and after resolving symlinks. So a symlink that points into `.git` is
  rejected too.
- **`list_files`.** The given directory itself is not listed. Directories report `size: 0`.
  Symlinks are listed under their target's type when the target is inside the workspace (and not
  in `.git`), otherwise skipped. Symlinked directories are not descended into, which avoids loops.
  A path that is a file is an error (`not a directory`). `truncated` appears only when true. The
  sort is a plain string sort of the path.
- **`read_file`.** Lines are split on `\n` only, and a trailing newline does not count as an
  extra line (`"a\nb\n"` has 2 lines). Line endings in `content` are kept exactly. With no range,
  `content` is the whole file verbatim. If `start_line` is past the end, `content` is `""` (not an
  error). `end_line` past the end is clamped. `end_line < start_line` is an error.
- **`write_file` / `edit_file`.** Content is written as exact UTF-8 bytes, with no newline
  translation. An empty `old_text` is an error. `edit_file` on a non-UTF-8 file is an error.
- **`search`.** Uses Python `re` syntax and `re.search` on each line. A trailing `\r` is stripped
  from the line `text`. A path may be a file or a directory. Files go in path order, and a match
  through a symlink is reported under the symlink's path.
- **Shell.** The child env is exactly the `PATH`, `HOME` and `LANG` that are set (any missing one is
  left out). Each child runs in its own session/process group, so a timeout `SIGKILL`s the whole
  tree, and stdin is `/dev/null`. Decoding is UTF-8 with replacement. `run_tests` merges stderr
  into the same pipe, so `output` is truly interleaved. A child killed by a signal reports the
  negative signal number, and the 30 s timeout reports `-1`. `run_command` checks the allowlist
  again on the server side. `argv[0]` must be the bare name (`/bin/ls` is refused). `test_command`
  is read from `task.json` once, on first use.
- **Step limit.** The tool calls requested in step 20 are still run, and their results are
  recorded, before the run ends with `step_limit` (20 model calls at most). Reaching end_turn at
  step 20 is `done`.
- **`reason`.** It is `null` for `done` and `step_limit`. For a bad stop reason it is the raw
  `stop_reason` (e.g. `"max_tokens"`). For API failures it is `"api error: HTTP <code>:
  <message>"` or `"api connection error: ..."`, and for other failures `"<ExceptionType>: <msg>"`.
  `final_text` is `""` unless the status is `done`.
- **Counters.** A failed model call (after its retries) still counts as a step. There is one `llm`
  line per step, written only when the call succeeds, and its `duration_ms` includes retry waits.
  `input_tokens`/`output_tokens` are sums of the API `usage` fields; cache tokens are only in the
  `llm` lines (null → 0). `wall_ms` runs from interpreter start (just after `main.py` loads) to
  the final line, including server startup and shutdown. `llm_ms` and `tool_ms` are sums over calls.
  All `*_ms` are floats rounded to 3 decimals; the shell server's `duration_ms` is an int, as SPEC says.
- **Log byte counts.** `args_bytes` is the UTF-8 length of the compact JSON of the arguments
  (`separators=(",", ":")`, non-ASCII kept). `result_bytes` is the UTF-8 length of the result text
  sent to the model.
- **`tool_result`.** `content` is the server's text, as a string. `is_error: true` is present only
  on errors. A `tool_use` stop with no `tool_use` blocks ends the run as `error`.
- **API key.** If `ANTHROPIC_API_KEY` is unset, the literal string `unset` is sent, so a fake API
  still works and the real API returns 401 (status `error`). SDK 2.8 sends the key as
  `Authorization: Bearer`, its own default.

## Known limitations

- **Startup cost.** Most of it is SDK import time. A server takes ~320 ms from spawn to its
  `initialize` reply: importing any `mcp` submodule runs `mcp/__init__.py` (~290 ms of pydantic
  models, client, auth and HTTP code). `run` reaches its first model request after ~0.75–1.0 s,
  mostly `import mcp` plus `import anthropic` (~550 ms), which the GIL forces to run one after the
  other. I measured that skipping the package `__init__` files with `sys.modules` stubs saves only
  ~60 ms and depends on SDK internals, so I did not ship it.
- **Memory.** Each Python process loads the SDK's pydantic models (three processes during `run`).
- **Env vars on macOS.** The OS adds `__CF_USER_TEXT_ENCODING` to every child's environment. We
  pass exactly PATH/HOME/LANG, and this one is outside our control.
- **Interpreter lookup.** The launcher hard-codes the kit venv path as a fallback, because the kit's
  `python3` on PATH cannot import `mcp` (see BUILD.md). Elsewhere, set `TINYCODER_PYTHON` or create
  `./.venv`.
- **Not tested here.** The real API and `tests/bench_dev.py`. There is no network, and the sandbox
  refuses even loopback TCP from my own processes.
- **Process tree.** A process that leaves its process group (e.g. calls `setsid`) can outlive a
  timeout. Its output is then dropped after a 5 s grace period.

## How I verified (commands)

Following RULES.md rule 4, I only ran the files in `tests/` and never opened their source. The
practice task folders were used only as inputs.

```sh
# environment / SDK discovery
python3 --version; ls /Users/Shared/tc-kit/pyenv/lib/python3.12/site-packages
/Users/Shared/tc-kit/pyenv/bin/python3 -c "import mcp, anthropic"     # works (bin-py/python3 does not)
# read SDK source: mcp/server/lowlevel/server.py, mcp/server/stdio.py, mcp/client/stdio.py,
#   mcp/client/session.py, anthropic/_client.py, anthropic/resources/messages/messages.py
/Users/Shared/tc-kit/pyenv/bin/python3 -X importtime -c "import mcp.server.lowlevel"   # startup profiling

# visible checks
chmod +x bin/tinycoder && python3 tests/check.py bin/tinycoder          # 11/11
# clean-copy rehearsal of the harness
cp -R . /tmp/tc-clean && cd /tmp/tc-clean && chmod +x bin/tinycoder \
  && /Users/Shared/tc-kit/pyenv/bin/python3 -m compileall -q src && python3 tests/check.py bin/tinycoder
```

Throwaway scripts in `/tmp/tc-scratch` (outside the project, since deleted):

- **`mock_run.py`** ran the real `Agent` and the real `anthropic` SDK over an in-process
  `httpx2.MockTransport`, used because loopback TCP is blocked here. It covered: request body
  keys and headers, tool order, thinking-block round-trip, hook blocks (`..`, `git push`, unknown
  tool), parallel tool results in one user message, a 529 then success, a 503 three times then
  success, 529/connection error on all 4 attempts, a 400 not retried, `max_tokens`, the step limit
  (20 requests, exit 2), a missing TASK.md, and a missing workspace (server dies, run reports
  `MCPError: Connection closed`, no hang).
- **`srv_test.py`** called both servers through the SDK client. It covered line ranges, non-UTF-8,
  absolute/`..`/symlink escapes, a symlink into `.git`, the 500/200 truncation, the regex `^`
  anchor, a bad regex, duplicate `old_text`, nested writes, env filtering, the
  stdout/stderr split, 8,000-character tails, refused commands, and the 30 s timeout killing a
  grandchild (`exit_code -1`).
- **`init_time.py`, `exit_time.py`, `phase.py`** measured startup, exit and per-phase timings.
