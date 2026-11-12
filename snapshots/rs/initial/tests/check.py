"""Visible checks for tinycoder. Standard library only, Python 3.9+.

  python3 tests/check.py bin/tinycoder [--only name,name] [--json report.json]

Each check prints PASS or FAIL with a reason. Exit code 0 only when every check passes.
Passing these is required but not enough: SPEC.md section 5.
"""
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mcp_client import Client, MCPError, clean_env  # noqa: E402

ROOT = HERE.parent
SYSTEM = (ROOT / 'shared' / 'system.txt').read_text()
FILES_TOOLS = ['list_files', 'read_file', 'write_file', 'edit_file', 'search']
SHELL_TOOLS = ['run_tests', 'run_command']
REQUIRED = {'list_files': [], 'read_file': ['path'], 'write_file': ['path', 'content'],
            'edit_file': ['path', 'old_text', 'new_text'], 'search': ['pattern'], 'run_tests': [], 'run_command': ['argv']}
CASES = []
FAKE_PORTS = list(range(18401, 18409))
BIN = None


class Fail(Exception):
    pass


def case(fn):
    CASES.append(fn)
    return fn


def need(cond, msg):
    if not cond:
        raise Fail(msg)


# ---------- fixtures ----------

def make_workspace():
    """A small repo plus a file outside it. Returns (workspace, outside_dir)."""
    base = Path(tempfile.mkdtemp(prefix='tc-check-'))
    ws, out = base / 'ws', base / 'outside'
    (ws / 'src').mkdir(parents=True)
    (ws / '.git').mkdir()
    out.mkdir()
    (ws / 'README.md').write_text('# demo\nA small demo repo.\n')
    (ws / 'src' / 'app.py').write_text(''.join('line %d\n' % i for i in range(1, 11)))
    (ws / 'src' / 'util.py').write_text('def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n')
    (ws / '.git' / 'config').write_text('[core]\n')
    (out / 'outside.txt').write_text('SECRET-OUTSIDE\n')
    return ws, out


def copy_task(name):
    src = HERE / 'tasks' / name
    base = Path(tempfile.mkdtemp(prefix='tc-task-'))
    shutil.copytree(src / 'repo', base / 'ws')
    return src, base / 'ws'


def serve(kind, ws, task=None):
    argv = [BIN, 'serve', kind, '--workspace', str(ws)] + (['--task', str(task)] if task else [])
    c = Client(argv, env=clean_env())
    c.initialize()
    return c


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Fake:
    """Runs tests/fake_api.py with a script; .requests() reads what the agent sent."""

    def __init__(self, replies, repeat_last=False):
        self.dir = Path(tempfile.mkdtemp(prefix='tc-fake-'))
        (self.dir / 'script.json').write_text(json.dumps({'replies': replies, 'repeat_last': repeat_last}))
        self.log = self.dir / 'requests.jsonl'
        # Fixed ports: the session sandbox allows only these local ports (plus its network filter).
        for port in FAKE_PORTS:
            self.p = subprocess.Popen([sys.executable, str(HERE / 'fake_api.py'), '--script', str(self.dir / 'script.json'),
                                       '--log', str(self.log), '--port', str(port)], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True)
            line = self.p.stdout.readline()
            if line.startswith('PORT'):
                self.port = int(line.split()[1])
                return
            self.p.wait()
        raise Fail('no free fake API port in %d-%d' % (FAKE_PORTS[0], FAKE_PORTS[-1]))

    def requests(self):
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    def stop(self):
        self.p.terminate()
        self.p.wait()


def run_agent(task_dir, ws, fake, run_id='check-run', timeout=180):
    runs = Path(tempfile.mkdtemp(prefix='tc-runs-'))
    env = clean_env({'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % fake.port, 'ANTHROPIC_API_KEY': 'test-key',
                     'TINYCODER_RUN_ID': run_id})
    t0 = time.time()
    p = subprocess.run([BIN, 'run', '--task', str(task_dir), '--workspace', str(ws), '--runs', str(runs)],
                       env=env, capture_output=True, text=True, timeout=timeout)
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    need(len(lines) == 1, 'run must print exactly one line on stdout, got %d: %r' % (len(lines), p.stdout[-300:]))
    try:
        final = json.loads(lines[0])
    except ValueError:
        raise Fail('stdout is not JSON: %r' % lines[0][:200])
    log = runs / ('%s.jsonl' % run_id)
    need(log.exists(), 'runs file %s.jsonl not written' % run_id)
    events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    return {'code': p.returncode, 'final': final, 'events': events, 'stderr': p.stderr, 'secs': time.time() - t0}


def msg(content, stop='tool_use', **usage):
    return {'message': {'content': content, 'stop_reason': stop, 'usage': usage}}


def tool_use(i, name, args):
    return {'type': 'tool_use', 'id': 'toolu_%02d' % i, 'name': name, 'input': args}


def text_of(content):
    """A tool_result or message content may be a string or a list of text blocks."""
    if isinstance(content, str):
        return content
    return ''.join(b.get('text', '') for b in content if b.get('type') == 'text')


def final_ok(r, status, code):
    f = r['final']
    for k in ('status', 'reason', 'steps', 'tool_calls', 'blocked_calls', 'input_tokens', 'output_tokens',
              'wall_ms', 'llm_ms', 'tool_ms', 'final_text'):
        need(k in f, 'final output is missing ' + k)
    need(f['status'] == status, 'status %r, expected %r (stderr: %s)' % (f['status'], status, r['stderr'][-300:]))
    need(r['code'] == code, 'exit code %d, expected %d' % (r['code'], code))
    last = r['events'][-1] if r['events'] else {}
    need(last.get('type') == 'final', 'last line of the runs file must have type "final"')
    need({k: v for k, v in last.items() if k != 'type'} == f, 'last runs line must equal the stdout object plus "type"')


def no_problems(fake):
    for q in fake.requests():
        need(not q['problems'], 'request %d: %s' % (q['n'], '; '.join(q['problems'])))


# ---------- MCP server checks ----------

@case
def files_tools_listed():
    ws, _ = make_workspace()
    c = serve('files', ws)
    try:
        tools = c.tools()
        need([t['name'] for t in tools] == FILES_TOOLS, 'files tools/order: %s' % [t['name'] for t in tools])
        for t in tools:
            s = t.get('inputSchema') or {}
            need(s.get('type') == 'object', '%s: inputSchema type must be object' % t['name'])
            need(sorted(s.get('required', [])) == sorted(REQUIRED[t['name']]), '%s: required is %s' % (t['name'], s.get('required')))
            need(t.get('description'), '%s: needs a description' % t['name'])
    finally:
        c.close()


@case
def shell_tools_listed():
    src, ws = copy_task('practice-pass')
    c = serve('shell', ws, src / 'task.json')
    try:
        tools = c.tools()
        need([t['name'] for t in tools] == SHELL_TOOLS, 'shell tools/order: %s' % [t['name'] for t in tools])
        for t in tools:
            s = t.get('inputSchema') or {}
            need(s.get('type') == 'object', '%s: inputSchema type must be object' % t['name'])
            need(sorted(s.get('required', [])) == sorted(REQUIRED[t['name']]), '%s: required is %s' % (t['name'], s.get('required')))
    finally:
        c.close()


@case
def list_and_read():
    ws, _ = make_workspace()
    c = serve('files', ws)
    try:
        err, r = c.call('list_files', {})
        need(not err, 'list_files failed: %s' % r)
        paths = [e['path'] for e in r['entries']]
        need(paths == sorted(paths), 'entries must be sorted by path')
        need(not any(p == '.git' or p.startswith('.git/') for p in paths), '.git must be skipped')
        need({'README.md', 'src', 'src/app.py', 'src/util.py'} <= set(paths), 'missing entries: %s' % paths)
        byp = {e['path']: e for e in r['entries']}
        need(byp['src']['type'] == 'dir' and byp['README.md']['type'] == 'file', 'wrong types')
        need(byp['README.md']['size'] == len('# demo\nA small demo repo.\n'), 'wrong size')
        err, r = c.call('read_file', {'path': 'src/app.py'})
        need(not err and r['total_lines'] == 10 and r['content'].startswith('line 1\n'), 'read_file whole: %s' % r)
        err, r = c.call('read_file', {'path': 'src/app.py', 'start_line': 3, 'end_line': 4})
        need(not err and r['content'] == 'line 3\nline 4\n' and r['total_lines'] == 10, 'read_file 3-4: %r' % r)
    finally:
        c.close()


@case
def write_and_edit():
    ws, _ = make_workspace()
    c = serve('files', ws)
    try:
        err, r = c.call('write_file', {'path': 'new/dir/x.txt', 'content': 'héllo\n'})
        need(not err and r == {'bytes_written': len('héllo\n'.encode())}, 'write_file: %s' % r)
        need((ws / 'new/dir/x.txt').read_text() == 'héllo\n', 'file content wrong after write')
        err, r = c.call('edit_file', {'path': 'src/util.py', 'old_text': 'a + b', 'new_text': 'b + a'})
        need(not err and r == {'replacements': 1}, 'edit_file single: %s' % r)
        need('return b + a' in (ws / 'src/util.py').read_text(), 'edit not applied')
        err, r = c.call('edit_file', {'path': 'src/util.py', 'old_text': 'nope', 'new_text': 'x'})
        need(err and str(r).startswith('error: '), 'edit_file with no match must be an error starting "error: "')
        err, r = c.call('edit_file', {'path': 'src/util.py', 'old_text': 'return', 'new_text': 'x'})
        need(err, 'edit_file with two matches must be an error')
        need((ws / 'src/util.py').read_text().count('return') == 2, 'a failed edit must not change the file')
    finally:
        c.close()


@case
def search_regex():
    ws, _ = make_workspace()
    c = serve('files', ws)
    try:
        err, r = c.call('search', {'pattern': r'^def (add|sub)'})
        need(not err, 'search failed: %s' % r)
        need(r['matches'] == [{'path': 'src/util.py', 'line': 1, 'text': 'def add(a, b):'},
                              {'path': 'src/util.py', 'line': 5, 'text': 'def sub(a, b):'}], 'search: %s' % r['matches'])
    finally:
        c.close()


@case
def files_refuse_escapes():
    ws, out = make_workspace()
    c = serve('files', ws)
    try:
        for args in ({'path': '../outside/outside.txt'}, {'path': str(out / 'outside.txt')}, {'path': '.git/config'}):
            err, r = c.call('read_file', args)
            need(err and str(r).startswith('error: '), 'read_file %s must be refused with "error: ..."' % args['path'])
        err, r = c.call('write_file', {'path': '../outside/new.txt', 'content': 'x'})
        need(err and not (out / 'new.txt').exists(), 'write outside the workspace must be refused')
    finally:
        c.close()


@case
def shell_tests_and_commands():
    src, ws = copy_task('practice-pass')
    c = serve('shell', ws, src / 'task.json')
    try:
        err, r = c.call('run_tests', {})
        need(not err and r['passed'] is True and r['exit_code'] == 0, 'run_tests on a passing repo: %s' % r)
        need(isinstance(r['duration_ms'], int) and isinstance(r['output'], str), 'run_tests field types')
        err, r = c.call('run_command', {'argv': ['ls']})
        need(not err and r['exit_code'] == 0 and 'calc.py' in r['stdout'], 'run_command ls: %s' % r)
        for argv in (['rm', '-rf', '.'], ['git', 'push'], ['sh', '-c', 'ls']):
            err, r = c.call('run_command', {'argv': argv})
            need(err, 'run_command %s must be refused' % argv)
        need((ws / 'calc.py').exists(), 'a refused command must not run')
    finally:
        c.close()
    src, ws = copy_task('practice-fail')
    c = serve('shell', ws, src / 'task.json')
    try:
        err, r = c.call('run_tests', {})
        need(not err and r['passed'] is False and r['exit_code'] != 0, 'run_tests on a failing repo: %s' % r)
    finally:
        c.close()


# ---------- agent loop checks (fake API) ----------

@case
def agent_basic_loop():
    src, ws = copy_task('practice-pass')
    first = [{'type': 'thinking', 'thinking': '', 'signature': 'sig-check-1'},
             {'type': 'text', 'text': 'Looking around.'},
             tool_use(1, 'list_files', {}), tool_use(2, 'read_file', {'path': 'calc.py'})]
    fake = Fake([msg(first, input_tokens=1000, output_tokens=50),
                 msg([{'type': 'text', 'text': 'Done.'}], 'end_turn', input_tokens=1200, output_tokens=10)])
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'done', 0)
        f = r['final']
        need(f['steps'] == 2 and f['tool_calls'] == 2 and f['blocked_calls'] == 0, 'counts: %s' % f)
        need(f['final_text'] == 'Done.', 'final_text: %r' % f['final_text'])
        need(f['input_tokens'] == 2200 and f['output_tokens'] == 60, 'token totals: %s' % f)
        no_problems(fake)
        q = fake.requests()
        need(len(q) == 2, 'expected 2 API requests, got %d' % len(q))
        b0, b1 = q[0]['body'], q[1]['body']
        sys0 = b0.get('system')
        need((text_of(sys0) if isinstance(sys0, list) else sys0) == SYSTEM, 'system must be shared/system.txt unchanged')
        need(len(b0['messages']) == 1 and b0['messages'][0]['role'] == 'user', 'first request: one user message')
        need(text_of(b0['messages'][0]['content']) == (src / 'TASK.md').read_text(), 'first user message must be TASK.md unchanged')
        need([t['name'] for t in b0['tools']] == FILES_TOOLS + SHELL_TOOLS, 'tools order: %s' % [t['name'] for t in b0['tools']])
        need(all('input_schema' in t for t in b0['tools']), 'every tool needs input_schema')
        m = b1['messages']
        need(len(m) == 3 and [x['role'] for x in m] == ['user', 'assistant', 'user'], 'second request message roles')
        a = m[1]['content']
        need([x['type'] for x in a] == ['thinking', 'text', 'tool_use', 'tool_use'], 'assistant content must be echoed in full')
        need(a[0].get('signature') == 'sig-check-1', 'thinking block must be echoed with its signature')
        need([x['id'] for x in a[2:]] == ['toolu_01', 'toolu_02'], 'tool_use ids changed')
        res = m[2]['content']
        need(isinstance(res, list) and [x['type'] for x in res] == ['tool_result', 'tool_result'], 'all results in one user message')
        need([x['tool_use_id'] for x in res] == ['toolu_01', 'toolu_02'], 'tool_result ids/order')
        need(not any(x.get('is_error') for x in res), 'results must not be errors')
        need('calc.py' in text_of(res[0]['content']) and 'def add' in text_of(res[1]['content']), 'tool results carry the server text')
        kinds = [e['type'] for e in r['events']]
        need(kinds.count('llm') == 2 and kinds.count('tool') == 2, 'runs file: 2 llm + 2 tool lines, got %s' % kinds)
        for e in r['events']:
            if e['type'] == 'tool':
                for k in ('step', 'tool', 'blocked', 'ok', 'duration_ms', 'args_bytes', 'result_bytes'):
                    need(k in e, 'tool line missing ' + k)
            if e['type'] == 'llm':
                for k in ('step', 'duration_ms', 'input_tokens', 'output_tokens', 'cache_read_input_tokens', 'stop_reason'):
                    need(k in e, 'llm line missing ' + k)
    finally:
        fake.stop()


@case
def agent_hook_blocks():
    src, ws = copy_task('practice-pass')
    (ws.parent / 'outside.txt').write_text('SECRET-OUTSIDE\n')
    fake = Fake([msg([tool_use(1, 'read_file', {'path': '../outside.txt'})]),
                 msg([{'type': 'text', 'text': 'Blocked, stopping.'}], 'end_turn')])
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'done', 0)
        need(r['final']['blocked_calls'] == 1, 'blocked_calls: %s' % r['final'])
        res = fake.requests()[1]['body']['messages'][2]['content'][0]
        need(res.get('is_error') is True, 'a blocked call must come back with is_error true')
        need(text_of(res['content']).startswith('blocked by hook: '), 'blocked text: %r' % text_of(res['content'])[:80])
        need('SECRET-OUTSIDE' not in json.dumps(fake.requests()), 'the outside file was read')
        t = [e for e in r['events'] if e['type'] == 'tool']
        need(len(t) == 1 and t[0]['blocked'] is True, 'runs file must log the blocked call')
    finally:
        fake.stop()


@case
def agent_step_limit():
    src, ws = copy_task('practice-pass')
    fake = Fake([msg([tool_use(1, 'list_files', {})])], repeat_last=True)
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'step_limit', 2)
        need(r['final']['steps'] == 20, 'steps: %s' % r['final']['steps'])
        need(len(fake.requests()) == 20, 'expected exactly 20 API requests, got %d' % len(fake.requests()))
    finally:
        fake.stop()


@case
def agent_retries_overloaded():
    src, ws = copy_task('practice-pass')
    fake = Fake([{'status': 529, 'error': 'overloaded_error'}, {'status': 529, 'error': 'overloaded_error'},
                 msg([{'type': 'text', 'text': 'ok'}], 'end_turn')])
    try:
        r = run_agent(src, ws, fake)
        final_ok(r, 'done', 0)
        need(r['final']['steps'] == 1, 'a retry is not a step: %s' % r['final'])
        need(len(fake.requests()) == 3, 'expected 3 requests (2 retries), got %d' % len(fake.requests()))
    finally:
        fake.stop()


def main(cases=None):
    global BIN
    ap = argparse.ArgumentParser()
    ap.add_argument('bin')
    ap.add_argument('--only')
    ap.add_argument('--json')
    o = ap.parse_args()
    BIN = str(Path(o.bin).resolve())
    todo = cases or CASES
    if o.only:
        want = set(o.only.split(','))
        todo = [c for c in todo if c.__name__ in want]
    results = []
    for fn in todo:
        t0 = time.time()
        try:
            fn()
            ok, why = True, ''
        except Fail as e:
            ok, why = False, str(e)
        except (MCPError, subprocess.TimeoutExpired, OSError, KeyError, ValueError, TypeError, IndexError) as e:
            ok, why = False, '%s: %s' % (type(e).__name__, e)
            if os.environ.get('TC_DEBUG'):
                traceback.print_exc()
        results.append({'check': fn.__name__, 'ok': ok, 'why': why, 'secs': round(time.time() - t0, 2)})
        print('%s %-28s %s' % ('PASS' if ok else 'FAIL', fn.__name__, why), flush=True)
    passed = sum(r['ok'] for r in results)
    print('%d/%d passed' % (passed, len(results)))
    if o.json:
        Path(o.json).write_text(json.dumps(results, indent=1))
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
