"""Validation set 291 races の 3 連単オッズを scrape して JSON にキャッシュ。

`src/eval_holdout.py` の Plan A/B/C 真 ROI 評価のために、chronological split
last 20% の trifecta odds を網羅取得する。

設計:
  - 1 browser instance を全 races で再利用 (race ごと起動の 3 秒/回オーバーヘッドを節約)
  - 既存キャッシュ skip
  - 1 race ごとに JSON 出力 (途中中断しても再開可)
  - 進捗 + ETA を逐次表示

使い方:
  python scripts/fetch_trifecta_odds_holdout.py            # 全件
  python scripts/fetch_trifecta_odds_holdout.py --limit 3  # 動作確認 3 件
  python scripts/fetch_trifecta_odds_holdout.py --settle-ms 600  # 高速化試験

出力:
  data/cache/trifecta_odds/{race_id}.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from src.parse import parse_trifecta_multi  # noqa: E402
from src.scrape import UA, _is_empty_block_html, odds_get_form_url  # noqa: E402

CACHE_DIR = ROOT / "data" / "cache" / "trifecta_odds"


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def get_holdout_races(valid_frac: float = 0.2) -> list[tuple[str, int]]:
    df = pd.read_parquet(ROOT / "data" / "datasets" / "all.parquet")
    rids = df["race_id"].unique().tolist()
    rids.sort(key=_race_id_to_int)
    n_valid = max(int(len(rids) * valid_frac), 1)
    valid_rids = rids[-n_valid:]
    df_v = df[df["race_id"].isin(valid_rids)]
    has_result = (
        df_v.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )
    df_v = df_v[df_v["race_id"].isin(has_result)]
    out: list[tuple[str, int]] = []
    rids_set = set(df_v["race_id"])
    for rid in valid_rids:
        if rid not in rids_set:
            continue
        n_h = int(df_v[df_v["race_id"] == rid]["n_horses"].iloc[0])
        out.append((rid, n_h))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--settle-ms", type=int, default=800)
    ap.add_argument("--timeout-ms", type=int, default=60_000)
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    races = get_holdout_races()
    if args.limit:
        races = races[: args.limit]
    print(f"target races: {len(races)}", flush=True)

    t0 = time.time()
    n_done = 0
    n_skip = 0
    n_err = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="ja-JP",
            viewport={"width": 1280, "height": 1800},
        )
        page = ctx.new_page()

        for i, (rid, n_h) in enumerate(races, 1):
            out = CACHE_DIR / f"{rid}.json"
            if out.exists():
                n_skip += 1
                continue
            try:
                htmls: list[str] = []
                blocked = False
                # 注意 (2026-06-11): jiku は軸馬番。n_h (頭数) で回すと取消レースで
                # 馬番 > n_h の出走馬の1着オッズを取りこぼす。holdout dataset は頭数しか
                # 持たないため、保守的に「頭数+2」まで余分に巡回する (存在しない jiku の
                # ページは組ゼロで無害、取消が3頭以上のレースは稀)。
                for jiku in range(1, min(n_h + 3, 19)):
                    url = odds_get_form_url(rid, "b8", jiku=jiku)
                    page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                    page.wait_for_timeout(args.settle_ms)
                    html = page.content()
                    # CloudFront 400 検出 — 1 jiku でも block されたら race ごと skip
                    if _is_empty_block_html(html):
                        blocked = True
                        break
                    htmls.append(html)
                if blocked:
                    raise RuntimeError(f"netkeiba blocked at jiku iteration (CloudFront 400)")
                triplets = parse_trifecta_multi(htmls)
                data = {
                    "race_id": rid,
                    "n_horses": n_h,
                    "trifecta": [
                        {"key": list(t.key), "odds": t.odds, "popularity": t.popularity}
                        for t in triplets
                    ],
                }
                out.write_text(json.dumps(data), encoding="utf-8")
                n_done += 1
            except Exception as e:
                print(f"  ERROR {rid} (n_h={n_h}): {type(e).__name__}: {e}", flush=True)
                n_err += 1

            elapsed = time.time() - t0
            done_count = n_done + n_skip + n_err
            if n_done > 0:
                avg = elapsed / max(n_done, 1)
                remaining = len(races) - done_count
                eta_min = avg * remaining / 60
                print(
                    f"  [{done_count}/{len(races)}] rid={rid} n_h={n_h} "
                    f"avg={avg:.1f}s/race eta={eta_min:.1f}min "
                    f"(done={n_done} skip={n_skip} err={n_err})",
                    flush=True,
                )

        browser.close()

    total_min = (time.time() - t0) / 60
    print(
        f"finished: {n_done} fetched, {n_skip} cached, {n_err} errors. "
        f"total {total_min:.1f}min",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
