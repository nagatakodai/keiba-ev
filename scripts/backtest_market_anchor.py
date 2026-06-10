#!/usr/bin/env python3
"""市場アンカー型クロスプール EV (Dr.Z 系) の過去 snapshot バックテスト。

snapshot に保存済みの単勝オッズ (bet_tables.win) から市場アンカー確率
(de-vig → Discounted Harville) を再構成し、同じ snapshot の
  - 複勝 (bet_tables.place — 頭数 ≤16 なので完全)
  - 3連単 (rows — 全 triple 保存済みなので完全)
を px_o (アンカー確率 × 保存オッズ × DRIFT_SHADE) でスキャンして、閾値以上を
flat ¥100 で買った場合の仮想 ROI を出す。的中時の払戻は result の確定値
(place:N final odds / trifecta_payout) を優先し、無ければ保存オッズ。

⚠ 注意:
  - ワイド/馬連等は bet_tables が旧モデル px_o の top30 に切られており選択バイアス
    があるため対象外 (複勝と3連単のみ完全)。
  - 保存オッズは bet 段 (締切1-2.5分前) のもの。実弾では自票インパクトが加わる。

使い方:
    .venv/bin/python scripts/backtest_market_anchor.py
    .venv/bin/python scripts/backtest_market_anchor.py --thresholds 1.0,1.1,1.2,1.5
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RES_DIR = ROOT / "data" / "results"

sys.path.insert(0, str(ROOT))
from src.ev import DEFAULT_LAMBDA_2, DEFAULT_LAMBDA_3, power_method_overround  # noqa: E402
from src.portfolio import DRIFT_SHADE  # noqa: E402

JRA_CODES = {f"{i:02d}" for i in range(1, 11)}

# CLI で上書き可能な λ (main で設定)
_L2 = DEFAULT_LAMBDA_2
_L3 = DEFAULT_LAMBDA_3


def anchor_probs(win_rows):
    raw = {r["key"][0]: 1.0 / r["odds"] for r in win_rows if r.get("odds", 0) > 0}
    if len(raw) < 3:
        return None
    try:
        win = power_method_overround(raw)
    except Exception:
        s = sum(raw.values())
        win = {k: v / s for k, v in raw.items()}
    s = sum(win.values())
    if s <= 0:
        return None
    return {k: v / s for k, v in win.items()}


def place3_prob(win: dict[int, float], n: int) -> dict[int, float]:
    """P(top3) を Discounted Harville (λ2/λ3) の 3 着内 marginalize で近似。

    厳密 marginalize は O(N^3)。ここでは portfolio.enumerate_outcomes と同じ
    PL 連鎖で全 triple を回して per-horse の top3 確率を合算する (N≤18 で十分速い)。
    """
    horses = [h for h, p in win.items() if p > 0]
    pl2 = {h: win[h] ** _L2 for h in horses}
    pl3 = {h: win[h] ** _L3 for h in horses}
    t2, t3 = sum(pl2.values()), sum(pl3.values())
    out = defaultdict(float)
    for a in horses:
        p1 = win[a]
        db = t2 - pl2[a]
        if db <= 0:
            continue
        for b in horses:
            if b == a:
                continue
            p2 = pl2[b] / db
            dc = t3 - pl3[a] - pl3[b]
            if dc <= 0:
                continue
            p12 = p1 * p2
            for c in horses:
                if c in (a, b):
                    continue
                p = p12 * pl3[c] / dc
                out[a] += p
                out[b] += p
                out[c] += p
    return dict(out)


def trifecta_probs(win: dict[int, float]):
    horses = [h for h, p in win.items() if p > 0]
    pl2 = {h: win[h] ** _L2 for h in horses}
    pl3 = {h: win[h] ** _L3 for h in horses}
    t2, t3 = sum(pl2.values()), sum(pl3.values())
    out = {}
    for a in horses:
        db = t2 - pl2[a]
        if db <= 0:
            continue
        for b in horses:
            if b == a:
                continue
            dc = t3 - pl3[a] - pl3[b]
            if dc <= 0:
                continue
            p12 = win[a] * pl2[b] / db
            for c in horses:
                if c in (a, b):
                    continue
                out[(a, b, c)] = p12 * pl3[c] / dc
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="1.0,1.1,1.2,1.5")
    ap.add_argument("--since", default=None)
    ap.add_argument("--lambda2", type=float, default=DEFAULT_LAMBDA_2)
    ap.add_argument("--lambda3", type=float, default=DEFAULT_LAMBDA_3)
    args = ap.parse_args()
    global _L2, _L3
    _L2, _L3 = args.lambda2, args.lambda3
    thresholds = [float(x) for x in args.thresholds.split(",")]

    # agg[(bet_type, seg, thr)] = dict(n, stake, ret, hits)
    agg = defaultdict(lambda: dict(n=0, stake=0, ret=0.0, hits=0))
    n_races = 0
    for rf in sorted(glob.glob(str(RES_DIR / "*.json"))):
        rid = os.path.basename(rf)[:-5]
        pf = PRED_DIR / f"{rid}.json"
        if not pf.exists():
            continue
        r = json.load(open(rf))
        fo = r.get("finish_order") or []
        if len(fo) < 3:
            continue
        d = json.load(open(pf))
        # 日付フィルタは saved_at で行う (race_id 先頭8桁は年+場コードで日付ではない)。
        if args.since and (d.get("saved_at") or "")[:10].replace("-", "") < args.since:
            continue
        win_rows = (d.get("bet_tables") or {}).get("win") or []
        win = anchor_probs(win_rows)
        if not win:
            continue
        n_races += 1
        seg = "jra" if rid.split("-")[0][4:6] in JRA_CODES else "nar"
        final = r.get("final_odds") or {}
        top3 = set(fo[:3])

        # --- 複勝 ---
        # 出走頭数ルール (2026-06-11 bughunt 第4R): 7頭以下は複勝2着まで・4頭以下は
        # 発売なし。win_rows は実頭数 (≤18 で top-30 cap 非該当) なので長さで判定。
        n_run = len(win_rows)
        if n_run <= 4:
            paying_place: set[int] = set()
        elif n_run <= 7:
            paying_place = set(fo[:2])
        else:
            paying_place = top3
        place_rows = (d.get("bet_tables") or {}).get("place") or []
        if place_rows:
            p3 = place3_prob(win, len(win))
            sh = DRIFT_SHADE.get("place", 0.9)
            for row in place_rows:
                h = row["key"][0]
                odds = row.get("odds", 0)
                if odds <= 1.0 or h not in p3:
                    continue
                pxo = p3[h] * odds * sh
                for thr in thresholds:
                    if pxo < thr:
                        continue
                    a = agg[("place", seg, thr)]
                    a["n"] += 1
                    a["stake"] += 100
                    if h in paying_place:
                        a["hits"] += 1
                        f_odds = final.get(f"place:{h}") or odds
                        a["ret"] += f_odds * 100

        # --- 3連単 (rows = 全 triple) ---
        rows = d.get("rows") or []
        if rows:
            tp = trifecta_probs(win)
            sh = DRIFT_SHADE.get("trifecta", 0.85)
            tri_payout = r.get("trifecta_payout")
            for row in rows:
                key = tuple(row["key"])
                odds = row.get("odds", 0)
                if odds <= 1.0 or key not in tp:
                    continue
                pxo = tp[key] * odds * sh
                for thr in thresholds:
                    if pxo < thr:
                        continue
                    a = agg[("trifecta", seg, thr)]
                    a["n"] += 1
                    a["stake"] += 100
                    if key == tuple(fo[:3]):
                        a["hits"] += 1
                        a["ret"] += (tri_payout if tri_payout else odds * 100)

    print(f"races={n_races} (アンカー構成可能なもの)")
    print(f"{'type':9s} {'seg':4s} {'thr':>4s} {'bets':>6s} {'hits':>5s} {'stake':>10s} {'ret':>10s} {'ROI':>7s}")
    for (bt, seg, thr), a in sorted(agg.items()):
        roi = a["ret"] / a["stake"] * 100 if a["stake"] else 0
        print(f"{bt:9s} {seg:4s} {thr:4.1f} {a['n']:6d} {a['hits']:5d} "
              f"¥{a['stake']:>9,} ¥{a['ret']:>9,.0f} {roi:6.1f}%")


if __name__ == "__main__":
    main()
