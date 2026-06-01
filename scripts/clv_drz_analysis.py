"""ライブ snapshot (bet 時オッズ) + 結果から CLV と Dr Z place/wide overlay を計測する。

研究メモ (data/research/profitable_betting.md) の検証2件:
  - CLV: 我々のバックテストは確定オッズ (settled) だが実際は締切~1分前に賭ける。
    snapshot の bet時 3連単オッズ vs 結果の確定3連単払戻 のドリフトを測る。
    大きければ「締切→確定で値が動く」= 確定オッズ前提のバックテストが楽観的。
  - Dr Z overlay: snapshot の bet_tables['place'/'wide'] は px_o (= モデルの place/wide EV)
    を持つ。px_o≥floor の +EV picks を実際に買ったら黒字か (= place プールの overlay を
    モデルが検出できているか) を測る。+EV (ROI>100%) なら build_bundle 配線候補。

データ: data/predictions/<rid>.json (bet時) + data/results/<rid>.json (finish_order, trifecta_payout)。
履歴 7000 は per-horse の place/exotic bet時オッズが無いのでライブ snapshot 限定 (N 小、傾向把握)。

使い方: python scripts/clv_drz_analysis.py
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRED = ROOT / "data" / "predictions"
RES = ROOT / "data" / "results"
JRA_CODES = {f"{i:02d}" for i in range(1, 11)}


def _seg(rid: str) -> str:
    # snapshot race_id は内部形式 (<cup>-<sched>-<rno>)。venue は market 由来で判定不能なので
    # cup_id 先頭の年 + venue を読むのは困難 → 結果の race_id 12桁があればそれで、無ければ unknown。
    return "?"


def _bootstrap_ci(vals, n=4000, seed=12345):
    vals = np.asarray(vals, float)
    if len(vals) == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(vals), size=(n, len(vals)))
    means = vals[idx].mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def _roi_ci(profit, stake, n=4000, seed=12345):
    profit, stake = np.asarray(profit, float), np.asarray(stake, float)
    if len(profit) == 0 or stake.sum() == 0:
        return 0.0, (0.0, 0.0)
    roi = profit.sum() / stake.sum() * 100
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(profit), size=(n, len(profit)))
    r = np.where(stake[idx].sum(1) > 0, profit[idx].sum(1) / stake[idx].sum(1) * 100, 0)
    return roi, (float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5)))


def _place_n(n_runners: int) -> int:
    """複勝の的中圏: 8頭以上=3着、5-7頭=2着、≤4頭=複勝なし(0)。"""
    if n_runners >= 8:
        return 3
    if n_runners >= 5:
        return 2
    return 0


def main() -> int:
    res_ids = {Path(f).stem for f in glob.glob(str(RES / "*.json"))}
    rows = []
    for f in glob.glob(str(PRED / "*.json")):
        if f.endswith(".llm.json"):
            continue
        rid = Path(f).stem
        if rid not in res_ids:
            continue
        try:
            d = json.loads(Path(f).read_text())
            rf = json.loads((RES / f"{rid}.json").read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ms = d.get("market_signals")
        fo = rf.get("finish_order")
        if not ms or not fo or len(fo) < 3:
            continue
        rows.append((rid, d, rf))

    print(f"=== ライブ snapshot CLV / Dr Z 計測 (N={len(rows)} races, bet時オッズ + 結果) ===\n")

    # ---------- CLV: 3連単 bet時オッズ vs 確定払戻 ----------
    drifts = []
    for rid, d, rf in rows:
        fo = rf["finish_order"][:3]
        pay = rf.get("trifecta_payout")
        if not pay:
            continue
        trip = fo
        hit = None
        for r in d.get("rows", []):
            if list(r.get("key", [])) == trip:
                hit = r
                break
        if hit and hit.get("odds", 0) > 0:
            final = pay / 100.0
            drifts.append((final - hit["odds"]) / hit["odds"])
    if drifts:
        dr = np.array(drifts)
        lo, hi = _bootstrap_ci(dr)
        print("[CLV] 3連単 bet時オッズ → 確定払戻 のドリフト (当たり目が rows にある race)")
        print(f"  n={len(dr)}  平均 {dr.mean()*100:+.1f}%  中央 {np.median(dr)*100:+.1f}%  "
              f"95%CI[{lo*100:+.1f},{hi*100:+.1f}]  確定>bet時の割合 {(dr>0).mean()*100:.0f}%")
        print(f"  → |ドリフト| 大 = 締切→確定で値が動く。平均が 0 近傍なら確定オッズ前提の")
        print(f"     バックテストは概ね妥当。系統的に負なら確定払戻は bet時より低め (楽観補正要)。\n")
    else:
        print("[CLV] 当たり3連単が rows に含まれる race が無く計測不能\n")

    # ---------- Dr Z: place / wide の +EV picks 実 ROI ----------
    for bt, floor in [("place", 1.02), ("wide", 1.02)]:
        prof, stake = [], []
        n_used = 0
        for rid, d, rf in rows:
            tbl = (d.get("bet_tables") or {}).get(bt)
            if not tbl:
                continue
            fo = set(rf["finish_order"][:3])
            n_run = len(d.get("market_signals") or [])
            pn = _place_n(n_run)
            if bt == "place" and pn == 0:
                continue
            top3 = set(rf["finish_order"][: (pn if bt == "place" else 3)])
            n_used += 1
            for r in tbl:
                if r.get("px_o", 0) < floor or r.get("odds", 0) <= 0:
                    continue
                key = r["key"]
                stake.append(100)
                if bt == "place":
                    won = key[0] in top3
                else:  # wide: 2頭とも3着以内
                    won = key[0] in fo and key[1] in fo
                prof.append(int(100 * r["odds"]) if won else 0)
        if stake:
            roi, (lo, hi) = _roi_ci(prof, stake)
            n_hit = int((np.array(prof) > 0).sum())
            flag = "  ← CI下限>100 = +EV候補" if lo > 100 else ""
            print(f"[Dr Z] {bt} px_o≥{floor} の +EV picks 実 ROI (bet時オッズで払戻近似)")
            print(f"  races_used={n_used}  bets={len(stake)}  hit={n_hit}  "
                  f"ROI {roi:.1f}%  95%CI[{lo:.1f},{hi:.1f}]{flag}")
        else:
            print(f"[Dr Z] {bt}: +EV picks が無く計測不能")
    print("\n注: N が小さくライブ snapshot 限定。ROI 95%CI 下限>100% の券種のみ build_bundle 配線候補。")
    print("    bet時オッズで払戻を近似 (複勝/ワイドは確定払戻が別途必要だが live result に無いため)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
