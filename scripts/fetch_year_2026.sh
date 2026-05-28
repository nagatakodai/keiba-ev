#!/usr/bin/env bash
# 2026 年分 全レース 保守的フルスクレイプ → フルパイプライン (one-shot, resumable)。
#
# 設計:
#   - bulk_fetch は resumable (既存 raw は skip)、連続 block 時 cooldown で自動 backoff
#   - workers=2 / polite=2000ms で netkeiba CloudFront 400 (IP block) を踏みにくくする
#   - set -e は使わない: fetch は成功保存済みなので、後段の解析が失敗しても切り分けられるよう
#     各 step の exit code をログに残す
#
# 使い方: nohup ./scripts/fetch_year_2026.sh >> data/cache/full_year_run.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

step() { echo ""; echo "===== [$(date '+%F %T')] $* ====="; }
rc_of() { echo "  -> exit code $1"; }

step "Step 0: enumerate recent gap 20260522-20260528 (JRA+NAR)"
$PY -m src.bulk_fetch --since 20260522 --until 20260528 --enum-only; rc_of $?

step "Step 1: build full-year rids list (union existing + gap)"
cat data/cache/rids_20260101_20260521.txt \
    data/cache/rids_20260522_20260525.txt \
    data/cache/rids_20260522_20260528.txt 2>/dev/null \
  | sed '/^$/d' | sort -u > data/cache/rids_year_2026.txt
echo "  full-year rids: $(wc -l < data/cache/rids_year_2026.txt)"

step "Step 2: conservative fetch (workers=2 polite=2000ms, resumable)"
$PY -m src.bulk_fetch --since 20260101 --until 20260528 \
    --workers 2 --polite-ms 2000 \
    --rids-file data/cache/rids_year_2026.txt; rc_of $?

step "Step 3: rebuild priors"
$PY -m src.priors; rc_of $?

step "Step 4: rebuild dataset (all.parquet)"
$PY -m src.dataset; rc_of $?

step "Step 5: train LightGBM (tuned defaults)"
$PY -m src.train --lr 0.03 --rounds 800 --leaves 24 --early-stop 100; rc_of $?

step "Step 6: backtest + reliability"
$PY -m src.backtest --reliability; rc_of $?

step "Step 7: holdout eval (β sweep / 単勝 ROI / market 比較)"
$PY -m src.eval_holdout --valid-frac 0.2; rc_of $?

step "ALL DONE"
