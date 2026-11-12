"""Reference tinycoder (standard library only; unlike the lanes it hand-writes the MCP protocol, which RULES.md forbids for them). Written outside the sandbox to prove the checks are passable and
correct; it is never shown to a session and is never timed or put in the film as a competitor.

  python3 bench/reference/tinycoder.py serve files --workspace W
  python3 bench/reference/tinycoder.py serve shell --workspace W --task T/task.json
  python3 bench/reference/tinycoder.py run --task T --workspace W --runs R
"""
import argparse, json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
SYSTEM_FILE = HERE.parents[2] / 'shared' / 'system.txt'
ALLOW = {'ls', 'cat', 'head', 'tail', 'wc', 'diff', 'python3'}
GIT_OK = {'status', 'diff', 'log', 'show'}
TAIL = 8000


class ToolError(Exception):
    pass


# ---------------- servers ----------------

def obj(props, required):
    return {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}


S, I = {'type': 'string'}, {'type': 'integer', 'minimum': 1}
FILES = [
    ('list_files', 'List files and folders under a path (recursive, sorted, skips .git, at most 500 entries).',
     obj({'path': S}, [])),
    ('read_file', 'Read a UTF-8 text file, optionally only lines start_line to end_line (1-based, inclusive).',
     obj({'path': S, 'start_line': I, 'end_line': I}, ['path'])),
    ('write_file', 'Write a UTF-8 text file, creating parent folders. Replaces the file if it exists.',
     obj({'path': S, 'content': S}, ['path', 'content'])),
    ('edit_file', 'Replace old_text with new_text in a file. old_text must occur exactly once.',
     obj({'path': S, 'old_text': S, 'new_text': S}, ['path', 'old_text', 'new_text'])),
    ('search', 'Search files for a regular expression. Returns path, line number and line text, at most 200 matches.',
     obj({'pattern': S, 'path': S}, ['pattern'])),
]
SHELL = [
    ('run_tests', "Run the task's test command in the workspace.", obj({}, [])),
    ('run_command', 'Run one allowed command (ls, cat, head, tail, wc, diff, python3, git status/diff/log/show). No shell.',
     obj({'argv': {'type': 'array', 'items': S, 'minItems': 1}}, ['argv'])),
]


def resolve(ws, rel, must_exist=True):
    if not isinstance(rel, str) or rel.startswith('/') or os.path.isabs(rel):
        raise ToolError('path must be relative to the workspace')
    p = (ws / rel).resolve()
    if p != ws and ws not in p.parents:
        raise ToolError('path is outside the workspace')
    r = p.relative_to(ws).parts
    if r and r[0] == '.git':
        raise ToolError('.git is not accessible')
    if must_exist and not p.exists():
        raise ToolError('no such file or folder: ' + rel)
    return p


def read_text(p):
    try:
        return p.read_bytes().decode('utf-8')
    except UnicodeDecodeError:
        raise ToolError('file is not valid UTF-8')


def walk(ws, base):
    out = []
    for dirpath, dirs, files in os.walk(base):
        d = Path(dirpath)
        dirs[:] = sorted(x for x in dirs if not (d == ws and x == '.git') and x != '.git')
        for name in dirs + files:
            q = d / name
            if q.is_symlink() and not str(q.resolve()).startswith(str(ws)):
                continue
            out.append(q)
    return sorted(out, key=lambda q: q.relative_to(ws).as_posix())


def files_tool(ws, name, a):
    if name == 'list_files':
        base = resolve(ws, a.get('path', '.'))
        entries = []
        for q in walk(ws, base):
            entries.append({'path': q.relative_to(ws).as_posix(), 'type': 'dir' if q.is_dir() else 'file',
                            'size': 0 if q.is_dir() else q.stat().st_size})
        r = {'entries': entries[:500]}
        if len(entries) > 500:
            r['truncated'] = True
        return r
    if name == 'read_file':
        p = resolve(ws, a['path'])
        if not p.is_file():
            raise ToolError('not a file')
        text = read_text(p)
        lines = text.splitlines(True)
        s, e = a.get('start_line'), a.get('end_line')
        if s is not None or e is not None:
            s = s or 1
            e = e or len(lines)
            if s < 1 or e < s:
                raise ToolError('bad line range')
            text = ''.join(lines[s - 1:e])
        return {'content': text, 'total_lines': len(lines)}
    if name == 'write_file':
        p = resolve(ws, a['path'], must_exist=False)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = a['content'].encode('utf-8')
        p.write_bytes(data)
        return {'bytes_written': len(data)}
    if name == 'edit_file':
        p = resolve(ws, a['path'])
        text = read_text(p)
        n = text.count(a['old_text']) if a['old_text'] else 0
        if n != 1:
            raise ToolError('old_text occurs %d times, expected exactly once' % n)
        p.write_bytes(text.replace(a['old_text'], a['new_text'], 1).encode('utf-8'))
        return {'replacements': 1}
    if name == 'search':
        try:
            rx = re.compile(a['pattern'])
        except re.error as e:
            raise ToolError('bad pattern: %s' % e)
        base = resolve(ws, a.get('path', '.'))
        items = [base] if base.is_file() else [q for q in walk(ws, base) if q.is_file()]
        matches, more = [], False
        for q in items:
            try:
                lines = q.read_bytes().decode('utf-8').splitlines()
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    if len(matches) == 200:
                        more = True
                        break
                    matches.append({'path': q.relative_to(ws).as_posix(), 'line': i, 'text': line})
            if more:
                break
        r = {'matches': matches}
        if more:
            r['truncated'] = True
        return r
    raise ToolError('unknown tool ' + name)


def run_argv(ws, argv):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, cwd=ws, env=env, capture_output=True, timeout=30)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = -1, e.stdout or b'', e.stderr or b''
    except OSError as e:
        raise ToolError('could not start %s: %s' % (argv[0], e))
    return code, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace'), int((time.perf_counter() - t0) * 1000)


def command_allowed(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return 'argv must be a non-empty list of strings'
    if argv[0] == 'git':
        return None if len(argv) > 1 and argv[1] in GIT_OK else 'git is allowed only with status, diff, log or show'
    return None if argv[0] in ALLOW else 'command not allowed: ' + argv[0]


def shell_tool(ws, task, name, a):
    if name == 'run_tests':
        argv = json.loads(Path(task).read_text())['test_command']
        t0 = time.perf_counter()
        try:
            p = subprocess.run(argv, cwd=ws, env={k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            code, out = p.returncode, p.stdout
        except subprocess.TimeoutExpired as e:
            code, out = -1, e.stdout or b''
        return {'exit_code': code, 'passed': code == 0, 'output': out.decode('utf-8', 'replace')[-TAIL:],
                'duration_ms': int((time.perf_counter() - t0) * 1000)}
    if name == 'run_command':
        why = command_allowed(a.get('argv'))
        if why:
            raise ToolError(why)
        code, out, err, ms = run_argv(ws, a['argv'])
        return {'exit_code': code, 'stdout': out[-TAIL:], 'stderr': err[-TAIL:], 'duration_ms': ms}
    raise ToolError('unknown tool ' + name)


def serve(kind, ws, task):
    ws = Path(ws).resolve()
    tools = FILES if kind == 'files' else SHELL
    out = sys.stdout

    def send(m):
        out.write(json.dumps(m) + '\n')
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        m = json.loads(line)
        mid, method = m.get('id'), m.get('method')
        if mid is None:
            continue
        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': m['params'].get('protocolVersion', '2025-11-25'),
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'tinycoder-' + kind, 'version': '1'}}})
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [
                {'name': n, 'description': d, 'inputSchema': s} for n, d, s in tools]}})
        elif method == 'tools/call':
            name, args = m['params']['name'], m['params'].get('arguments') or {}
            try:
                r = files_tool(ws, name, args) if kind == 'files' else shell_tool(ws, task, name, args)
                res = {'content': [{'type': 'text', 'text': json.dumps(r)}], 'isError': False}
            except ToolError as e:
                res = {'content': [{'type': 'text', 'text': 'error: %s' % e}], 'isError': True}
            except (KeyError, TypeError) as e:
                res = {'content': [{'type': 'text', 'text': 'error: bad arguments: %s' % e}], 'isError': True}
            send({'jsonrpc': '2.0', 'id': mid, 'result': res})
        elif method == 'ping':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not found'}})


# ---------------- agent ----------------

class Conn:
    def __init__(self, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.n = 0
        self.req('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'tinycoder', 'version': '1'}})
        self.p.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')

    def req(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params}) + '\n')
        self.p.stdin.flush()
        while True:
            m = json.loads(self.p.stdout.readline())
            if m.get('id') == self.n:
                return m['result']


def hook(name, a):
    for k in ('path',):
        v = a.get(k)
        if isinstance(v, str) and (v.startswith('/') or '..' in Path(v).parts):
            return 'path must stay inside the workspace'
    if name == 'write_file' and len(a.get('content', '').encode('utf-8')) > 100000:
        return 'write_file content is over 100,000 bytes'
    if name == 'run_command':
        return command_allowed(a.get('argv'))
    return None


def call_api(body):
    base = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com').rstrip('/')
    data = json.dumps(body).encode()
    for attempt in range(4):
        rq = urllib.request.Request(base + '/v1/messages', data=data, method='POST', headers={
            'content-type': 'application/json', 'anthropic-version': '2026-10-21', 'x-api-key': os.environ.get('ANTHROPIC_API_KEY', '')})
        try:
            with urllib.request.urlopen(rq, timeout=600) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 529) or attempt == 3:
                raise RuntimeError('api error %d' % e.code)
        except (urllib.error.URLError, ConnectionError) as e:
            if attempt == 3:
                raise RuntimeError('connection error: %s' % e)
        time.sleep([1, 2, 4][attempt])


def agent(task_dir, ws, runs):
    t0 = time.perf_counter()
    run_id = os.environ.get('TINYCODER_RUN_ID') or str(int(time.time() * 1000))
    Path(runs).mkdir(parents=True, exist_ok=True)
    log = open(Path(runs) / (run_id + '.jsonl'), 'a', buffering=1)
    me = [sys.executable, str(HERE)]
    files = Conn(me + ['serve', 'files', '--workspace', ws])
    shell = Conn(me + ['serve', 'shell', '--workspace', ws, '--task', str(Path(task_dir) / 'task.json')])
    owner, tools = {}, []
    for c in (files, shell):
        for t in c.req('tools/list', {})['tools']:
            owner[t['name']] = c
            tools.append({'name': t['name'], 'description': t['description'], 'input_schema': t['inputSchema']})
    messages = [{'role': 'user', 'content': (Path(task_dir) / 'TASK.md').read_text()}]
    st = {'status': 'error', 'reason': None, 'steps': 0, 'tool_calls': 0, 'blocked_calls': 0, 'input_tokens': 0,
          'output_tokens': 0, 'llm_ms': 0.0, 'tool_ms': 0.0, 'final_text': ''}
    try:
        while True:
            if st['steps'] == 20:
                st['status'] = 'step_limit'
                break
            st['steps'] += 1
            a0 = time.perf_counter()
            r = call_api({'model': 'claude-sonnet-5-5', 'max_tokens': 16000, 'system': SYSTEM_FILE.read_text(),
                          'messages': messages, 'tools': tools, 'output_config': {'effort': 'medium'}})
            ms = (time.perf_counter() - a0) * 1000
            st['llm_ms'] += ms
            u = r.get('usage', {})
            st['input_tokens'] += u.get('input_tokens', 0)
            st['output_tokens'] += u.get('output_tokens', 0)
            log.write(json.dumps({'type': 'llm', 'step': st['steps'], 'duration_ms': round(ms, 3), 'input_tokens': u.get('input_tokens', 0),
                                  'output_tokens': u.get('output_tokens', 0), 'cache_read_input_tokens': u.get('cache_read_input_tokens', 0),
                                  'stop_reason': r['stop_reason']}) + '\n')
            messages.append({'role': 'assistant', 'content': r['content']})
            if r['stop_reason'] == 'end_turn':
                st['status'] = 'done'
                st['final_text'] = '\n'.join(b['text'] for b in r['content'] if b['type'] == 'text')
                break
            if r['stop_reason'] != 'tool_use':
                st['reason'] = r['stop_reason']
                break
            results = []
            for b in r['content']:
                if b['type'] != 'tool_use':
                    continue
                c0 = time.perf_counter()
                args = b.get('input') or {}
                why = hook(b['name'], args) if b['name'] in owner else 'unknown tool: ' + b['name']
                if why:
                    text, err, blocked = 'blocked by hook: ' + why, True, True
                    st['blocked_calls'] += 1
                else:
                    res = owner[b['name']].req('tools/call', {'name': b['name'], 'arguments': args})
                    text = ''.join(x.get('text', '') for x in res.get('content', []))
                    err, blocked = bool(res.get('isError')), False
                st['tool_calls'] += 1
                cms = (time.perf_counter() - c0) * 1000
                st['tool_ms'] += cms
                log.write(json.dumps({'type': 'tool', 'step': st['steps'], 'tool': b['name'], 'blocked': blocked, 'ok': not err,
                                      'duration_ms': round(cms, 3), 'args_bytes': len(json.dumps(args)), 'result_bytes': len(text.encode())}) + '\n')
                results.append({'type': 'tool_result', 'tool_use_id': b['id'], 'content': text, 'is_error': err})
            messages.append({'role': 'user', 'content': results})
    except RuntimeError as e:
        st['status'], st['reason'] = 'error', str(e)
    st['wall_ms'] = round((time.perf_counter() - t0) * 1000, 3)
    st['llm_ms'], st['tool_ms'] = round(st['llm_ms'], 3), round(st['tool_ms'], 3)
    final = {k: st[k] for k in ('status', 'reason', 'steps', 'tool_calls', 'blocked_calls', 'input_tokens', 'output_tokens',
                                'wall_ms', 'llm_ms', 'tool_ms', 'final_text')}
    log.write(json.dumps(dict(final, type='final')) + '\n')
    print(json.dumps(final), flush=True)
    for c in (files, shell):
        c.p.stdin.close()
        c.p.wait()
    return {'done': 0, 'step_limit': 2}.get(final['status'], 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve')
    s.add_argument('kind', choices=['files', 'shell'])
    s.add_argument('--workspace', required=True)
    s.add_argument('--task')
    r = sub.add_parser('run')
    r.add_argument('--task', required=True)
    r.add_argument('--workspace', required=True)
    r.add_argument('--runs', required=True)
    o = ap.parse_args()
    if o.cmd == 'serve':
        serve(o.kind, o.workspace, o.task)
        return 0
    return agent(o.task, o.workspace, o.runs)


if __name__ == '__main__':
    sys.exit(main())
