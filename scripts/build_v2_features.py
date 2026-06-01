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
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW = ROOT / "data" / "raw"
PAR = ROOT / "data" / "cache" / "par_times.json"
OUT = ROOT / "data" / "datasets" / "v2_features.parquet"

_PAR = json.loads(PAR.read_text())
PTS_PER_SEC = 10.0   # 1 秒 = 10 指数点 (Beyer 流のスケール感)
WEIGHTS = (0.5, 0.3, 0.2)


def _bucket(dist: int) -> int:
    return int(round(dist / 100.0) * 100)


def _par(table_key: str, surf: str, dist: int, ven: str, going: str):
    """par lookup: 厳密キー → 馬場落とす → 場落とす → 両方落とす のフォールバック。"""
    tab = _PAR.get(table_key, {})
    b = _bucket(dist)
    for k in (f"{surf}|{b}|{ven}|{going}", f"{surf}|{b}|{ven}|", f"{surf}|{b}||{going}", f"{surf}|{b}||"):
        # 末尾空フィールドの厳密キーは無いので、ven/going を空にした集約は別途取れない →
        # 厳密キーが無ければ「同 surface|bucket の全キー」を median する。
        if k in tab:
            return tab[k]["median"]
    # 集約フォールバック: surface|bucket で始まる全キーの median
    vals = [v["median"] for kk, v in tab.items() if kk.startswith(f"{surf}|{b}|")]
    return float(np.median(vals)) if vals else None


def _run_figs(pr) -> dict | None:
    surf = getattr(pr, "surface", "") or ""
    dist = getattr(pr, "distance", 0) or 0
    if not surf or dist <= 0:
        return None
    ven = getattr(pr, "venue", "") or ""
    going = getattr(pr, "going", "") or ""
    wt = getattr(pr, "winner_time_sec", 0) or 0
    diff = getattr(pr, "time_diff_sec", 0) or 0
    par_wt = _par("winner_time", surf, dist, ven, going)
    out = {}
    if wt > 0 and par_wt:
        own = wt + diff   # 自走破タイム = 勝ち時計 + 着差(秒)
        out["speed_v2"] = (par_wt - own) * PTS_PER_SEC
    # pace: 上がり3F vs par_last3f
    l3 = getattr(pr, "last_3f_sec", 0) or 0
    par_l3 = _par("last3f", surf, dist, ven, going)
    if l3 > 0 and par_l3:
        out["pace_v2"] = (par_l3 - l3) * PTS_PER_SEC
    # trip: 通過順 "a-b-c-d" → 前後位置と位置取り変化
    passing = getattr(pr, "passing", "") or ""
    nums = [int(x) for x in passing.replace("-", " ").split() if x.isdigit()]
    fs = getattr(pr, "field_size", 0) or 0
    if nums and fs > 0:
        early = nums[0]
        late = nums[-1]
        out["front"] = 1.0 if early <= 2 else 0.0
        out["gain"] = (early - late) / fs   # 正 = 位置を上げた (差し)
    return out or None


def _wavg(vals: list[float]) -> float:
    if not vals:
        return 0.0
    v = vals[:3]
    w = WEIGHTS[:len(v)]
    return float(sum(a * b for a, b in zip(v, w)) / sum(w))


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
        runs = h.past_runs or []      # 馬柱 = 既に対象 race 以前 (新しい順)
        sp, pc, gains, fronts = [], [], [], []
        for pr in runs:
            f = _run_figs(pr)
            if not f:
                continue
            if "speed_v2" in f:
                sp.append(f["speed_v2"])
            if "pace_v2" in f:
                pc.append(f["pace_v2"])
            if "gain" in f:
                gains.append(f["gain"]); fronts.append(f["front"])
        rows.append({
            "race_id": rid,
            "horse_number": h.number,
            "speed_v2_wavg": _wavg(sp),
            "speed_v2_best": float(max(sp)) if sp else 0.0,
            "pace_v2_wavg": _wavg(pc),
            "trip_gain_avg": float(np.mean(gains)) if gains else 0.0,
            "front_rate": float(np.mean(fronts)) if fronts else 0.0,
            "v2_n_runs": len(sp),
        })
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
