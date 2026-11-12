"""A fake Claude Messages API that replays scripted replies. Standard library only, Python 3.9+.

  python3 tests/fake_api.py --script s.json --log requests.jsonl [--port 0]

Prints `PORT <n>` on stdout once it is listening. Every request is appended to the log as one JSON line:
{"n": i, "path", "headers": {...lower-case...}, "body": {...}, "problems": [...]}. `problems` lists anything the request
does that SPEC.md section 4.1 forbids; the checks read them.

Script: {"replies": [reply, ...], "repeat_last": false}. A reply is either
  {"message": {"content": [...], "stop_reason": "...", "usage": {...}}}   -> 200 with a full message object
  {"status": 529, "error": "overloaded_error"}                          -> an API error
  optional "delay_ms" on either: wait before answering (simulated model time).
When the script runs out (and repeat_last is false) the server answers 500 and logs the problem "script exhausted".
"""
import argparse, json, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORBIDDEN = ('temperature', 'top_p', 'top_k', 'thinking', 'tool_choice', 'stream', 'stop_sequences')
ALLOWED = {'model', 'max_tokens', 'system', 'messages', 'tools', 'output_config', 'metadata'}


def check(headers, body):
    p = []
    if headers.get('anthropic-version') != '2026-10-21':
        p.append('anthropic-version is %r' % headers.get('anthropic-version'))
    if headers.get('x-api-key') != 'test-key' and headers.get('authorization') != 'Bearer test-key':
        p.append('api key not sent')
    if headers.get('anthropic-beta'):
        p.append('beta header sent: %s' % headers.get('anthropic-beta'))
    if body.get('model') != 'claude-sonnet-5-5':
        p.append('model is %r' % body.get('model'))
    if body.get('max_tokens') != 16000:
        p.append('max_tokens is %r' % body.get('max_tokens'))
    if body.get('output_config') != {'effort': 'medium'}:
        p.append('output_config is %r' % body.get('output_config'))
    for k in FORBIDDEN:
        if k in body:
            p.append('forbidden field: ' + k)
    for k in body:
        if k not in ALLOWED and k not in FORBIDDEN:
            p.append('unexpected field: ' + k)
    return p


def message(n, m):
    return {'id': 'msg_fake_%03d' % n, 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-5-5',
            'content': m['content'], 'stop_reason': m['stop_reason'], 'stop_sequence': None,
            'usage': dict({'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 0,
                           'cache_creation_input_tokens': 0}, **m.get('usage', {}))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--script', required=True)
    ap.add_argument('--log', required=True)
    ap.add_argument('--port', type=int, default=0)
    o = ap.parse_args()
    script = json.load(open(o.script))
    replies, repeat = script['replies'], script.get('repeat_last', False)
    lock, state = threading.Lock(), {'n': 0}
    log = open(o.log, 'a', buffering=1)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj, extra=None):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(data)))
            self.send_header('request-id', 'req_fake')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(404, {'type': 'error', 'error': {'type': 'not_found_error', 'message': 'fake api: POST /v1/messages only'}})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('content-length') or 0))
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                body = {'_unparsed': raw.decode('utf-8', 'replace')[:500]}
            with lock:
                n = state['n']
                state['n'] += 1
            problems = [] if self.path.split('?')[0] == '/v1/messages' else ['path is ' + self.path]
            problems += check(headers, body)
            if n < len(replies):
                r = replies[n]
            elif repeat and replies:
                r = replies[-1]
            else:
                r = None
                problems.append('script exhausted')
            keep = {k: headers[k] for k in ('anthropic-version', 'anthropic-beta', 'content-type', 'user-agent') if k in headers}
            keep['has_key'] = 'x-api-key' in headers or 'authorization' in headers
            log.write(json.dumps({'n': n, 't': time.time(), 'path': self.path, 'headers': keep, 'body': body, 'problems': problems}) + '\n')
            if r and r.get('delay_ms'):
                time.sleep(r['delay_ms'] / 1000.0)
            if r is None:
                return self._send(500, {'type': 'error', 'error': {'type': 'api_error', 'message': 'fake api: script exhausted'}})
            if 'status' in r:
                return self._send(r['status'], {'type': 'error', 'error': {'type': r.get('error', 'api_error'), 'message': 'fake api error'}},
                                  {'retry-after': '0', 'x-should-retry': 'true'} if r['status'] in (429, 500, 502, 503, 529) else None)
            self._send(200, message(n, r['message']))

    srv = ThreadingHTTPServer(('127.0.0.1', o.port), H)
    print('PORT %d' % srv.server_address[1], flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
