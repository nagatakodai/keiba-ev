"""v2 速度図表 (実データ par + pace + trip) を全 (race, horse) で算出して parquet 化。

研究の「本物の edge = 市場に直交する情報 = 公衆の粗い図表が見落とす補正」を実装する第一歩。
現状の speed_index は par がハードコード・pace/trip 無し。v2 は:
  - speed_v2: 実データ駆動 par (data/cache/par_times.json) からの走破タイム差 (秒)。
  - pace_v2: 上がり3F を condition 別 par_last3f と比較した「終い脚」(速い終い=+)。
  - trip:    通過順 (passing) から前後位置・位置取り変化 (= タイムに出ない不利/利)。
これらは win_odds に織り込まれにくい直交情報の候補。leakage 防止: past_runs は構造的に
対象 race 以前のみ (馬柱)。

出力 data/datasets/v2_features.parquet: (race_id, horse_number, speed_v2_wavg, speed_v2_best,
  pace_v2_wavg, trip_gain_avg, front_rate, v2_n_runs)。test_speed_v2.py が all.parquet に
merge して β-MLE で「市場超え (β<1.0)」を検証する。

使い方: python scripts/build_v2_features.py [--workers 8]
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 図表計算は src/speed_chart.py に集約 (live fundamental と同一式)。
from src.speed_chart import horse_figures  # noqa: E402

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "datasets" / "v2_features.parquet"


def _one(rid: str):
    from src.dataset import load_race
    try:
        loaded = load_race(rid)
    except Exception:
        return []
    if loaded is None:
        return []
    rd, _ = loaded
    rows = []
    for h in rd.race.horses:
        if h.absent:
            continue
        # 馬柱 = 既に対象 race 以前 (新しい順) → 集約図表を 1 行に
        rows.append({"race_id": rid, "horse_number": h.number,
                     **horse_figures(h.past_runs)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    rids = sorted({p.name.split("-shutuba")[0] for p in RAW.glob("*-shutuba.html.gz")})
    print(f"computing v2 features for {len(rids):,} races ...", flush=True)
    all_rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for rows in ex.map(_one, rids, chunksize=20):
            all_rows.extend(rows)
    df = pd.DataFrame(all_rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"saved {OUT} — rows={len(df):,}", flush=True)
    print(df[["speed_v2_wavg", "speed_v2_best", "pace_v2_wavg", "trip_gain_avg",
              "front_rate", "v2_n_runs"]].describe().round(2).to_string(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
