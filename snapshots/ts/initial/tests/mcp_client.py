"""A minimal MCP stdio client (JSON-RPC 2.0, one JSON message per line). Standard library only, Python 3.9+.

Used by the checks and benchmarks so they never depend on the SDK of the version under test.
"""
import json, os, queue, subprocess, threading, time

PROTOCOL = '2025-11-25'


class MCPError(Exception):
    pass


class Client:
    def __init__(self, argv, env=None, cwd=None):
        self.t_spawn = time.perf_counter()
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, cwd=cwd, bufsize=0)
        self.q = queue.Queue()
        self.next_id = 1
        self.stderr = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            line = line.strip()
            if line:
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    self.q.put({'_bad_line': line.decode('utf-8', 'replace')[:300]})
        self.q.put(None)

    def _err(self):
        for line in self.p.stderr:
            self.stderr.append(line.decode('utf-8', 'replace'))

    def send(self, msg):
        self.p.stdin.write((json.dumps(msg) + '\n').encode())
        self.p.stdin.flush()

    def request(self, method, params=None, timeout=30.0):
        rid = self.next_id
        self.next_id += 1
        self.send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}})
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                raise MCPError('timeout waiting for ' + method)
            try:
                m = self.q.get(timeout=left)
            except queue.Empty:
                raise MCPError('timeout waiting for ' + method)
            if m is None:
                raise MCPError('server exited during ' + method + ': ' + ''.join(self.stderr)[-500:])
            if '_bad_line' in m:
                raise MCPError('server wrote a non-JSON line to stdout: ' + m['_bad_line'])
            if m.get('id') == rid:
                if 'error' in m:
                    raise MCPError(method + ' error: ' + json.dumps(m['error'])[:300])
                return m.get('result')
            # notifications or server requests are ignored

    def initialize(self, timeout=30.0):
        r = self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                        'clientInfo': {'name': 'tc-check', 'version': '1'}}, timeout)
        self.t_ready = time.perf_counter()
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return r

    def tools(self):
        return self.request('tools/list')['tools']

    def call(self, name, args, timeout=60.0):
        """Returns (is_error, parsed_json_or_text). Success results must be one text block holding JSON."""
        r = self.request('tools/call', {'name': name, 'arguments': args}, timeout)
        content = r.get('content') or []
        texts = [c.get('text', '') for c in content if c.get('type') == 'text']
        text = texts[0] if len(texts) == 1 else None
        if r.get('isError'):
            return True, text if text is not None else texts
        if text is None:
            raise MCPError('%s: expected one text block, got %d' % (name, len(texts)))
        try:
            return False, json.loads(text)
        except ValueError:
            raise MCPError('%s: result text is not JSON: %s' % (name, text[:200]))

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


def clean_env(extra=None):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
    env.update(extra or {})
    return env
