# tinycoder: one coding agent, three languages

Claude Code (Opus 5.5, high effort) builds the same small coding agent, **tinycoder**, in Python, TypeScript and Rust:
two MCP servers (`files`, `shell`) and an agent loop that calls Claude Sonnet 5.5. A harness Claude never sees checks
and times every version on one M1 Mac (8 GB). This is the experiment behind the video "Is AI Killing Python?".

## Results (all numbers from `results/summary.json`)

- **Builds:** all six versions pass 26/26 hidden checks and the SDK audit. Fix rounds: Python 0, TypeScript 1 (its
  optimize step left two build steps out of BUILD.md), Rust 0. Rust's first build used the full 30-minute cap.
- **Benchmark (2 sittings, 5 shuffled rounds each, no model):** MCP server start 239–306 ms (Python), 84–120 ms
  (TypeScript), 3–6 ms (Rust). Agent process memory 73–106 MB, 95–97 MB, 20 MB.
- **Agent runs (Sonnet 5.5, 3 tasks × 5 runs per language, $1.17 total):** 45/45 pass the hidden tests. Time outside
  the model per run: 0.55 s (Python), 0.20 s (TypeScript), 0.09 s (Rust), out of 6–13 s runs.

`python3 results/summarize.py` rebuilds every number from the raw files. `HISTORY.md` is the full story, failures included.

## What is here

| Path | Contents |
|---|---|
| `SPEC.md`, `RULES.md`, `INTERFACE.md`, `CLAUDE.md` | What Claude was asked to build, the lane rules and the bundle interface (all in each session's bundle) |
| `prompts/` | The exact prompts: initial, optimize, repair |
| `shared/system.txt` | The agent's system prompt (the same for all three) |
| `tests/` | Visible checks, fake Messages API and practice tasks (in the bundle) |
| `bench/` | Hidden checks, task repos with hidden tests, benchmark and agent-run harness (never in the bundle); `FROZEN.json` |
| `sandbox/`, `tools/` | macOS sandbox profile, session recorder, step runner, network filter, OpenRouter relay |
| `snapshots/<lane>/<step>/` | The code Claude wrote at the end of each step (`ts/repair` is the measured TypeScript optimize) |
| `results/` | `summary.json`, `sessions.json`, `raw/` benchmark sittings, `agent/` runs |

## Run it yourself

Needs macOS, Claude Code, Python 3.12, Node and Rust (versions in `KIT.md`).

1. **Offline kits:** build `/Users/Shared/tc-kit` as described in `KIT.md`, and a local `agentlab` user for the sessions.
2. **Check the protocol is unchanged:** `python3 bench/freeze.py --check`.
3. **Build sessions:** `python3 tools/run_steps.py` prints the plan; `--go` runs each step (one Claude Code session per
   language and step, sandboxed, internet limited to api.anthropic.com by `tools/net_filter.py`, 30-minute cap).
4. **Check each build:** `python3 bench/validate.py <build>/bin/tinycoder` (hidden checks) and
   `python3 bench/audit.py PY|TS|RS <workspace>` (SDK use).
5. **Benchmark (no model):** `bench/sitting.sh 1`, then `bench/sitting.sh 2` at a quiet time.
6. **Agent runs:** set `OPENROUTER_API_KEY` (environment or a `.env` in the repo root, git-ignored), then
   `bench/agent_all.sh`. Each run starts its own local relay (`tools/openrouter_relay.py`), pinned to Anthropic.
7. **Numbers:** `python3 results/summarize.py`.
