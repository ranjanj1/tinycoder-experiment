"""Hidden checks: every visible check plus the cases below. Never in the bundle. Python 3.9+, standard library only.

  python3 bench/validate.py <bin/tinycoder> [--only a,b] [--json validation/<id>.json]

A version is eligible for timing only if every check passes.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import check  # noqa: E402
from check import Fail, Fake, case, copy_task, make_workspace, msg, need, run_agent, serve, text_of, tool_use, final_ok  # noqa: E402
from mcp_client import Client, clean_env  # noqa: E402

TASKS = ROOT / 'bench' / 'tasks'


# ---------- servers ----------

@case
def h_symlink_escape():
    ws, out = make_workspace()
    os.symlink(str(out), str(ws / 'link'))
    c = serve('files', ws)
    try:
        err, r = c.call('read_file', {'path': 'link/outside.txt'})
        need(err, 'reading through a symlink that leaves the workspace must be refused')
        err, r = c.call('list_files', {})
        need(not err and not any('outside.txt' in e['path'] for e in r['entries']), 'list_files followed a symlink out')
        err, r = c.call('write_file', {'path': 'link/new.txt', 'content': 'x'})
        need(err and not (out / 'new.txt').exists(), 'writing through an outside symlink must be refused')
    finally:
        c.close()


@case
def h_git_variants():
    ws, _ = make_workspace()
    c = serve('files', ws)
    try:
        for p in ('./.git/config', 'src/../.git/config', '.git'):
            err, r = c.call('read_file', {'path': p})
            need(err, 'read_file %s must be refused' % p)
        err, r = c.call('write_file', {'path': '.git/hooks/x', 'content': 'x'})
        need(err and not (ws / '.git/hooks/x').exists(), 'writing into .git must be refused')
    finally:
        c.close()


@case
def h_non_utf8():
    ws, _ = make_workspace()
    (ws / 'blob.bin').write_bytes(b'\xff\xfe\x00def add\n')
    c = serve('files', ws)
    try:
        err, r = c.call('read_file', {'path': 'blob.bin'})
        need(err, 'read_file on non-UTF-8 must be an error')
        err, r = c.call('search', {'pattern': 'def add'})
        need(not err and [m['path'] for m in r['matches']] == ['src/util.py'], 'search must skip non-UTF-8 files: %s' % r)
    finally:
        c.close()


@case
def h_truncation():
    ws, _ = make_workspace()
    (ws / 'many').mkdir()
    for i in range(600):
        (ws / 'many' / ('f%03d.txt' % i)).write_text('hit\n' if i else ''.join('hit %d\n' % k for k in range(300)))
    c = serve('files', ws)
    try:
        err, r = c.call('list_files', {})
        need(not err and len(r['entries']) == 500 and r.get('truncated') is True, 'list_files must stop at 500 with truncated')
        err, r = c.call('search', {'pattern': '^hit'})
        need(not err and len(r['matches']) == 200 and r.get('truncated') is True, 'search must stop at 200 with truncated')
        need(r['matches'][0] == {'path': 'many/f000.txt', 'line': 1, 'text': 'hit 0'}, 'search order: %s' % r['matches'][0])
        err, r = c.call('list_files', {'path': 'src'})
        need(not err and [e['path'] for e in r['entries']] == ['src/app.py', 'src/util.py'], 'list_files of a subfolder: %s' % r)
    finally:
        c.close()


@case
def h_read_variants():
    ws, _ = make_workspace()
    c = serve('files', ws)
    try:
        err, r = c.call('read_file', {'path': 'src/app.py', 'start_line': 9})
        need(not err and r['content'] == 'line 9\nline 10\n', 'start_line only: %r' % r)
        err, r = c.call('read_file', {'path': 'src/app.py', 'start_line': 10, 'end_line': 99})
        need(not err and r['content'] == 'line 10\n', 'end_line beyond the file: %r' % r)
        err, r = c.call('read_file', {'path': 'nope.txt'})
        need(err, 'missing file must be an error')
        err, r = c.call('edit_file', {'path': 'nope.txt', 'old_text': 'a', 'new_text': 'b'})
        need(err and not (ws / 'nope.txt').exists(), 'edit of a missing file must be an error')
    finally:
        c.close()


@case
def h_shell_limits():
    src, ws = copy_task('practice-pass')
    argv = check.BIN, 'serve', 'shell', '--workspace', str(ws), '--task', str(src / 'task.json')
    c = Client(list(argv), env=clean_env({'TC_SECRET_TOKEN': 'leak-me'}))
    c.initialize()
    try:
        err, r = c.call('run_command', {'argv': ['python3', '-c', "print('x' * 20000 + 'END')"]})
        need(not err and len(r['stdout']) <= 8000 and r['stdout'].endswith('END\n'), 'stdout must keep its last 8,000 characters')
        err, r = c.call('run_command', {'argv': ['python3', '-c', 'import os; print(sorted(os.environ))']})
        # macOS's /usr/bin/python3 shim adds SDKROOT, CPATH, LIBRARY_PATH, MANPATH itself; __CF* comes from CoreFoundation
        shim = {'SDKROOT', 'CPATH', 'LIBRARY_PATH', 'MANPATH'}
        keys = [k for k in eval(r['stdout']) if not k.startswith('__CF') and k not in shim]
        need(not err and set(keys) <= {'PATH', 'HOME', 'LANG'}, 'environment must be PATH, HOME, LANG only: %s' % keys)
        err, r = c.call('run_command', {'argv': ['git', 'status']})
        need(not err, 'git status must be allowed')
        for bad in ([], ['git'], ['git', 'commit', '-m', 'x'], [''], ['/bin/ls']):
            err, r = c.call('run_command', {'argv': bad})
            need(err, 'run_command %s must be refused' % bad)
        err, r = c.call('run_command', {'argv': ['python3', '-c', 'import time; time.sleep(45)']}, timeout=60)
        need(not err and r['exit_code'] == -1 and 29000 <= r['duration_ms'] < 40000, '30 s timeout: %s' % r)
    finally:
        c.close()


# ---------- agent ----------

def one_call_then_end(name, args):
    return [msg([tool_use(1, name, args)]), msg([{'type': 'text', 'text': 'end'}], 'end_turn')]


def result_of(fake, k=0):
    return fake.requests()[1]['body']['messages'][2]['content'][k]


@case
def h_hooks():
    for name, args, check_fn in (
            ('read_file', {'path': '/etc/hosts'}, None),
            ('write_file', {'path': 'big.txt', 'content': 'x' * 100001}, lambda ws: not (ws / 'big.txt').exists()),
            ('run_command', {'argv': ['rm', 'calc.py']}, lambda ws: (ws / 'calc.py').exists()),
            ('list_files', {'path': 'src/../..'}, None)):
        src, ws = copy_task('practice-pass')
        fake = Fake(one_call_then_end(name, args))
        try:
            r = run_agent(src, ws, fake)
            final_ok(r, 'done', 0)
            res = result_of(fake)
            need(res.get('is_error') is True and text_of(res['content']).startswith('blocked by hook: '),
                 '%s %s must be blocked by the hook' % (name, str(args)[:60]))
            need(r['final']['blocked_calls'] == 1, 'blocked_calls for %s' % name)
            if check_fn:
                need(check_fn(ws), '%s: the blocked call had an effect' % name)
        finally:
            fake.stop()


@case
def h_server_error_passthrough():
    src, ws = copy_task('practice-pass')
    fake = Fake(one_call_then_end('edit_file', {'path': 'calc.py', 'old_text': 'nope', 'new_text': 'x'}))
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'done', 0)
        res = result_of(fake)
        need(res.get('is_error') is True and text_of(res['content']).startswith('error: '), 'server error must pass through')
        t = [e for e in r['events'] if e['type'] == 'tool'][0]
        need(t['blocked'] is False and t['ok'] is False and r['final']['blocked_calls'] == 0, 'log: %s' % t)
    finally:
        fake.stop()


@case
def h_unknown_tool():
    src, ws = copy_task('practice-pass')
    fake = Fake(one_call_then_end('delete_everything', {}))
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'done', 0)
        need(result_of(fake).get('is_error') is True, 'an unknown tool must come back as an error result')
    finally:
        fake.stop()


@case
def h_parallel_and_text():
    src, ws = copy_task('practice-pass')
    calls = [tool_use(1, 'read_file', {'path': 'calc.py'}), tool_use(2, 'run_tests', {}),
             tool_use(3, 'search', {'pattern': 'def '})]
    fake = Fake([msg(calls), msg([{'type': 'text', 'text': 'A'}, {'type': 'text', 'text': 'B'}], 'end_turn',
                                 cache_read_input_tokens=77)])
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'done', 0)
        res = fake.requests()[1]['body']['messages'][2]['content']
        need([x['tool_use_id'] for x in res] == ['toolu_01', 'toolu_02', 'toolu_03'], 'three results in order, one message')
        need('"passed": true' in text_of(res[1]['content']).replace('"passed":true', '"passed": true'), 'run_tests result')
        need(r['final']['final_text'] == 'A\nB', 'final_text joins text blocks with one newline: %r' % r['final']['final_text'])
        llm = [e for e in r['events'] if e['type'] == 'llm']
        need(llm[-1]['cache_read_input_tokens'] == 77, 'cache_read_input_tokens not logged')
    finally:
        fake.stop()


@case
def h_stop_reasons_and_errors():
    for replies, status, code, n_req in (
            ([msg([{'type': 'text', 'text': 'cut'}], 'max_tokens')], 'error', 1, 1),
            ([msg([], 'refusal')], 'error', 1, 1),
            ([{'status': 400, 'error': 'invalid_request_error'}], 'error', 1, 1),
            ([{'status': 529, 'error': 'overloaded_error'}] * 4, 'error', 1, 4),
            ([{'status': 429, 'error': 'rate_limit_error'}, msg([{'type': 'text', 'text': 'ok'}], 'end_turn')], 'done', 0, 2)):
        src, ws = copy_task('practice-pass')
        fake = Fake(replies)
        try:
            r = run_agent(src, ws, fake)
            final_ok(r, status, code)
            need(len(fake.requests()) == n_req, '%s: expected %d requests, got %d' % (replies[0], n_req, len(fake.requests())))
            if replies[0].get('message', {}).get('stop_reason') in ('max_tokens', 'refusal'):
                need(r['final']['reason'] == replies[0]['message']['stop_reason'], 'reason: %r' % r['final']['reason'])
        finally:
            fake.stop()


@case
def h_default_run_id():
    src, ws = copy_task('practice-pass')
    fake = Fake([msg([{'type': 'text', 'text': 'ok'}], 'end_turn')])
    runs = Path(tempfile.mkdtemp(prefix='tc-runs-'))
    try:
        env = clean_env({'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % fake.port, 'ANTHROPIC_API_KEY': 'test-key'})
        p = subprocess.run([check.BIN, 'run', '--task', str(src), '--workspace', str(ws), '--runs', str(runs)],
                           env=env, capture_output=True, text=True, timeout=120)
        names = [f.name for f in runs.glob('*.jsonl')]
        need(p.returncode == 0 and len(names) == 1 and names[0][:-6].isdigit(), 'default run id file: %s' % names)
    finally:
        fake.stop()


def replay_task(name):
    """Scripted agent applies the known solution, runs the tests, ends. Then the hidden tests must pass."""
    t = TASKS / name
    base = Path(tempfile.mkdtemp(prefix='tc-replay-'))
    ws = base / 'ws'
    subprocess.run(['cp', '-R', str(t / 'repo'), str(ws)], check=True)
    sols = sorted(p for p in (t / 'solution').glob('*.py') if p.read_text() != (t / 'repo' / p.name).read_text())
    calls = [tool_use(i + 1, 'write_file', {'path': p.name, 'content': p.read_text()}) for i, p in enumerate(sols)]
    fake = Fake([msg([tool_use(90, 'list_files', {})]), msg(calls), msg([tool_use(91, 'run_tests', {})]),
                 msg([{'type': 'text', 'text': 'Fixed.'}], 'end_turn')])
    try:
        r = run_agent(t, ws, fake)
        final_ok(r, 'done', 0)
        last = fake.requests()[3]['body']['messages'][-1]['content'][0]
        need('"passed": true' in text_of(last['content']).replace('"passed":true', '"passed": true'), '%s: tests did not pass after the fix' % name)
        for h in (t / 'hidden').glob('*.py'):
            subprocess.run(['cp', str(h), str(ws / h.name)], check=True)
        p = subprocess.run(['python3', '-m', 'unittest', '-q'], cwd=ws, capture_output=True, text=True, env=clean_env())
        need(p.returncode == 0, '%s: hidden tests fail after replay: %s' % (name, p.stderr[-300:]))
    finally:
        fake.stop()


@case
def h_replay_t1():
    replay_task('t1-bugfix')


@case
def h_replay_t2():
    replay_task('t2-feature')


@case
def h_replay_t3():
    replay_task('t3-failing')


if __name__ == '__main__':
    sys.exit(check.main())
