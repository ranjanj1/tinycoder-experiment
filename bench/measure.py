"""Official benchmark sitting. Standard library only; run with /usr/bin/python3 from the repo root, outside the sandbox,
on a quiet Mac on AC power. Nothing here calls a model: the agent runs talk to the local fake API.

  /usr/bin/python3 bench/measure.py <sitting-name> [--rounds 5] [--seed N] [--only py-initial,...]

1. Build: each snapshot is copied to builds/<lane>-<step>/ with old build outputs removed, then built from scratch
   with the commands its BUILD.md gives (timed; offline kits only).
2. Static: source lines (non-blank, non-comment), direct and total dependencies, size on disk of what it needs besides
   the language runtime.
3. Runtime, <rounds> rounds, the six builds in a new shuffled order each round (fixed seed):
     startup_ms     spawn to the MCP initialize reply, per server, 10 starts (the first of each round kept separately)
     call_us        one tools/call through a plain stdlib MCP client: read_file 500, list_files 100, search 100,
                    write_file 100, run_command ls 30
     server_rss_mb  peak RSS of each server process across those calls (/usr/bin/time -l)
     agent          a 20-step scripted run against the fake API (no model delay), 3 timed runs; then 1 run with the
                    whole process tree's RSS sampled every 50 ms (sum of agent + both servers)
Raw records go to results/raw/<sitting>.jsonl (one per measurement) and results/raw/<sitting>.meta.json.
"""
import argparse, hashlib, io, json, os, platform, random, re, shutil, statistics, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import check  # noqa: E402
from mcp_client import Client as _FrozenClient  # noqa: E402


class Client(_FrozenClient):
    """The frozen test client, reading the server's stdout through a 64 KB buffer. The frozen one iterates an
    unbuffered pipe, which costs one read syscall per byte and made every server look the same on large results."""

    def _read(self):
        for line in io.BufferedReader(self.p.stdout, 1 << 16):
            line = line.strip()
            if line:
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    self.q.put({'_bad_line': line.decode('utf-8', 'replace')[:300]})
        self.q.put(None)

KIT = Path('/Users/Shared/tc-kit')
# Same PATH for every lane at run time, so the task's python3 (run_tests, run_command) is the same interpreter.
RUN_PATH = '%s/bin-ts:%s/bin-py:/usr/bin:/bin' % (KIT, KIT)
BUILDS = [(l, s) for l in ('py', 'ts', 'rs') for s in ('initial', 'optimize')]
# The measured version of each build: TS optimize is its repaired snapshot (HISTORY.md, 11 Nov).
SNAPSHOT = {('ts', 'optimize'): 'repair'}
SRC_EXT = {'py': ('.py',), 'ts': ('.ts',), 'rs': ('.rs',)}


def env(extra=None):
    e = {'PATH': RUN_PATH, 'HOME': os.environ['HOME'], 'LANG': os.environ.get('LANG', 'en_US.UTF-8')}
    e.update(extra or {})
    return e


def sh(cmd, cwd, e):
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=1200)
    if p.returncode:
        raise SystemExit('build step failed in %s: %s\n%s' % (cwd, ' '.join(cmd), (p.stdout + p.stderr)[-1500:]))
    return round(time.perf_counter() - t0, 2)


def du(path):
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob('*') if f.is_file() and not f.is_symlink())


def build_commands(d):
    """The command lines of the first ```sh block in the build's own BUILD.md, run as written."""
    m = re.search(r'```(?:sh|bash)\n(.*?)```', (d / 'BUILD.md').read_text(), re.S)
    if not m:
        raise SystemExit('no sh block in %s/BUILD.md' % d)
    return [l.split('#')[0].strip() for l in m.group(1).splitlines() if l.split('#')[0].strip()]


def build(lane, step):
    """Fresh copy of the snapshot with build outputs removed, then exactly the BUILD.md commands (each timed)."""
    snap = SNAPSHOT.get((lane, step), step)
    d = ROOT / 'builds' / ('%s-%s' % (lane, step))
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(ROOT / 'snapshots' / lane / snap, d, symlinks=True)
    for junk in ('dist', 'target', 'node_modules', '.cargo-home', '.npm-cache'):
        shutil.rmtree(d / junk, ignore_errors=True)
    for pc in list(d.rglob('__pycache__')):
        shutil.rmtree(pc)
    if lane == 'rs':
        (d / 'bin' / 'tinycoder').unlink(missing_ok=True)
        e = {'PATH': '%s/bin-rs:/usr/bin:/bin' % KIT, 'HOME': str(d), 'CARGO_HOME': str(d / '.cargo-home'),
             'CARGO_NET_OFFLINE': 'true'}
    elif lane == 'ts':
        shutil.copytree(KIT / 'npm-cache', d / '.npm-cache')
        e = env({'npm_config_cache': str(d / '.npm-cache'), 'npm_config_offline': 'true',
                 'npm_config_update_notifier': 'false', 'npm_config_audit': 'false', 'npm_config_fund': 'false'})
    else:
        e = env()
    t = {}
    for cmd in build_commands(d):
        t[cmd] = sh(['/bin/sh', '-c', cmd], d, e)
    shutil.rmtree(d / '.npm-cache', ignore_errors=True)
    t = {'commands': t, 'total': round(sum(t.values()), 2), 'snapshot': snap}
    return d, t


def verify(d, lane):
    """Hidden checks and the SDK audit on the fresh build; the sitting stops if either fails."""
    v = subprocess.run(['/usr/bin/python3', 'bench/validate.py', str(d / 'bin/tinycoder')], cwd=ROOT, capture_output=True, text=True)
    a = subprocess.run(['/usr/bin/python3', 'bench/audit.py', lane.upper(), str(d)], cwd=ROOT, capture_output=True, text=True)
    last = (v.stdout.strip().splitlines() or [''])[-1]
    if v.returncode or a.returncode or not re.match(r'26/26 passed', last):
        raise SystemExit('fresh build %s failed verification: %s | %s' % (d.name, last, a.stdout.strip()[-300:]))
    return {'hidden': last, 'audit': a.stdout.strip().splitlines()[-1]}


def loc(d, lane):
    n = files = 0
    for f in sorted((d / 'src').rglob('*')):
        if f.suffix in SRC_EXT[lane] and f.is_file():
            files += 1
            for line in f.read_text(errors='replace').splitlines():
                s = line.strip()
                if s and not s.startswith(('#', '//', '/*', '*')):
                    n += 1
    return {'files': files, 'lines': n}


def deps(d, lane):
    if lane == 'py':
        lock = [l for l in (d / 'requirements.lock').read_text().splitlines() if l.strip() and not l.startswith('#')]
        py = (d / 'pyproject.toml').read_text()
        m = re.search(r'dependencies\s*=\s*\[(.*?)\]', py, re.S)
        direct = len(re.findall(r'"[^"]+"', m.group(1))) if m else None
        site = KIT / 'pyenv/lib/python3.12/site-packages'
        return {'direct': direct, 'total': len(lock), 'needs_bytes': du(d / 'src') + du(site),
                'needs_note': 'src + the kit venv site-packages (shared kit)'}
    if lane == 'ts':
        pj = json.loads((d / 'package.json').read_text())
        lock = json.loads((d / 'package-lock.json').read_text())
        return {'direct': len(pj.get('dependencies', {})), 'direct_dev': len(pj.get('devDependencies', {})),
                'total': sum(1 for k in lock.get('packages', {}) if k), 'needs_bytes': du(d / 'node_modules') + du(d / 'dist'),
                'needs_note': 'node_modules (incl. dev deps such as typescript) + dist'}
    lock = (d / 'Cargo.lock').read_text()
    ct = (d / 'Cargo.toml').read_text()
    sec = re.search(r'^\[dependencies\](.*?)(^\[|\Z)', ct, re.S | re.M)
    direct = len([l for l in sec.group(1).splitlines() if re.match(r'^[A-Za-z0-9_-]+\s*=', l)]) if sec else None
    return {'direct': direct, 'total': lock.count('[[package]]') - 1, 'needs_bytes': du(d / 'bin/tinycoder'),
            'needs_note': 'the release binary'}


def rss_mb(text):
    m = re.search(r'(\d+)\s+maximum resident set size', text)
    return round(int(m.group(1)) / 1048576, 2) if m else None


def startups(binp, kind, ws, task, n):
    out = []
    for _ in range(n):
        argv = [binp, 'serve', kind, '--workspace', str(ws)] + (['--task', str(task)] if task else [])
        c = Client(argv, env=env())
        c.initialize()
        out.append(round((c.t_ready - c.t_spawn) * 1000, 2))
        c.close()
    return out


def calls(c, name, args_fn, n):
    ts = []
    for i in range(n):
        t0 = time.perf_counter()
        err, _ = c.call(name, args_fn(i))
        ts.append(round((time.perf_counter() - t0) * 1e6))
        if err:
            raise SystemExit('%s failed during the sitting' % name)
    return ts


def agent_script():
    seq = [('list_files', {}), ('read_file', {'path': 'calc.py'}), ('search', {'pattern': 'def '}),
           ('write_file', {'path': 'notes.txt', 'content': 'note\n' * 200}), ('run_command', {'argv': ['ls']}),
           ('read_file', {'path': 'test_calc.py'}), ('run_tests', {})]
    steps = [check.msg([check.tool_use(i, n, a)]) for i, (n, a) in enumerate((seq * 3)[:19], 1)]
    return steps + [check.msg([{'type': 'text', 'text': 'done'}], 'end_turn')]


def tree_rss_kb(root_pid):
    p = subprocess.run(['/bin/ps', '-A', '-o', 'pid=,ppid=,rss='], capture_output=True, text=True)
    kids, rss = {}, {}
    for line in p.stdout.splitlines():
        a = line.split()
        if len(a) == 3:
            kids.setdefault(int(a[1]), []).append(int(a[0]))
            rss[int(a[0])] = int(a[2])
    tot, todo = 0, [root_pid]
    while todo:
        x = todo.pop()
        tot += rss.get(x, 0)
        todo += kids.get(x, [])
    return tot


def agent_run(binp, sample=False):
    src, tws = check.copy_task('practice-pass')
    fake = check.Fake(agent_script())
    runs = Path(tempfile.mkdtemp(prefix='tc-sit-'))
    e = env({'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % fake.port, 'ANTHROPIC_API_KEY': 'test-key',
             'TINYCODER_RUN_ID': 'sit'})
    argv = [binp, 'run', '--task', str(src), '--workspace', str(tws), '--runs', str(runs)]
    if not sample:
        argv = ['/usr/bin/time', '-l'] + argv
    t0 = time.perf_counter()
    p = subprocess.Popen(argv, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak = [0]
    if sample:
        def poll():
            while p.poll() is None:
                peak[0] = max(peak[0], tree_rss_kb(p.pid))
                time.sleep(0.05)
        th = threading.Thread(target=poll, daemon=True)
        th.start()
    out, err = p.communicate(timeout=300)
    wall = round((time.perf_counter() - t0) * 1000, 1)
    if sample:
        th.join()
    fake.stop()
    if p.returncode != 0:
        raise SystemExit('scripted agent run failed (%s): %s' % (binp, err[-600:]))
    final = json.loads(out.strip().splitlines()[-1])
    shutil.rmtree(tws.parent, ignore_errors=True)
    shutil.rmtree(runs, ignore_errors=True)
    shutil.rmtree(fake.dir, ignore_errors=True)
    if sample:
        return {'tree_peak_rss_mb': round(peak[0] / 1024, 2), 'wall_ms': wall}
    return {'wall_ms': wall, 'largest_process_rss_mb': rss_mb(err), 'steps': final['steps'],
            'tool_calls': final['tool_calls'], 'reported_wall_ms': final['wall_ms'], 'llm_ms': final['llm_ms'],
            'tool_ms': final['tool_ms']}


def round_for(binp):
    ws, _ = check.make_workspace()
    for i in range(40):
        (ws / 'src' / ('mod%02d.py' % i)).write_text(''.join('def g%d_%d(y):\n    return y * %d\n\n' % (i, k, k) for k in range(40)))
    (ws / 'src' / 'big.py').write_text(''.join('value_%04d = %d\n' % (k, k) for k in range(1200)))
    src, tws = check.copy_task('practice-pass')
    r = {'startup_ms': {'files': startups(binp, 'files', ws, None, 10),
                        'shell': startups(binp, 'shell', tws, src / 'task.json', 10)}}
    c = Client(['/usr/bin/time', '-l', binp, 'serve', 'files', '--workspace', str(ws)], env=env())
    c.initialize()
    r['call_us'] = {'read_file': calls(c, 'read_file', lambda i: {'path': 'src/big.py'}, 500),
                    'list_files': calls(c, 'list_files', lambda i: {}, 100),
                    'search': calls(c, 'search', lambda i: {'pattern': r'return y \* 3\d'}, 100),
                    'write_file': calls(c, 'write_file', lambda i: {'path': 'out/w.txt', 'content': 'x' * (1000 + i)}, 100)}
    c.close()
    time.sleep(0.2)
    r['server_rss_mb'] = {'files': rss_mb(''.join(c.stderr))}
    s = Client(['/usr/bin/time', '-l', binp, 'serve', 'shell', '--workspace', str(tws), '--task', str(src / 'task.json')], env=env())
    s.initialize()
    r['call_us']['run_command'] = calls(s, 'run_command', lambda i: {'argv': ['ls']}, 30)
    s.close()
    time.sleep(0.2)
    r['server_rss_mb']['shell'] = rss_mb(''.join(s.stderr))
    r['agent'] = [agent_run(binp) for _ in range(3)]
    r['agent_tree'] = agent_run(binp, sample=True)
    shutil.rmtree(ws.parent, ignore_errors=True)
    shutil.rmtree(tws.parent, ignore_errors=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sitting')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--seed', type=int, default=20261111)
    ap.add_argument('--only', default='')
    o = ap.parse_args()
    chosen = [b for b in BUILDS if not o.only or '%s-%s' % b in o.only.split(',')]
    raw = ROOT / 'results' / 'raw' / ('%s.jsonl' % o.sitting)
    raw.parent.mkdir(parents=True, exist_ok=True)
    if raw.exists():
        raise SystemExit('%s already exists; not overwriting' % raw)
    meta = {'sitting': o.sitting, 'started': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'rounds': o.rounds, 'seed': o.seed,
            'machine': platform.platform(), 'cpu': subprocess.run(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'],
                                                                   capture_output=True, text=True).stdout.strip(),
            'mem_bytes': int(subprocess.run(['/usr/sbin/sysctl', '-n', 'hw.memsize'], capture_output=True, text=True).stdout),
            'power': subprocess.run(['/usr/bin/pmset', '-g', 'batt'], capture_output=True, text=True).stdout.splitlines()[0],
            'load_before': os.getloadavg(), 'harness_python': sys.version.split()[0],
            'measure_py_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'snapshots': {'%s-%s' % b: hashlib.sha256((ROOT / 'snapshots' / b[0] / (SNAPSHOT.get(b, b[1]) + '.sha256')).read_bytes()).hexdigest()
                          for b in chosen},
            'run_path': RUN_PATH}
    f = raw.open('a')

    def put(rec):
        f.write(json.dumps(rec) + '\n')
        f.flush()

    bins = {}
    for lane, step in chosen:
        name = '%s-%s' % (lane, step)
        d, t = build(lane, step)
        bins[name] = str(d / 'bin' / 'tinycoder')
        ver = verify(d, lane)
        put({'kind': 'build', 'build': name, 'seconds': t, 'verify': ver, 'loc': loc(d, lane), 'deps': deps(d, lane)})
        print('built', name, t['total'], ver, flush=True)
    rng = random.Random(o.seed)
    for rnd in range(1, o.rounds + 1):
        order = list(bins)
        rng.shuffle(order)
        print('round', rnd, order, flush=True)
        for name in order:
            check.BIN = bins[name]
            r = round_for(bins[name])
            put({'kind': 'round', 'round': rnd, 'build': name, 'position': order.index(name), **r})
            med = statistics.median(a['wall_ms'] for a in r['agent'])
            print('  %-12s files %.0f ms  read %d us  agent %.0f ms  tree %.0f MB' % (
                name, statistics.median(r['startup_ms']['files']), statistics.median(r['call_us']['read_file']),
                med, r['agent_tree']['tree_peak_rss_mb']), flush=True)
    meta['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    meta['load_after'] = os.getloadavg()
    (raw.parent / ('%s.meta.json' % o.sitting)).write_text(json.dumps(meta, indent=1))
    print('SITTING FINISHED', o.sitting, flush=True)


if __name__ == '__main__':
    main()
