#!/usr/bin/env python3
"""頭数に対する Claude 指数上位 N 頭 3連単 BOX の最適 N をバックテストで探索する。

ダッシュボードの「勝負レース仮想収支」は Claude 指数上位 N 頭の3連単 BOX を買ったと仮定して
収支を測る (api/store._shobu_box_pnl)。N は `_shobu_box_size(出走頭数)` で決まる (現状: 8頭以上=5,
7頭=4, 少頭数は最低3頭を場外に残す)。本スクリプトは shobu 評価レース (= 当日スキャン母集団) を
**出走頭数でバケット**し、各バケットで N=3..8 を sweep して ROI / 的中率 / bootstrap CI を出し、
**頭数別の最適 N** を提示する。さらに固定 N ルールと現行 `_shobu_box_size` ルールを全数で比較する。

読み取り専用 (data/cache/shobu/*.json + data/predictions + data/results)。scrape 不要。

    .venv/bin/python scripts/optimal_box_n.py [--recommended] [--point 100] [--min-n 3 --max-n 8]
"""
from __future__ import annotations

import argparse
import json
from itertools import permutations

from api.store import (
    PRED_DIR,
    RESULT_DIR,
    _bootstrap_roi_ci,
    _claude_index_by_number,
    _safe_race_id,
    _shobu_box_size,
    _shobu_eval_races,
)


def _load_races(recommended_only: bool) -> list[dict]:
    """shobu 評価レースから (claude ranking, n_runners, finish, trifecta_payout) を集める。"""
    out: list[dict] = []
    by_race = _shobu_eval_races(recommended_only)
    for rid, race in by_race.items():
        safe = _safe_race_id(rid)
        if safe is None:
            continue
        snap_path = PRED_DIR / f"{safe}.json"
        res_path = RESULT_DIR / f"{safe}.json"
        if not snap_path.exists() or not res_path.exists():
            continue
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            result = json.loads(res_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        idx = _claude_index_by_number(snap)
        if len(idx) < 3:
            continue
        finish = [x for x in (result.get("finish_order") or [])[:3]
                  if isinstance(x, int) and x > 0]
        if len(finish) < 3:
            continue
        tri = int(result.get("trifecta_payout") or 0)
        if tri <= 0:
            continue
        ranked = [n for n, _ci in sorted(idx.items(), key=lambda kv: kv[1], reverse=True)]
        n_runners = snap.get("n_runners") or race.get("n_runners") or len(idx)
        out.append({
            "race_id": rid,
            "ranked": ranked,
            "n_runners": int(n_runners),
            "n_ranked": len(ranked),
            "finish": finish,
            "trifecta_payout": tri,
        })
    return out


def _race_box(r: dict, box_n: int, point: int) -> tuple[int, int, bool] | None:
    """1 レースを上位 box_n 頭 BOX で買ったときの (stake, payout, hit)。box 不能なら None。"""
    # BOX は出走頭数・ランク数を超えられない。
    box = min(box_n, r["n_ranked"], r["n_runners"])
    if box < 3:
        return None
    top = set(r["ranked"][:box])
    n_points = len(list(permutations(range(box), 3)))   # P(box,3)
    stake = n_points * point
    hit = all(f in top for f in r["finish"])
    payout = int(round(r["trifecta_payout"] * point / 100.0)) if hit else 0
    return stake, payout, hit


def _agg(per_race: list[tuple[int, int, bool]]) -> dict:
    n = len(per_race)
    stake = sum(s for s, _p, _h in per_race)
    payout = sum(p for _s, p, _h in per_race)
    hits = sum(1 for _s, _p, h in per_race if h)
    roi = payout / stake if stake else 0.0
    lo, hi = _bootstrap_roi_ci([(s, p) for s, p, _h in per_race])
    return {"n": n, "hits": hits, "hit_rate": hits / n if n else 0.0,
            "stake": stake, "payout": payout, "roi": roi, "roi_lo": lo, "roi_hi": hi}


# 頭数バケット (サンプルを稼ぐため): ラベル → 判定。
BUCKETS = [
    ("≤7",    lambda n: n <= 7),
    ("8-9",   lambda n: 8 <= n <= 9),
    ("10-11", lambda n: 10 <= n <= 11),
    ("12-13", lambda n: 12 <= n <= 13),
    ("14-15", lambda n: 14 <= n <= 15),
    ("16+",   lambda n: n >= 16),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recommended", action="store_true",
                    help="勝負レース(推奨)のみ (既定は shobu 評価レース全体)")
    ap.add_argument("--point", type=int, default=100)
    ap.add_argument("--min-n", type=int, default=3)
    ap.add_argument("--max-n", type=int, default=8)
    args = ap.parse_args()

    races = _load_races(args.recommended)
    pop = "勝負レース(推奨)" if args.recommended else "shobu 評価レース全体"
    ns = list(range(args.min_n, args.max_n + 1))
    print(f"母集団: {pop} / 集計対象 {len(races)} レース (指数3頭以上+結果+3連単配当)")
    print(f"point_cost ¥{args.point} ・ 上位N頭3連単BOX (N={ns[0]}..{ns[-1]})\n")

    # ---- 頭数バケット × N の ROI 表 ----
    hdr = "  ".join(f"N={n:>2}" for n in ns)
    print(f"{'頭数':<7}{'races':>6}   {hdr}")
    print("-" * (7 + 6 + 3 + len(ns) * 6))
    for label, pred in BUCKETS:
        sub = [r for r in races if pred(r["n_runners"])]
        if not sub:
            continue
        cells = []
        best_n, best_roi = None, -1.0
        for n in ns:
            per = [x for r in sub if (x := _race_box(r, n, args.point))]
            a = _agg(per)
            if a["n"] == 0:
                cells.append("  -  ")
                continue
            cells.append(f"{a['roi']*100:4.0f}%")
            if a["roi"] > best_roi:
                best_roi, best_n = a["roi"], n
        marker = f"  ← best N={best_n} ({best_roi*100:.0f}%)" if best_n else ""
        print(f"{label:<7}{len(sub):>6}   " + "  ".join(cells) + marker)

    # ---- 各 N の的中率/ROI (全頭数まとめ) ----
    print("\n固定N (全頭数まとめ) — N が頭数を超える場合は頭数に丸め:")
    print(f"{'N':>3}{'races':>7}{'hits':>6}{'的中率':>8}{'賭金':>11}{'払戻':>11}{'ROI':>7}   95%CI")
    for n in ns:
        per = [x for r in races if (x := _race_box(r, n, args.point))]
        a = _agg(per)
        if a["n"] == 0:
            continue
        print(f"{n:>3}{a['n']:>7}{a['hits']:>6}{a['hit_rate']*100:>7.1f}%"
              f"{a['stake']:>11,}{a['payout']:>11,}{a['roi']*100:>6.0f}%"
              f"   {a['roi_lo']*100:.0f}-{a['roi_hi']*100:.0f}%")

    # ---- ルール比較: 現行 _shobu_box_size vs 固定N ----
    print("\nルール比較 (全頭数まとめ):")
    rules: list[tuple[str, callable]] = [
        ("現行 _shobu_box_size", lambda nr: _shobu_box_size(nr, base=5)),
    ]
    for n in ns:
        rules.append((f"固定 N={n}", (lambda nr, _n=n: _n)))
    print(f"{'ルール':<22}{'races':>6}{'hits':>6}{'的中率':>8}{'賭金':>11}{'払戻':>11}{'ROI':>7}   95%CI")
    for name, fn in rules:
        per = []
        for r in races:
            box_n = fn(r["n_runners"])
            x = _race_box(r, box_n, args.point)
            if x:
                per.append(x)
        a = _agg(per)
        if a["n"] == 0:
            continue
        print(f"{name:<22}{a['n']:>6}{a['hits']:>6}{a['hit_rate']*100:>7.1f}%"
              f"{a['stake']:>11,}{a['payout']:>11,}{a['roi']*100:>6.0f}%"
              f"   {a['roi_lo']*100:.0f}-{a['roi_hi']*100:.0f}%")

    print("\n※ 3連単 BOX flat は控除率 27.5% (払戻率 72.5%) が天井。ROI>72.5% でも母数が小さいと"
          " CI が広い。最適 N は『ROI 最大』だけでなく CI と整合性で読むこと。")


if __name__ == "__main__":
    main()
