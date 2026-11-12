# tinycoder: build spec (version 1, draft until frozen)

Build **tinycoder**: a small coding agent. It is one program with two MCP servers and an agent loop that uses them.
Build it in the language you are assigned (see RULES.md). Follow this file exactly. Where it is silent, choose the
simplest thing that works and write the choice down in NOTES.md.

## 1. Command line

The program is invoked as `bin/tinycoder` (a script or binary you provide). It has three commands:

| Command | What it does |
|---|---|
| `tinycoder serve files --workspace <dir>` | Run MCP server 1 on stdio (section 2) |
| `tinycoder serve shell --workspace <dir> --task <task.json>` | Run MCP server 2 on stdio (section 3) |
| `tinycoder run --task <task-dir> --workspace <dir> --runs <dir>` | Run the agent on one task (section 4) |

- Both servers are built with the server API of your lane's official MCP SDK, and `run` starts both servers itself,
  as child processes of the same program, and talks to them over stdio with the client API of the same SDK.
  Do not write your own MCP or JSON-RPC protocol code (message framing, `initialize`, `tools/list`, `tools/call`):
  the SDK does that. Lanes and SDKs: RULES.md.
- `run` prints exactly one JSON object on stdout when it finishes (section 4.5). Logs go to stderr and `--runs`.
- Exit code: 0 when status is `done`, 2 for `step_limit`, 1 for `error`.

## 2. MCP server `files`

Every path is relative to the workspace. A path is rejected if it resolves outside the workspace after following
symlinks, is absolute, or goes inside `.git/`. A rejected or failing call returns an MCP tool error (`isError: true`)
whose single text block starts with `error: `.

A successful call returns one text content block holding a JSON object, exactly the fields below.

| Tool | Input (JSON Schema, all fields required unless marked) | Output |
|---|---|---|
| `list_files` | `path`: string (optional, default `"."`) | `{"entries": [{"path": str, "type": "file"\|"dir", "size": int}]}` recursive, sorted by path, skips `.git`, at most 500 entries, then `"truncated": true` |
| `read_file` | `path`: string; `start_line`: int ≥ 1 (optional); `end_line`: int ≥ start (optional) | `{"content": str, "total_lines": int}`; lines are 1-based and inclusive |
| `write_file` | `path`: string; `content`: string | `{"bytes_written": int}`; creates parent folders |
| `edit_file` | `path`: string; `old_text`: string; `new_text`: string | `{"replacements": 1}`; error if `old_text` occurs 0 times or more than once |
| `search` | `pattern`: string (a regular expression); `path`: string (optional, default `"."`) | `{"matches": [{"path": str, "line": int, "text": str}]}` in path then line order, at most 200, then `"truncated": true` |

Text is UTF-8. A file that is not valid UTF-8 is an error for `read_file` and is skipped by `search`.

## 3. MCP server `shell`

`--task` points to the task's `task.json`, which holds `{"test_command": [argv...]}`.

| Tool | Input | Output |
|---|---|---|
| `run_tests` | none (empty object) | `{"exit_code": int, "passed": bool, "output": str, "duration_ms": int}`; `passed` is `exit_code == 0`; `output` is stdout and stderr combined, last 8,000 characters |
| `run_command` | `argv`: array of strings, at least 1 | `{"exit_code": int, "stdout": str, "stderr": str, "duration_ms": int}`; each stream keeps its last 8,000 characters |

- Commands run with the workspace as the working directory, no shell (argv is passed straight to the OS), a 30 s
  timeout (the process is killed and `exit_code` is -1), and the environment variables `PATH`, `HOME`, `LANG` only.
- `run_command` allowlist: `ls`, `cat`, `head`, `tail`, `wc`, `diff`, `python3`, and `git` only with first argument
  `status`, `diff`, `log` or `show`. Anything else is an error.

## 4. Agent loop

### 4.1 Model call
- Claude Messages API, model `claude-sonnet-5-5`, header `anthropic-version: 2026-10-21`.
- Request fields: `model`, `max_tokens: 16000`, `system`, `messages`, `tools`, `output_config: {"effort": "medium"}`.
  Nothing else: no `temperature`, `top_p`, `top_k`, `thinking`, `tool_choice`, streaming or beta headers.
- Base URL from `ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`), key from `ANTHROPIC_API_KEY`.
- Use the official Anthropic SDK where one exists for your language. Where none exists, call the HTTP API directly.
- Retry on HTTP 429, 500, 502, 503, 529 and connection errors: up to 3 retries (4 attempts in all), waiting about
  1, 2 and 4 s. The SDK's own retry is fine if set to 3 retries. A retry is not a new step. Any other error, or the
  last attempt failing, ends the run with status `error`.

### 4.2 Prompt and tools
- `system` is the exact contents of `shared/system.txt`. The first user message is the exact contents of
  `<task-dir>/TASK.md`.
- `tools` are the tools listed by both servers (`tools/list`), `files` first then `shell`, each in the order its
  server lists them, with `name`, `description` and `input_schema` taken unchanged from the server.

### 4.3 Loop
1. Send the request. That is one **step**.
2. Append the full assistant `content` to `messages` unchanged, every block including thinking blocks.
3. If `stop_reason` is `tool_use`: run every `tool_use` block (section 4.4) in the order given. Return all results
   in **one** user message, one `tool_result` per call with its `tool_use_id`, the server's text, and
   `is_error: true` for errors. Go to 1.
4. If `stop_reason` is `end_turn`: finish with status `done`. The final text is the text of that message's text blocks, joined with one newline (`\n`).
5. If `stop_reason` is `max_tokens`, `refusal` or anything else: finish with status `error` and that reason.
6. After step 20, if the run hasn't finished, it ends with status `step_limit`.

### 4.4 Hooks
- **Before each tool call**, the agent checks the call itself, before it reaches a server. It blocks the call when:
  a `path` argument is absolute or contains a `..` part; `write_file` content is over 100,000 bytes;
  `run_command` argv is not allowed by section 3; the tool name is not one the servers listed. A blocked call is
  not sent. Its `tool_result` is `is_error: true` with text `blocked by hook: <reason>`, and it counts in
  `blocked_calls` and `tool_calls`.
- **After each tool call** (blocked or not), append one JSON line to `<runs>/<run-id>.jsonl`:
  `{"type": "tool", "step": int, "tool": str, "blocked": bool, "ok": bool, "duration_ms": number,
  "args_bytes": int, "result_bytes": int}`.
- After each model call, append `{"type": "llm", "step": int, "duration_ms": number, "input_tokens": int,
  "output_tokens": int, "cache_read_input_tokens": int, "stop_reason": str}`.
- `run-id` is given by the `TINYCODER_RUN_ID` environment variable, or is the start time in milliseconds.

### 4.5 Final output (stdout, one line of JSON)
`{"status": "done"|"step_limit"|"error", "reason": str|null, "steps": int, "tool_calls": int, "blocked_calls": int,
"input_tokens": int, "output_tokens": int, "wall_ms": number, "llm_ms": number, "tool_ms": number,
"final_text": str}`. The same object is the last line of the runs file, with `"type": "final"` added.

## 5. Tests you get
`tests/` holds the visible checks and a fake Messages API server that replays scripted responses, so you can test
the whole loop with no network and no key. Run `python3 tests/check.py bin/tinycoder`. Passing them is required but
does not prove the spec is met: other checks you will not see are run later. The checks and the
task repos use `python3` from `PATH`.
