"""Local relay: tinycoder (any lane, unchanged) -> OpenRouter's Anthropic Messages endpoint, pinned to Anthropic.

  /usr/bin/python3 tools/openrouter_relay.py --port 18480 --log <dir>

The same for every lane. It changes only what OpenRouter needs and logs what it changed:
  - x-api-key (tinycoder sends a dummy) is dropped; Authorization: Bearer <OPENROUTER_API_KEY> is added.
    The key comes from OPENROUTER_API_KEY (environment or the repo's git-ignored .env) and is never logged or printed.
  - model "claude-sonnet-5-5" becomes "anthropic/claude-sonnet-5.5".
  - provider {"only": ["anthropic"], "allow_fallbacks": false} is added, so every call is served by Anthropic.
Everything else (anthropic-version header, output_config, messages, tools, thinking blocks) passes through untouched.
The upstream status and body go back unchanged, so tinycoder's own retry logic sees what Anthropic returned.
Each exchange is written to <log>/calls.jsonl (request and response bodies in full, no headers with secrets).
"""
import os, argparse, json, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM = 'https://openrouter.ai/api/v1/messages'
MODEL_MAP = {'claude-sonnet-5-5': 'anthropic/claude-sonnet-5.5'}
PROVIDER = {'only': ['anthropic'], 'allow_fallbacks': False}
PASS_HEADERS = ('anthropic-version', 'anthropic-beta', 'content-type')


def key():
    """OPENROUTER_API_KEY from the environment, else from a .env file in the repo root (git-ignored)."""
    if os.environ.get('OPENROUTER_API_KEY'):
        return os.environ['OPENROUTER_API_KEY']
    env = Path(__file__).resolve().parents[1] / '.env'
    for line in (env.read_text().splitlines() if env.exists() else []):
        if line.startswith('OPENROUTER_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"\'')
    raise SystemExit('set OPENROUTER_API_KEY (environment or .env in the repo root)')


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_POST(self):
        t0 = time.time()
        body = self.rfile.read(int(self.headers.get('content-length') or 0))
        try:
            req = json.loads(body)
        except ValueError:
            return self.reply(400, b'{"type":"error","error":{"type":"invalid_request_error","message":"relay: bad JSON"}}')
        sent_model = req.get('model')
        req['model'] = MODEL_MAP.get(sent_model, sent_model)
        req['provider'] = PROVIDER
        hdr = {k: self.headers[k] for k in PASS_HEADERS if self.headers.get(k)}
        hdr['Authorization'] = 'Bearer ' + self.server.key
        hdr['content-type'] = 'application/json'
        status, out, up_hdr = 0, b'', {}
        try:
            r = urllib.request.urlopen(urllib.request.Request(UPSTREAM, json.dumps(req).encode(), hdr, method='POST'), timeout=600)
            status, out, up_hdr = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, out, up_hdr = e.code, e.read(), dict(e.headers)
        except Exception as e:  # connection problem: 502, which tinycoder retries
            status, out = 502, json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': 'relay: %s' % e}}).encode()
        try:
            resp = json.loads(out)
        except ValueError:
            resp = {'_raw': out[:2000].decode('utf-8', 'replace')}
        rec = {'t': round(t0, 3), 'ms': round((time.time() - t0) * 1000, 1), 'status': status,
               'sent_model': sent_model, 'anthropic_version': self.headers.get('anthropic-version'),
               'request': {k: v for k, v in req.items()}, 'response': resp,
               'upstream_request_id': up_hdr.get('x-request-id') or up_hdr.get('X-Request-Id')}
        with self.server.lock_path.open('a') as f:
            f.write(json.dumps(rec) + '\n')
        self.reply(status, out)

    def reply(self, status, out):
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=18480)
    ap.add_argument('--log', required=True)
    o = ap.parse_args()
    s = ThreadingHTTPServer(('127.0.0.1', o.port), H)
    s.key = key()
    Path(o.log).mkdir(parents=True, exist_ok=True)
    s.lock_path = Path(o.log) / 'calls.jsonl'
    print('RELAY', o.port, flush=True)
    s.serve_forever()


if __name__ == '__main__':
    main()
