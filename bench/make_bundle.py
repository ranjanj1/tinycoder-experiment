"""Builds the starting folder for one lane: sessions/private/bundle-<lane>/. Only these files reach a session.

  python3 bench/make_bundle.py PY|TS|RS        (or: all)

Nothing from bench/ (hidden checks, task repos, reference) is ever copied. The bundle is listed with hashes in
bundle-<lane>.sha256 so run_steps can confirm it matches the frozen files.
"""
import hashlib, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = Path('/Users/Shared/tc-kit')
COMMON = ['RULES.md', 'SPEC.md', 'INTERFACE.md', 'CLAUDE.md', 'shared/system.txt', 'tests/check.py', 'tests/fake_api.py',
          'tests/mcp_client.py', 'tests/bench_dev.py']


def build(lane):
    out = ROOT / 'sessions' / 'private' / ('bundle-' + lane.lower())
    if out.exists():
        shutil.rmtree(out)
    for rel in COMMON:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, out / rel)
    shutil.copytree(ROOT / 'tests' / 'tasks', out / 'tests' / 'tasks')
    if lane == 'TS':
        shutil.copytree(KIT / 'npm-cache', out / '.npm-cache', ignore=shutil.ignore_patterns('_logs'))
    if lane == 'RS':
        (out / '.cargo').mkdir()
        shutil.copy2(KIT / 'rs-vendor-config.toml', out / '.cargo' / 'config.toml')
    leaks = [p for p in out.rglob('*') if 'bench/' in p.relative_to(out).as_posix() or p.name in ('validate.py', 'solution')]
    assert not leaks, leaks
    hashes = ''.join('%s  %s\n' % (hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(out).as_posix())
                     for p in sorted(out.rglob('*')) if p.is_file() and '.npm-cache' not in p.parts)
    (out.parent / ('bundle-%s.sha256' % lane.lower())).write_text(hashes)
    print(lane, 'bundle:', sum(1 for p in out.rglob('*') if p.is_file()), 'files ->', out)


if __name__ == '__main__':
    for lane in (['PY', 'TS', 'RS'] if sys.argv[1] == 'all' else [sys.argv[1]]):
        build(lane)
