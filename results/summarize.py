"""Recomputes every number the film uses from the raw files. Standard library only. From the repo root:

  /usr/bin/python3 results/summarize.py            reads results/sessions.json + results/raw + results/agent
  /usr/bin/python3 results/summarize.py --refresh  first rebuilds results/sessions.json from sessions/private (local only)

Writes results/summary.json. Medians throughout; ranges are the min and max of the per-round (or per-run) values.
"""
import glob, json, statistics as st, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R = ROOT / 'results'
LANES = ('py', 'ts', 'rs')
BUILDS = ['%s-%s' % (l, s) for l in LANES for s in ('initial', 'optimize')]


def md(xs):
    return round(st.median(xs), 3)


def rng(xs):
    return [round(min(xs), 3), round(max(xs), 3)]


def refresh_sessions():
    out = {}
    for f in sorted((ROOT / 'sessions/private').glob('*/*/usage.json')):
        lane, step = f.parent.parent.name, f.parent.name
        if lane not in LANES:
            continue
        u = json.loads(f.read_text())
        e = json.loads((f.parent / 'events.json').read_text())
        net = (f.parent / 'net.log').read_text().splitlines() if (f.parent / 'net.log').exists() else []
        allowed = sorted({l.split()[-1] for l in net if ' allow ' in l})
        out['%s-%s' % (lane, step)] = {'seconds': e['seconds'], 'end': e['end'], 'model': e['model'], 'effort': e['effort'],
                                       'api_calls': u['api_calls'], 'output_tokens': u['output_tokens'],
                                       'cache_read_input_tokens': u['cache_read_input_tokens'],
                                       'estimated_usd_api_prices': u['estimated_usd_api_prices'], 'network_allowed': allowed}
    (R / 'sessions.json').write_text(json.dumps(out, indent=1))


def sitting(name):
    recs = [json.loads(l) for l in (R / 'raw' / (name + '.jsonl')).read_text().splitlines()]
    meta = json.loads((R / 'raw' / (name + '.meta.json')).read_text())
    out = {'meta': {k: meta[k] for k in ('started', 'finished', 'rounds', 'seed', 'cpu', 'mem_bytes', 'power', 'load_before', 'load_after')}}
    for b in BUILDS:
        bd = next(r for r in recs if r['kind'] == 'build' and r['build'] == b)
        rs = [r for r in recs if r['kind'] == 'round' and r['build'] == b]
        per = lambda f: [f(r) for r in rs]
        x = {'rounds': len(rs), 'verify': bd['verify'], 'build_seconds': bd['seconds']['total'], 'source': bd['loc'],
             'deps': {k: v for k, v in bd['deps'].items()},
             'startup_ms': {k: md(per(lambda r: st.median(r['startup_ms'][k]))) for k in ('files', 'shell')},
             'startup_ms_range': {k: rng(per(lambda r: st.median(r['startup_ms'][k]))) for k in ('files', 'shell')},
             'call_us': {k: md(per(lambda r: st.median(r['call_us'][k]))) for k in rs[0]['call_us']},
             'server_rss_mb': {k: md(per(lambda r: r['server_rss_mb'][k])) for k in ('files', 'shell')},
             'agent_fake_20_steps_ms': md(per(lambda r: st.median(a['wall_ms'] for a in r['agent']))),
             'agent_fake_20_steps_ms_range': rng(per(lambda r: st.median(a['wall_ms'] for a in r['agent']))),
             'agent_process_rss_mb': md(per(lambda r: max(a['largest_process_rss_mb'] for a in r['agent'])))}
        out[b] = x
    return out


def agents():
    runs = [json.loads(Path(f).read_text()) for f in sorted(glob.glob(str(R / 'agent/*-*-*.json')))]
    out = {'runs': len(runs), 'cost_usd_total': round(sum(r['cost_usd_openrouter'] for r in runs), 4),
           'all_served_by': sorted({p for r in runs for p in r['providers']}),
           'http_non_200': sum(1 for r in runs for s in r['http_statuses'] if s != 200), 'by_lane': {}, 'by_lane_task': {}}

    def agg(sel):
        f = [r['final'] for r in sel]
        outside = [(x['wall_ms'] - x['llm_ms']) / 1000 for x in f]
        return {'n': len(sel), 'done': sum(x['status'] == 'done' for x in f), 'hidden_pass': sum(r['hidden_tests_pass'] for r in sel),
                'visible_pass': sum(r['visible_tests_pass'] for r in sel), 'steps': md([x['steps'] for x in f]),
                'steps_range': rng([x['steps'] for x in f]), 'blocked_calls': sum(x['blocked_calls'] for x in f),
                'wall_s': md([x['wall_ms'] / 1000 for x in f]), 'wall_s_range': rng([x['wall_ms'] / 1000 for x in f]),
                'llm_s': md([x['llm_ms'] / 1000 for x in f]), 'tool_ms': md([x['tool_ms'] for x in f]),
                'outside_model_s': md(outside), 'outside_model_s_range': rng(outside),
                'outside_model_share_pct': md([100 * (x['wall_ms'] - x['llm_ms']) / x['wall_ms'] for x in f]),
                'output_tokens': md([x['output_tokens'] for x in f]), 'cost_usd': md([r['cost_usd_openrouter'] for r in sel])}
    for l in LANES:
        out['by_lane'][l] = agg([r for r in runs if r['lane'] == l])
        for t in sorted({r['task'] for r in runs}):
            out['by_lane_task']['%s/%s' % (l, t)] = agg([r for r in runs if r['lane'] == l and r['task'] == t])
    return out


def main():
    if '--refresh' in sys.argv:
        refresh_sessions()
    sessions = json.loads((R / 'sessions.json').read_text())
    s = {'protocol_hash': json.loads((ROOT / 'bench/FROZEN.json').read_text())['protocol_hash'],
         'builder': 'Claude Code, claude-opus-5-5, effort high, 30-minute cap per step',
         'agent_model': 'claude-sonnet-5-5, effort medium, via OpenRouter pinned to Anthropic',
         'build_sessions': sessions,
         'fix_rounds': {l: sum(1 for k in sessions if k == '%s-repair' % l) for l in LANES},
         'build_cost_estimate_usd_by_lane': {l: round(sum(v['estimated_usd_api_prices'] for k, v in sessions.items() if k.startswith(l + '-')), 4) for l in LANES},
         'sittings': {n: sitting(n) for n in ('sitting-1', 'sitting-2')},
         'agent_runs': agents()}
    (R / 'summary.json').write_text(json.dumps(s, indent=1))
    print('wrote results/summary.json')


if __name__ == '__main__':
    main()
