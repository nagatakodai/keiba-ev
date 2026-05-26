#!/usr/bin/env bash
# netkeiba 規制の自動解除待ち → 解除検出で今年分フルスクレイプを再開する watcher。
#
# 30 分ごとに netkeiba race_list を 1 回だけ probe (= 規制を悪化させない低頻度)。
# 空 body (CloudFront 400) でなくなったら fetch_year_2026.sh を実行して終了する。
# fetch は resumable なので取得済み 2,486 races は skip され、残りだけ取得 →
# そのまま priors→dataset→train→backtest→holdout まで自動で流れる。
#
# 使い方: nohup ./scripts/resume_on_unblock.sh >> data/cache/resume_on_unblock.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
INTERVAL=1800   # 30 分

probe() {
  "$PY" - <<'PYEOF'
from datetime import datetime
from src.scrape import fetch_html, race_list_url, NetkeibaBlocked
ok = False
for nar in (False, True):
    try:
        fetch_html(race_list_url(datetime.now().strftime("%Y%m%d"), nar=nar), timeout_ms=30000)
        ok = True
        break
    except NetkeibaBlocked:
        continue
    except Exception:
        continue
print("OK" if ok else "BLOCKED")
PYEOF
}

echo "[$(date '+%F %T')] resume-watcher 起動 (netkeiba 解除を ${INTERVAL}s 間隔で監視)"
while true; do
  st=$(probe)
  echo "[$(date '+%F %T')] netkeiba probe: $st"
  if [ "$st" = "OK" ]; then
    echo "[$(date '+%F %T')] netkeiba 解除を検出 → fetch_year_2026.sh を再開"
    ./scripts/fetch_year_2026.sh >> data/cache/full_year_run.log 2>&1
    echo "[$(date '+%F %T')] フルスクレイプ+パイプライン完了。watcher 終了。"
    break
  fi
  sleep "$INTERVAL"
done
