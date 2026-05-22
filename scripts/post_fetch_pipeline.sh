#!/usr/bin/env bash
# 2026 bulk fetch 完了後に走らせる連鎖 pipeline:
#   1. 馬柱から過去 race_id を harvest (Gen 1)
#   2. Gen 1 fetch
#   3. 馬柱から再 harvest (Gen 2)
#   4. Gen 2 fetch (小規模)
#   5. dataset 再構築 + train + backtest
#
# 使い方:
#   ./scripts/post_fetch_pipeline.sh
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "=== Step 1: harvest past race_ids (Gen 1) ==="
$PY -m src.harvest_past_rids --output data/cache/rids_gen1.txt

echo ""
echo "=== Step 2: fetch Gen 1 ==="
$PY -m src.bulk_fetch --since 20240101 --until 20261231 \
    --workers 5 --rids-file data/cache/rids_gen1.txt

echo ""
echo "=== Step 3: re-harvest (Gen 2) ==="
$PY -m src.harvest_past_rids --output data/cache/rids_gen2.txt

echo ""
echo "=== Step 4: fetch Gen 2 ==="
$PY -m src.bulk_fetch --since 20220101 --until 20261231 \
    --workers 5 --rids-file data/cache/rids_gen2.txt

echo ""
echo "=== Step 5: rebuild priors + dataset + train + backtest ==="
$PY -m src.priors
$PY -m src.dataset
$PY -m src.train --rounds 800 --lr 0.04
$PY -m src.backtest --rerun --reliability

echo ""
echo "=== ALL DONE ==="
