"""全レースの past_runs から実データ駆動の par タイム表を作る (Beyer 式図表の基礎)。

現状の speed_index.py は基準タイムがハードコード ("Tier 0 暫定")。これを、手元の全 past_runs
(各馬の馬柱、数十万走) の実走破タイムから per-condition の中央値で置き換える。

出力 data/cache/par_times.json:
  {
    "winner_time": {"<surface>|<dist_bucket>|<venue>|<going>": {median, n}},
    "last3f":      {同キー: {median, n}},
  }
dist_bucket = 距離を 100m 単位に丸め。par は **過去 race の勝ち時計** (finish_pos==1) を
race_id でユニーク化して集計 (同じ過去 race を複数馬が持つ重複を除く)。

使い方: python scripts/build_par_table.py [--workers 8]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "cache" / "par_times.json"


def _one(rid: str):
    """1 race の全馬の past_runs から (key, winner_time, last3f) を、勝ち馬走 (finish_pos==1)
    に限り、過去 race_id でユニーク化して返す。"""
    from src.dataset import load_race
    try:
        loaded = load_race(rid)
    except Exception:
        return []
    if loaded is None:
        return []
    rd, _ = loaded
    out = {}
    for h in rd.race.horses:
        for pr in (h.past_runs or []):
            # 勝ち時計 = winner_time_sec (その過去 race の1着タイム)。finish_pos に依らず winner_time は共通。
            wt = getattr(pr, "winner_time_sec", 0) or 0
            if wt <= 0:
                continue
            surf = getattr(pr, "surface", "") or ""
            dist = getattr(pr, "distance", 0) or 0
            ven = getattr(pr, "venue", "") or ""
            going = getattr(pr, "going", "") or ""
            # speed_chart.par_lookup と同じ正規化 (稍重→稍 / 不良→不)。netkeiba 馬柱は
            # 1文字だが keibago/JRA 由来の PastRun が混ざっても語彙が割れないように。
            going = {"稍重": "稍", "不良": "不"}.get(going, going)
            if not surf or dist <= 0:
                continue
            bucket = int(round(dist / 100.0) * 100)
            key = f"{surf}|{bucket}|{ven}|{going}"
            prid = getattr(pr, "race_id", "") or f"{ven}|{getattr(pr,'date','')}|{getattr(pr,'race_no','')}"
            # 勝ち馬の last_3f を par_last3f に使う (finish_pos==1 の行のみ)
            l3 = None
            if getattr(pr, "finish_pos", None) == 1:
                l3 = getattr(pr, "last_3f_sec", 0) or 0
                if l3 <= 0:
                    l3 = None
            out[prid] = (key, float(wt), l3)
    return list(out.items())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    rids = sorted({p.name.split("-shutuba")[0] for p in RAW.glob("*-shutuba.html.gz")})
    print(f"scanning {len(rids):,} races for past_runs ...", flush=True)

    seen_prid: set[str] = set()
    wt_by_key: dict[str, list[float]] = defaultdict(list)
    l3_by_key: dict[str, list[float]] = defaultdict(list)
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for items in ex.map(_one, rids, chunksize=20):
            for prid, (key, wt, l3) in items:
                if prid in seen_prid:
                    continue
                seen_prid.add(prid)
                wt_by_key[key].append(wt)
                if l3 is not None:
                    l3_by_key[key].append(l3)

    winner_time = {k: {"median": float(np.median(v)), "n": len(v)}
                   for k, v in wt_by_key.items() if len(v) >= 3}
    last3f = {k: {"median": float(np.median(v)), "n": len(v)}
              for k, v in l3_by_key.items() if len(v) >= 3}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"winner_time": winner_time, "last3f": last3f},
                              ensure_ascii=False), encoding="utf-8")
    print(f"saved {OUT} — unique past races={len(seen_prid):,} "
          f"winner_time keys={len(winner_time):,} last3f keys={len(last3f):,}", flush=True)
    # サンプル
    for k in list(winner_time)[:5]:
        print(f"  {k}: par_wt={winner_time[k]['median']:.1f}s (n={winner_time[k]['n']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
