# History (what happened, in order, including failures)

## 11 Nov 2026: pilot (throwaway, not a result)
- One Claude Code session as `agentlab` (Opus 5.5, high), lane PY prompt, 10-minute cap, id `pilot-py`.
- Recorder, sandbox and logs worked end to end: 613 s, 35 API calls, about $2.15 at API list prices (an estimate;
  subscription use is not billed this way). Network log: only api.anthropic.com allowed; Claude Code's own calls to
  downloads.claude.ai and github.com were denied. 35 permission prompts, all answered yes by the recorder.
- The build passed the visible checks 11/11 and the hidden checks 25/26 within 10 minutes.
- Two spec gaps found and fixed before freezing:
  1. It hand-wrote the MCP servers (raw JSON-RPC, for fast startup) instead of using the MCP SDK. The spec only
     required the SDK for the client. Now SPEC section 1 + RULES rule 6 require the official SDK for servers and
     client, and `bench/audit.py` checks the source (it flags the pilot).
  2. "Text blocks joined" did not say with what; the pilot used a newline, the checker expected none. Now SPEC says
     one newline; the checker and the reference follow.
- The pilot's workspace and session log were deleted after this note. Its snapshot is not in the published repo (a throwaway, never measured).

## 11 Nov 2026: real build sessions (Claude Code Opus 5.5, high effort, 30-minute cap per step)
Fresh sandboxed session per lane and step, order PY, TS, RS. Costs are estimates at API list prices (subscription
use is not billed this way). Every net.log shows only api.anthropic.com allowed.

| Lane | Step | Time | Ended | Est. cost | API calls | Output tokens | Hidden checks | Audit |
|---|---|---|---|---|---|---|---|---|
| PY | initial | 1007.8 s | agent idle | $3.9465 | 61 | 75,262 | 26/26 | OK |
| PY | optimize | 1809.0 s | time limit | $7.7563 | 84 | 116,738 | 26/26 | OK |
| TS | initial | 790.0 s | agent idle | $2.8061 | 54 | 59,118 | 26/26 | OK |
| TS | optimize | 1809.2 s | time limit | $6.4121 | 77 | 111,557 | 26/26 | OK |
| RS | initial | 1813.3 s | time limit | $4.1295 | 64 | 81,382 | 26/26 | OK |
| RS | optimize | 1809.7 s | time limit | $3.7142 | 48 | 58,469 | 26/26 | OK |

- Rust is the only lane whose first build ran into the 30-minute cap; the snapshot at the cap still passes everything.
- Snapshots rebuilt outside the sessions from each BUILD.md (TS: `npm ci --offline` + `tsc`; RS:
  `cargo build --release --offline` with the vendored crates, about 80 s each). No repair prompt was needed.
- The RS optimize session log holds one Claude Code placeholder message ("No response requested.", model
  `<synthetic>`, zero tokens) at session start; not a model call.

## 11 Nov 2026: sitting 1, first attempt aborted
- The sitting rebuilds every snapshot from scratch by its BUILD.md. TS optimize then failed to start: its launcher
  loads `dist/tinycoder-<cmd>.js` bundles and V8 code caches, which come from two build scripts the session wrote
  (`src/build/bundle.ts`, `src/build/warm.ts`) but never added to BUILD.md (the session ended at the 30-minute cap).
- The hidden checks had passed earlier because the snapshot kept its own `dist/`, so my checker never tested a
  clean build by BUILD.md alone. That is a gap in the checker, not in the build.
- With the two extra steps (`node dist/build/bundle.js`, `node dist/build/warm.js`) the clean build passes 26/26 and
  the bundles are byte-identical to the shipped ones.
- The partial data (round 1, five of six builds) was not used and is not published.

## 11 Nov 2026: TS optimize repair (the repair prompt, the Experiment 3 rule)
- `prompts/repair.txt` with the failure report (clean build by BUILD.md, then `serve files` exits: ENOENT
  `dist/tinycoder-files.js`), sent with `--resume` into the same TS session. It got the failure, not the fix.
- 190.1 s, agent idle, 11 API calls, 11,094 output tokens, $2.8905 estimated. Network: api.anthropic.com only.
- It added the two steps to BUILD.md (`node dist/build/bundle.js`, `node dist/build/warm.js`), added
  `npm run build`, and made the launcher fall back to `dist/main.js` if the bundles are missing.
- The clean build from BUILD.md alone passes 26/26 and the audit. TS optimize is measured from `snapshots/ts/repair`.
  TS's fix rounds: 1.
- The sitting now runs each build's own BUILD.md commands as written and runs the hidden checks and audit on every
  fresh build before measuring.

## 11 Nov 2026: sitting 1 (official, no model calls)
- 16:21-16:33 EST, Apple M1, 8 GB, AC power, seed 20261111, 5 rounds, the six builds shuffled in each round. Every
  fresh build passed 26/26 and the audit first. Raw: results/raw/sitting-1.jsonl + .meta.json.
- Medians of the 5 round medians (range of round medians in the jsonl):

| Build | Server start | read_file | search | Agent 20 steps, fake API | Agent process RSS | files server RSS | Build from clean | Source lines | Deps (total) |
|---|---|---|---|---|---|---|---|---|---|
| py-initial | 306 ms | 577 us | 1,676 us | 961 ms | 106 MB | 64.7 MB | 0.1 s | 825 | 32 |
| py-optimize | 239 ms | 508 us | 1,088 us | 623 ms | 73.5 MB | 55.6 MB | 0.1 s | 1,167 | 32 |
| ts-initial | 120 ms | 240 us | 763 us | 414 ms | 97.2 MB | 72.1 MB | 2.9 s | 931 | 126 |
| ts-optimize | 84 ms | 237 us | 405 us | 318 ms | 95.3 MB | 68.1 MB | 4.4 s | 1,321 | 126 |
| rs-initial | 5.8 ms | 204 us | 320 us | 99 ms | 20.0 MB | 10.1 MB | 88 s | 1,255 | 224 |
| rs-optimize | 3.0 ms | 200 us | 301 us | 102 ms | 20.1 MB | 5.1 MB | 91 s | 1,421 | 224 |

- Caveats, to keep on screen or in notes:
  - Per-call times include the stdlib Python client's own JSON parsing; that cost is the same for every build, so
    roughly 200 us is the floor here (Rust sits at it for read_file).
  - The process-tree memory sample (every 50 ms) also counts the task's own python3 test runs, and it missed the
    Rust optimize run once (3.8 MB) because the whole run takes about 100 ms. Use the per-process numbers.
  - Load average was high before the sitting (10.2, from the checks just before) and the clean builds and checks
    run inside it before the timed rounds. A second sitting at a quiet time would confirm the numbers.
  - The fake API answers instantly; a real Sonnet call takes seconds, so these are the time outside the model only.

## 11 Nov 2026: sitting 2 (confirmation, quieter Mac)
- 16:56-17:07 EST, same measure.py (sha d4d573f06c34), same snapshots, AC power, seed 20261112. Load before 2.6
  (sitting 1: 10.2). Open apps were quit by hand (the quit script was blocked by macOS Automation permission).
- Every fresh build 26/26 + audit again. Every runtime number within 5% of sitting 1, except Rust optimize's 3.0 vs
  3.3 ms startup and 102 vs 94 ms agent run (well under a millisecond and 10 ms apart). Memory within 1%.
- Rust clean builds were faster on the quieter Mac: 75 and 77 s (sitting 1: 88 and 91 s).
- Sitting 1's numbers stand; the film can use either sitting, with the range across both.

## 11 Nov 2026: real agent runs (Sonnet 5.5, effort medium, via OpenRouter pinned to Anthropic)
- No Anthropic API key was available; an OpenRouter key is used through tools/openrouter_relay.py, the same for all
  lanes (swaps the key header and model name, pins provider Anthropic with no fallback, logs every call).
  Compatibility test first: 2 turns, $0.0019, effort accepted, served by Anthropic.
- 45 runs = 3 tasks x 3 lanes (optimize builds) x 5, lanes rotated within each round; one at a time with a credit check.
  Total $1.1727 (OpenRouter's own cost field). Every call HTTP 200, every call served by Anthropic.
- 45/45 finished "done" and pass the task's tests and the hidden tests. No blocked calls. Steps: t1 and t2 always 4,
  t3 always 3, in every lane. Per-run results: results/agent/*.json; full calls + diffs in sessions/private/agent-runs.
- The model is about 99% of each run's time in every lane (tool calls 69-83 ms per run in total). See STATUS for the
  outside-the-model time per lane.
