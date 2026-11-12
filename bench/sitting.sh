#!/bin/bash
# One official benchmark sitting (no model calls). From the repo root: bench/sitting.sh 1
cd "$(dirname "$0")/.." || exit 1
n=$1
case "$n" in 1) seed=20261111;; 2) seed=20261112;; *) echo 'usage: bench/sitting.sh 1|2'; exit 1;; esac
[ -e "results/raw/sitting-$n.jsonl" ] && { echo "sitting $n already has data; not starting"; exit 1; }
/usr/bin/python3 bench/freeze.py --check || exit 1
pmset -g batt | head -1 | grep -q 'AC Power' || { echo 'not on AC power; not starting'; exit 1; }
caffeinate -i /usr/bin/python3 bench/measure.py sitting-$n --rounds 5 --seed $seed || exit 1
echo "SITTING $n FINISHED $(date)"
