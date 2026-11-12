#!/bin/bash
# All agent runs, 5 rounds; within a round every task, the three lanes in rotating order. Skips runs already done.
cd "$(dirname "$0")/.." || exit 1
lanes=(py ts rs)
for n in 1 2 3 4 5; do
  for t in t1-bugfix t2-feature t3-failing; do
    for k in 0 1 2; do
      l=${lanes[$(( (n + k) % 3 ))]}
      [ -e "results/agent/$l-$t-$n.json" ] && continue
      /usr/bin/python3 bench/agent_run.py "$l" "$t" "$n" || { echo "STOPPED at $l $t $n"; exit 1; }
    done
  done
done
echo "ALL AGENT RUNS FINISHED $(date)"
