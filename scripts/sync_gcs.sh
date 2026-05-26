#!/usr/bin/env bash
# data/ をローカル保持したまま GCS にミラーする (incremental rsync, 冪等)。
#
# 設計:
#   - gsutil rsync は -d を付けないので「追加・更新のみ・GCS 側を削除しない」安全な
#     ミラー (ローカルでファイルを消しても GCS のバックアップは残る)。
#   - サイズ/mtime/md5 比較で変更分だけ転送 → 何度走らせても差分のみ。
#   - data/cache の *.log (実行ログ) は価値が無いので除外。raw HTML / parquet /
#     snapshots / calibration / odds・aptitude・rids cache はミラー対象。
#   - バケットは東京リージョン STANDARD (asia-northeast1)、別途 gcloud で作成済:
#       gs://keiba-race-data-788  (レースデータ: 過去レース + 今後解析する分。"788" は keiba シグネチャ)
#
# 使い方: ./scripts/sync_gcs.sh            (手動)
#         fetch_year_2026.sh の最終 step から自動呼び出し
set -uo pipefail
cd "$(dirname "$0")/.."
BUCKET="${KEIBA_GCS_BUCKET:-gs://keiba-race-data-788}"
GS="gsutil -m -q"

echo "[$(date '+%F %T')] GCS sync 開始 → $BUCKET"
$GS rsync -r              data/raw         "$BUCKET/raw"         || echo "  ! raw sync 失敗"
$GS rsync -r              data/datasets    "$BUCKET/datasets"    || echo "  ! datasets sync 失敗"
$GS rsync -r              data/predictions "$BUCKET/predictions" || echo "  ! predictions sync 失敗"
$GS rsync -r              data/results     "$BUCKET/results"     || echo "  ! results sync 失敗"
$GS rsync -r -x '.*\.log$' data/cache      "$BUCKET/cache"       || echo "  ! cache sync 失敗"
echo "[$(date '+%F %T')] GCS sync 完了 → $BUCKET"
