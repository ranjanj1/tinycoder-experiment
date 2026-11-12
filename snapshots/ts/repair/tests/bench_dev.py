"""Development benchmark for the optimize step. Standard library only, Python 3.9+.

  python3 tests/bench_dev.py bin/tinycoder [--repeat 5]

Measures, from outside the program:
  startup_ms      process start to the MCP initialize reply, for each server (median of --repeat)
  call_us         median time of one tools/call through a plain MCP client: read_file (300 calls), search (50),
                  run_command ls (20)
  peak_rss_mb     peak memory of one server process across 300 read_file calls (/usr/bin/time -l)
  agent_overhead  a 12-step scripted agent run against the fake API (no model delay): wall time and the agent's
                  own peak memory. Everything here is time spent outside the model.
The official benchmark measures the same kinds of things with different inputs and counts.
"""
import argparse, json, re, statistics, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check  # noqa: E402
from mcp_client import Client, clean_env  # noqa: E402


def rss_mb(stderr_text):
    m = re.search(r'(\d+)\s+maximum resident set size', stderr_text)
    return round(int(m.group(1)) / 1048576, 1) if m else None


def startup(binp, kind, ws, task, n):
    out = []
    for _ in range(n):
        argv = [binp, 'serve', kind, '--workspace', str(ws)] + (['--task', str(task)] if task else [])
        c = Client(argv, env=clean_env())
        c.initialize()
        out.append((c.t_ready - c.t_spawn) * 1000)
        c.close()
    return round(statistics.median(out), 1)


def per_call(c, name, args, n):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        err, _ = c.call(name, args)
        ts.append((time.perf_counter() - t0) * 1e6)
        if err:
            raise SystemExit('%s failed during the benchmark' % name)
    return round(statistics.median(ts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bin')
    ap.add_argument('--repeat', type=int, default=5)
    o = ap.parse_args()
    binp = str(Path(o.bin).resolve())
    check.BIN = binp
    ws, _ = check.make_workspace()
    for i in range(40):
        (ws / 'src' / ('mod%02d.py' % i)).write_text(''.join('def f%d_%d(x):\n    return x + %d\n\n' % (i, k, k) for k in range(30)))
    src, tws = check.copy_task('practice-pass')
    res = {'startup_ms': {'files': startup(binp, 'files', ws, None, o.repeat),
                          'shell': startup(binp, 'shell', tws, src / 'task.json', o.repeat)}}

    c = Client(['/usr/bin/time', '-l', binp, 'serve', 'files', '--workspace', str(ws)], env=clean_env())
    c.initialize()
    res['call_us'] = {'read_file': per_call(c, 'read_file', {'path': 'src/mod07.py'}, 300),
                      'search': per_call(c, 'search', {'pattern': r'return x \+ 2\d'}, 50)}
    c.close()
    time.sleep(0.2)
    res['peak_rss_mb'] = {'files': rss_mb(''.join(c.stderr))}
    s = Client(['/usr/bin/time', '-l', binp, 'serve', 'shell', '--workspace', str(tws), '--task', str(src / 'task.json')], env=clean_env())
    s.initialize()
    res['call_us']['run_command'] = per_call(s, 'run_command', {'argv': ['ls']}, 20)
    s.close()
    time.sleep(0.2)
    res['peak_rss_mb']['shell'] = rss_mb(''.join(s.stderr))

    steps = [check.msg([check.tool_use(i, n, a)]) for i, (n, a) in enumerate(
        [('list_files', {}), ('read_file', {'path': 'calc.py'}), ('search', {'pattern': 'def '}), ('run_tests', {})] * 3, 1)]
    walls = []
    for _ in range(max(1, o.repeat // 2)):
        src, tws = check.copy_task('practice-pass')
        fake = check.Fake(steps[:11] + [check.msg([{'type': 'text', 'text': 'done'}], 'end_turn')])
        runs = Path(tempfile.mkdtemp(prefix='tc-bench-'))
        env = clean_env({'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % fake.port, 'ANTHROPIC_API_KEY': 'test-key', 'TINYCODER_RUN_ID': 'bench'})
        t0 = time.perf_counter()
        p = subprocess.run(['/usr/bin/time', '-l', binp, 'run', '--task', str(src), '--workspace', str(tws), '--runs', str(runs)],
                           env=env, capture_output=True, text=True, timeout=300)
        walls.append(((time.perf_counter() - t0) * 1000, rss_mb(p.stderr)))
        fake.stop()
        if p.returncode != 0:
            raise SystemExit('scripted agent run failed: ' + p.stderr[-400:])
    res['agent_overhead'] = {'steps': 12, 'wall_ms': round(statistics.median(w for w, _ in walls), 1),
                             'peak_rss_mb_largest_process': max(m or 0 for _, m in walls)}
    print(json.dumps(res, indent=1))


if __name__ == '__main__':
    main()
