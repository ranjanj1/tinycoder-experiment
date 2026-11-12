"""Static audit of one build: does it use its lane's official MCP SDK for servers and client, and no hand-written
protocol code? Python 3.9+, standard library only. A pass here plus a human read of the source = RULES.md rule 6.

  python3 bench/audit.py PY|TS|RS <workspace>
"""
import re, sys
from pathlib import Path

NEED = {
    'PY': (['*.py'], [r'from mcp\.server|import mcp\.server', r'mcp\.client\.stdio|from mcp import .*ClientSession|ClientSession', r'import anthropic|from anthropic']),
    'TS': (['*.ts'], [r"@modelcontextprotocol/sdk/server/", r"@modelcontextprotocol/sdk/client/", r"@anthropic-ai/sdk"]),
    'RS': (['*.rs'], [r'rmcp::', r'serve_server|ServerHandler|tool_router|#\[tool', r'TokioChildProcess|serve_client|\.serve\(']),
}
SKIP = ('node_modules', 'target', '.npm-cache', '.cargo-home', 'tests', '__pycache__', 'dist')


def main(lane, ws):
    ws = Path(ws)
    globs, needs = NEED[lane]
    files = [p for g in globs for p in ws.rglob(g) if not any(s in p.relative_to(ws).parts for s in SKIP)]
    text = '\n'.join(p.read_text(errors='replace') for p in files)
    problems = ['missing: ' + n for n in needs if not re.search(n, text)]
    hits = [str(p.relative_to(ws)) for p in files if re.search(r'["\']jsonrpc["\']\s*[:,]', p.read_text(errors='replace'))]
    problems += ['hand-written JSON-RPC in ' + h for h in hits]
    if lane == 'RS' and 'rmcp' not in (ws / 'Cargo.toml').read_text() if (ws / 'Cargo.toml').exists() else lane == 'RS':
        problems.append('Cargo.toml does not depend on rmcp')
    print('%s audit: %s (%d source files)' % (lane, 'OK' if not problems else '; '.join(problems), len(files)))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
