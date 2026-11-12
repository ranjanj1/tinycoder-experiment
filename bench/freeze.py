"""Writes bench/FROZEN.json: hashes of everything that defines the test. `python3 bench/freeze.py --check` must pass
before any Claude session or official run. Run from the repo root."""
import glob, hashlib, json, sys

F = ['RULES.md', 'SPEC.md', 'INTERFACE.md', 'CLAUDE.md', 'shared/system.txt', 'sandbox/session.sb.template',
     'tools/record_session.py', 'tools/run_steps.py', 'tools/net_filter.py', 'bench/make_bundle.py', 'bench/validate.py', 'bench/audit.py'] + \
    sorted(glob.glob('prompts/*.txt')) + sorted(glob.glob('tests/*.py')) + \
    sorted(f for f in glob.glob('tests/tasks/**/*', recursive=True) + glob.glob('bench/tasks/**/*', recursive=True)
           if not f.endswith('/') and '__pycache__' not in f and '.' in f.split('/')[-1])
now = {f: hashlib.sha256(open(f, 'rb').read()).hexdigest() for f in F}
if '--check' in sys.argv:
    old = json.load(open('bench/FROZEN.json'))['files']
    bad = sorted(k for k in set(now) | set(old) if old.get(k) != now.get(k))
    print('changed since freeze:', bad or 'nothing')
    sys.exit(1 if bad else 0)
d = {'version': 1, 'date': sys.argv[1] if len(sys.argv) > 1 else 'unset', 'files': now}
d['protocol_hash'] = hashlib.sha256(json.dumps(now, sort_keys=True).encode()).hexdigest()
json.dump(d, open('bench/FROZEN.json', 'w'), indent=1)
print('frozen', d['protocol_hash'][:16], len(now), 'files')
