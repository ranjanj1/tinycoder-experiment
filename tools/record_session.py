"""One step of a Claude Code session as `agentlab`, direct to Anthropic, in the session sandbox, recorded as a timed terminal cast.

  .venv/bin/python tools/record_session.py --id PY --step initial --prompt-file f [--bundle dir] [--kit py|ts|rs]
                                                      [--minutes 30] [--cost 10] [--resume]

--resume continues the same session and workspace. Saves to sessions/private/<id>/<step>/: session.cast, transcript.txt,
events.json, usage.json, claude-session.jsonl (private), net.log. Source snapshot and hashes go to snapshots/<id>/<step>/ (build output, node_modules and caches left out).
The login token is read inside the agentlab shell before the sandbox starts, never passed in argv.
"""
import argparse, codecs, fcntl, hashlib, json, os, pty, re, select, struct, subprocess, sys, termios, time
from pathlib import Path
import pyte

ROOT = Path(__file__).resolve().parents[1]
COLS, ROWS = 120, 40
AS = ['sudo', '-n', '-u', 'agentlab', '-H', 'bash', '-c']
MODEL, EFFORT = 'claude-opus-5-5', 'high'
PATHS = '/usr/bin:/bin'
# Per-lane environment: TypeScript installs from the copied npm cache, Rust builds from the vendored crates. Paths are inside the workspace.
LANE_ENV = {'py': 'PYTHONDONTWRITEBYTECODE=1 ',
            'ts': 'npm_config_cache=$PWD/.npm-cache npm_config_offline=true npm_config_update_notifier=false npm_config_audit=false npm_config_fund=false ',
            'rs': 'CARGO_HOME=$PWD/.cargo-home CARGO_NET_OFFLINE=true '}
LSP_OFFER = re.compile(r'LSP plugin recommendation')   # must never be accepted: language tools would differ between sessions
APPROVAL = re.compile(r'Do you want to (proceed|make|create|allow|run|edit|write|overwrite)|Would you like to|Permission needed|requires approval|Esc to cancel')
TRUST = re.compile(r'trust the (contents|files)|Do you trust|one you trust|trust this folder|Folder access', re.I)
BUSY = re.compile(r'esc to interrupt|\d+ shells? ·|shells? still running')   # a waiting background job is not idle
# USD per token at API list price, fitted exactly to Claude Code's own cost-state totals for Opus 5.5 (tests 5 to 8). An estimate: subscription use is not billed this way.
RATE = {'input_tokens': 4e-6, 'output_tokens': 20e-6, 'cache_read_input_tokens': 0.2e-6, 'cache_creation_input_tokens': 8e-6}


def session_log(sid):
    return subprocess.run(AS + [f'cd ~ && cat tccfg/{sid}/projects/-Users-agentlab-tc-{sid}/*.jsonl 2>/dev/null'], cwd='/tmp', capture_output=True, text=True).stdout


def usage_by_message(log):
    seen = {}
    for line in log.splitlines():
        try:
            m = json.loads(line).get('message')
        except ValueError:
            continue
        if isinstance(m, dict) and m.get('usage') and m.get('id'):
            seen[m['id']] = {'model': m.get('model'), **{k: m['usage'].get(k, 0) for k in RATE}}
    return seen


def estimate(rows):
    return sum(r[k] * RATE[k] for r in rows for k in RATE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', required=True); ap.add_argument('--step', required=True); ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--bundle'); ap.add_argument('--kit', required=True, choices=['py', 'ts', 'rs']); ap.add_argument('--minutes', type=float, default=30)
    ap.add_argument('--cost', type=float, default=10); ap.add_argument('--resume', action='store_true')
    o = ap.parse_args()
    sid, kit, max_min = o.id.lower(), o.kit, o.minutes
    if not re.fullmatch(r'[a-z0-9-]+', sid):
        sys.exit('id: letters, digits and dashes only')
    out = ROOT / 'sessions' / 'private' / sid / o.step
    out.mkdir(parents=True, exist_ok=True)
    prompt = Path(o.prompt_file).read_text().strip()
    ws, cfg = f'~/tc/{sid}', f'~/tccfg/{sid}'
    if not o.resume:
        subprocess.run(AS + [f'cd ~ && rm -rf {ws} {cfg} && mkdir -p {ws} {cfg}'], cwd='/tmp', check=True, capture_output=True)
        subprocess.run(AS + [f'cd ~ && cat > {cfg}/settings.json'], cwd='/tmp', check=True, capture_output=True, text=True,
                       input=json.dumps({'enabledPlugins': {}, 'permissions': {'deny': ['WebFetch', 'WebSearch']}}))
        subprocess.run(AS + [f'cd ~ && cat > {cfg}/.claude.json'], cwd='/tmp', check=True, capture_output=True, text=True,
                       input=json.dumps({'hasCompletedOnboarding': True, 'theme': 'light'}))
        subprocess.run(AS + [f'cd ~ && cat > {cfg}/session.sb'], cwd='/tmp', check=True, capture_output=True, text=True,
                       input=(ROOT / 'sandbox' / 'session.sb.template').read_text().replace('@SID@', sid))
        if o.bundle:   # bundle copied into the workspace before the first step
            tar = subprocess.run(['tar', '-C', str(Path(o.bundle).resolve()), '-cf', '-', '.'], check=True, capture_output=True).stdout
            subprocess.run(AS + [f'cd ~ && cd {ws} && tar -xf -'], cwd='/tmp', check=True, capture_output=True, input=tar)
    before = set(usage_by_message(session_log(sid)))
    net = subprocess.Popen([sys.executable, '-I', str(ROOT / 'tools' / 'net_filter.py'), str(out / 'net.log')])
    time.sleep(0.5)
    # Token is read before the sandbox starts; inside it the file is closed and subprocess env scrubbing hides it from the agent's shell.
    cmd = (f'cd {ws} && TOKEN="$(cat ~/lab/claude-token)" && exec env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_MODEL -u ANTHROPIC_BASE_URL '
           f'PATH=/Users/Shared/tc-kit/bin-{kit}:{PATHS} {LANE_ENV[kit]}DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 CLAUDE_CONFIG_DIR=$HOME/tccfg/{sid} '
           f'HTTPS_PROXY=http://127.0.0.1:8899 CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" '
           f'sandbox-exec -f $HOME/tccfg/{sid}/session.sb ~/.local/bin/claude {"--continue " if o.resume else ""}--model {MODEL} --effort {EFFORT}')
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir('/tmp')
        os.environ.update(TERM='xterm-256color', COLUMNS=str(COLS), LINES=str(ROWS))
        os.execvp('sudo', AS + [cmd])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
    dec = codecs.getincrementaldecoder('utf-8')('replace')
    t0, cast, events = time.time(), [], []
    text = lambda: '\n'.join(screen.display)

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                stream.feed(data)
                cast.append([round(time.time() - t0, 3), dec.decode(data)])
                if b'\x1b]11;?' in data:
                    os.write(fd, b'\x1b]11;rgb:ffff/ffff/ffff\x1b\\')   # light mode
                if b'\x1b]10;?' in data:
                    os.write(fd, b'\x1b]10;rgb:1c1c/1c1c/1c1c\x1b\\')
        return True

    def send(s):
        try:
            os.write(fd, s.encode())
        except OSError:
            events.append('agent_exited')

    for _ in range(60):   # start up, answer the folder trust question
        pump(0.5)
        if TRUST.search(text()):
            if 'Yes, I trust this folder' in text():
                for _ in range(3):
                    if re.search(r'❯\s*(\d\.\s*)?Yes, I trust', text()):
                        break
                    send('\x1b[B'); pump(0.6)
            send('\r'); events.append('trust_yes'); pump(3)
            break
        if re.search(r'Try "|What can I help|for shortcuts', text()):
            break
    pump(3)
    send('\x1b[200~' + prompt + '\x1b[201~'); pump(0.5); send('\r'); events.append('prompt_sent')
    start, quiet, alive, reason, checked = time.time(), 0, True, 'time_limit', 0
    while time.time() - start < max_min * 60:
        alive = pump(1.0)
        if time.time() - checked > 15:
            checked = time.time()
            spent = estimate(r for k, r in usage_by_message(session_log(sid)).items() if k not in before)
            if spent >= o.cost:
                reason = 'cost_limit'; break
        t = text()
        if LSP_OFFER.search(t):
            events.append('lsp_declined'); send('2'); pump(1.5); quiet = 0
            continue
        if APPROVAL.search(t) and not TRUST.search(t):
            events.append('approval_yes'); send('\r'); pump(1.5); quiet = 0
            continue
        quiet = 0 if BUSY.search(t) else quiet + 1
        if not alive:
            reason = 'agent_exited'; break
        if quiet >= 8:
            reason = 'agent_idle'; break
    pump(2)
    for _ in range(2):
        send('\x03'); pump(1)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    net.terminate()
    log = session_log(sid)
    (out / 'claude-session.jsonl').write_text(log)   # holds session content: private, never uploaded
    rows = [r for k, r in usage_by_message(log).items() if k not in before]
    totals = {k: sum(r[k] for r in rows) for k in RATE}
    (out / 'usage.json').write_text(json.dumps({'estimated_usd_api_prices': round(estimate(rows), 4), 'api_calls': len(rows), 'models': sorted({r['model'] for r in rows}), **totals}, indent=1))
    snap = ROOT / 'snapshots' / sid / o.step
    snap.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(AS + [f'cd ~/tc/{sid} && tar --exclude ./.npm-cache --exclude node_modules --exclude ./target --exclude ./.cargo-home --exclude __pycache__ -cf - .'], cwd='/tmp', capture_output=True).stdout
    subprocess.run(['tar', '-xf', '-', '-C', str(snap)], input=tar, check=True)
    (snap.parent / f'{o.step}.sha256').write_text(''.join(f'{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.relative_to(snap)}\n' for f in sorted(snap.rglob('*')) if f.is_file()))
    summary = {'session': sid, 'step': o.step, 'model': MODEL, 'effort': EFFORT, 'seconds': round(time.time() - t0, 1), 'end': reason, 'events': events}
    (out / 'session.cast').write_text('\n'.join(json.dumps(c) for c in cast))
    (out / 'events.json').write_text(json.dumps(summary))
    (out / 'transcript.txt').write_text(re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)', '', ''.join(c[1] for c in cast)))
    print(json.dumps(summary), flush=True)


main()
