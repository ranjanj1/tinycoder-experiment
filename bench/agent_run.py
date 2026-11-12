"""One real agent run: a tinycoder build fixes one task with Claude Sonnet 5.5 (via tools/openrouter_relay.py,
served by Anthropic). Run from the repo root with /usr/bin/python3, outside the sandbox.

  /usr/bin/python3 bench/agent_run.py <py|ts|rs> <t1-bugfix|t2-feature|t3-failing> <run-number> [--min-credit 1.5]

Uses the optimize build of the lane from builds/ (made and checked by the benchmark sitting). Checks the OpenRouter
credit first (free call) and refuses to start below --min-credit. Afterwards: the task's own tests, then the hidden
tests on a copy of the finished workspace. Writes results/agent/<lane>-<task>-<n>.json; full model calls and the
workspace diff go to sessions/private/agent-runs/<same id>/.
"""
import os, argparse, json, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = '/Users/Shared/tc-kit'
RUN_PATH = '%s/bin-ts:%s/bin-py:/usr/bin:/bin' % (KIT, KIT)
PORT = 18480


def openrouter_key():
    """OPENROUTER_API_KEY from the environment, else from a .env file in the repo root (git-ignored). Never printed."""
    if os.environ.get('OPENROUTER_API_KEY'):
        return os.environ['OPENROUTER_API_KEY']
    env = ROOT / '.env'
    for l in (env.read_text().splitlines() if env.exists() else []):
        if l.startswith('OPENROUTER_API_KEY='):
            return l.split('=', 1)[1].strip().strip('"\'')
    sys.exit('set OPENROUTER_API_KEY (environment or .env in the repo root)')


def credit():
    k = openrouter_key()
    h = {'Authorization': 'Bearer ' + k}
    c = json.load(urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/credits', headers=h), timeout=30))['data']
    kk = json.load(urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/key', headers=h), timeout=30))['data']
    acct = round(c['total_credits'] - c['total_usage'], 4)
    key_left = kk.get('limit_remaining')
    return round(min(acct, key_left) if key_left is not None else acct, 4), acct, key_left


def tests(ws, extra=()):
    w = Path(tempfile.mkdtemp(prefix='tc-ar-chk-')) / 'ws'
    shutil.copytree(ws, w)
    for f in extra:
        shutil.copy(f, w / f.name)
    p = subprocess.run(['python3', '-m', 'unittest', '-q'], cwd=w, capture_output=True, text=True, timeout=120,
                       env={'PATH': RUN_PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8'})
    shutil.rmtree(w.parent, ignore_errors=True)
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lane', choices=['py', 'ts', 'rs'])
    ap.add_argument('task', choices=['t1-bugfix', 't2-feature', 't3-failing'])
    ap.add_argument('n', type=int)
    ap.add_argument('--min-credit', type=float, default=1.5)
    o = ap.parse_args()
    rid = '%s-%s-%d' % (o.lane, o.task, o.n)
    out = ROOT / 'results' / 'agent' / (rid + '.json')
    if out.exists():
        sys.exit('%s already ran; not repeating' % rid)
    binp = ROOT / 'builds' / ('%s-optimize' % o.lane) / 'bin' / 'tinycoder'
    if not binp.exists():
        sys.exit('no build at %s (run the benchmark sitting first)' % binp)
    left, acct, key_left = credit()
    if left < o.min_credit:
        sys.exit('credit below $%.2f; not starting' % o.min_credit)

    logd = ROOT / 'sessions' / 'private' / 'agent-runs' / rid
    logd.mkdir(parents=True, exist_ok=True)
    relay = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'tools/openrouter_relay.py'), '--port', str(PORT), '--log', str(logd)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not relay.stdout.readline().startswith('RELAY'):
        relay.kill()
        sys.exit('relay did not start')
    task = ROOT / 'bench' / 'tasks' / o.task
    base = Path(tempfile.mkdtemp(prefix='tc-ar-'))
    ws, runs = base / 'ws', base / 'runs'
    shutil.copytree(task / 'repo', ws)
    runs.mkdir()
    env = {'PATH': RUN_PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % PORT,
           'ANTHROPIC_API_KEY': 'relay-dummy', 'TINYCODER_RUN_ID': rid}
    t0 = time.time()
    try:
        p = subprocess.run([str(binp), 'run', '--task', str(task), '--workspace', str(ws), '--runs', str(runs)],
                           env=env, capture_output=True, text=True, timeout=1800)
        code, stdout, stderr = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        code, stdout, stderr = 'timeout', (e.stdout or b'').decode() if isinstance(e.stdout, bytes) else (e.stdout or ''), ''
    wall = round(time.time() - t0, 1)
    relay.terminate()
    relay.wait()

    try:
        final = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        final = None
    visible_ok, visible_out = tests(ws)
    hidden_ok, hidden_out = tests(ws, sorted((task / 'hidden').glob('*.py')))
    diff = subprocess.run(['/usr/bin/diff', '-ruN', '-x', '__pycache__', str(task / 'repo'), str(ws)], capture_output=True, text=True).stdout
    (logd / 'workspace.diff').write_text(diff.replace(str(task / 'repo'), 'a').replace(str(ws), 'b'))
    (logd / 'stderr.txt').write_text(stderr[-20000:])
    if (runs / (rid + '.jsonl')).exists():
        shutil.copy(runs / (rid + '.jsonl'), logd / 'runs.jsonl')
    calls = [json.loads(l) for l in (logd / 'calls.jsonl').read_text().splitlines()] if (logd / 'calls.jsonl').exists() else []
    cost = round(sum((c['response'].get('usage') or {}).get('cost') or 0 for c in calls), 6)
    providers = sorted({c['response'].get('provider') for c in calls if isinstance(c['response'], dict)} - {None})
    rec = {'id': rid, 'lane': o.lane, 'task': o.task, 'n': o.n, 'build': '%s-optimize' % o.lane, 'model': 'claude-sonnet-5-5',
           'route': 'OpenRouter, provider pinned to Anthropic', 'exit_code': code, 'final': final, 'harness_wall_s': wall,
           'visible_tests_pass': visible_ok, 'hidden_tests_pass': hidden_ok, 'model_calls': len(calls),
           'http_statuses': [c['status'] for c in calls], 'providers': providers, 'cost_usd_openrouter': cost,
           'diff_lines': diff.count('\n'),
           'started': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(t0))}
    if not hidden_ok:
        rec['hidden_tail'] = hidden_out[-600:]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=1))
    shutil.rmtree(base, ignore_errors=True)
    f = final or {}
    print('%s: status %s, %s steps, %s tool calls, %s blocked, visible %s, hidden %s, %d model calls, $%.4f, %.1f s' % (
        rid, f.get('status'), f.get('steps'), f.get('tool_calls'), f.get('blocked_calls'), visible_ok, hidden_ok, len(calls), cost, wall))


if __name__ == '__main__':
    main()
