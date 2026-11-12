"""Runs the AI steps in order. Prints the plan and changes nothing unless --go is given. Run from the repo root.

  .venv/bin/python tools/run_steps.py [--go] [--ids PY,TS,RS]

A step with an events.json already ran, so it is skipped and never rerun. The runner stops at a usage limit, marks that step interrupted
and continues from the next step when started again. A failed step stays failed: nothing here repairs or retries.
"""
import argparse, hashlib, json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORDER = {'PY': 'py', 'TS': 'ts', 'RS': 'rs'}   # Round 2 lanes (PY2, TS2, RS2) are added once their kits exist
STEPS = [('initial', 'initial.txt'), ('optimize', 'optimize.txt')]
BUNDLES = ROOT / 'sessions/private'
# Saved transcripts often lose their spaces, so matching ignores them. The real wording has not been seen here; a false match only pauses the run.
LIMIT = re.compile(r"(usage|session|weekly|hourly)limit|limitreached|hityour[a-z]*limit|outof(extra)?usage|resets?(at|in|on)?\d", re.I)


def techniques():
    rows = re.findall(r'^\| (\w+) \| ([^|]+) \| (.+) \|$', (ROOT / 'RULES.md').read_text(), re.M)
    return {k: '%s with %s' % (lang.strip(), allowed.strip().rstrip('.')) for k, lang, allowed in rows if k != 'ID'}


def fill(sid, name):
    return (ROOT / 'prompts' / name).read_text().strip().replace('{ID}', sid).replace('{TECHNIQUES}', techniques()[sid])


def hit_limit(transcript):
    return bool(LIMIT.search(re.sub(r'\s', '', transcript[-6000:])))


def at_prompt(transcript):
    return 'Esctocancel' in re.sub(r'\s', '', transcript[-1500:])[-400:]


def clear_tmp(sid):
    """Archives then removes agentlab's files in /tmp, so a session cannot read scratch left by an earlier version. No session may be running."""
    AS = ['sudo', '-n', '-u', 'agentlab', '-H', 'bash', '-c']
    # /tmp is shared by every agentlab session, so it is archived and emptied before each lane's first step.
    names = subprocess.run(AS + ['find /private/tmp -maxdepth 1 -mindepth 1 -user agentlab -exec basename {} \\;'], cwd='/tmp', capture_output=True, text=True).stdout.split()
    if names:
        tar = subprocess.run(AS + ['cd /private/tmp && tar -czf - ' + ' '.join(names)], cwd='/tmp', capture_output=True).stdout
        (ROOT / 'sessions/private' / f'tmp-before-{sid.lower()}.tgz').write_bytes(tar)
        subprocess.run(AS + ['cd /private/tmp && rm -rf ' + ' '.join(names)], cwd='/tmp', check=True, capture_output=True)
    print('cleared /tmp:', names or 'nothing', flush=True)


def ready():
    bad = []
    if subprocess.run(['/usr/bin/python3', 'bench/freeze.py', '--check'], cwd=ROOT, capture_output=True).returncode:
        bad.append('frozen files changed')
    for lane in ORDER:
        b = BUNDLES / ('bundle-' + lane.lower())
        for rel in ('RULES.md', 'SPEC.md', 'INTERFACE.md', 'CLAUDE.md', 'shared/system.txt', 'tests/check.py', 'tests/bench_dev.py'):
            if not (b / rel).exists() or hashlib.sha256((ROOT / rel).read_bytes()).digest() != hashlib.sha256((b / rel).read_bytes()).digest():
                bad.append(f'bundle {lane} out of date: {rel}')
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--go', action='store_true'); ap.add_argument('--ids', default=','.join(ORDER))
    o = ap.parse_args()
    bad = ready()
    if bad:
        sys.exit('not starting: ' + '; '.join(bad))
    for sid in o.ids.split(','):
        for i, (step, name) in enumerate(STEPS):
            out = ROOT / 'sessions/private' / sid.lower() / step
            if (out / 'events.json').exists():
                print('done  ', sid, step); continue
            prompt = fill(sid, name)
            if not o.go:
                print('would ', sid, step, 'kit', ORDER[sid], '|', prompt[:110]); continue
            if not i:
                clear_tmp(sid)
            pf = ROOT / 'sessions/private' / sid.lower() / f'{step}-prompt.txt'
            pf.parent.mkdir(parents=True, exist_ok=True); pf.write_text(prompt)
            cmd = [sys.executable, str(ROOT / 'tools/record_session.py'), '--id', sid, '--step', step, '--prompt-file', str(pf), '--kit', ORDER[sid]]
            cmd += ['--resume'] if i else ['--bundle', str(BUNDLES / ('bundle-' + sid.lower()))]
            code = subprocess.run(cmd, cwd=ROOT).returncode
            ev = json.loads((out / 'events.json').read_text()) if (out / 'events.json').exists() else {}
            if code or not ev:
                sys.exit(f'stopped: recorder failed on {sid} {step}, exit {code}')
            if hit_limit((out / 'transcript.txt').read_text()):
                ev['end'] = 'interrupted_usage_limit'
                (out / 'events.json').write_text(json.dumps(ev))
                sys.exit(f'stopped: usage limit during {sid} {step}; recorded as interrupted. Start again after it resets.')
            if at_prompt((out / 'transcript.txt').read_text()):
                sys.exit(f'stopped: {sid} {step} ended on an unanswered prompt, which is a recorder fault. Check before continuing.')


if __name__ == '__main__':
    main()
