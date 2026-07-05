"""上位3頭ギャップ + 荒れ具合特徴量の読み取り専用スイープ (2026-07-05 ユーザ指示)。

「Claude 指数上位3頭 (指数と市場との差 / 1,2,3 の開き / 4頭目との開き) と市場の荒れ具合
(オッズの開き) からどの券種の回収率が高くなるか」をプレレジ登録前に in-sample で観察する。

これは **discovery (発見) 専用**: ここで見つけた条件×券種は SIGNAL_RULES に新 key で
プレレジ登録し、登録日以降の prospective データだけで確証判定する (発見と検証の分離)。
出力の ROI は発見に使ったデータそのものなので楽観バイアス込み — bin-selection の罠
(trio1234box 失効・馬連202%→36% と同型) を常に疑い、drop-best / 前後半分割を併記する。

使い方: .venv/bin/python scripts/signal_feature_sweep.py [--min-n 15]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.store import (  # noqa: E402
    STRATEGY_DEFS,
    _agreement_pairs,
    _roi_ci,
    _roi_of,
    _tagged_eval_races,
)

# 特徴量ごとの候補閾値 (min 側 / max 側)。固定の丸い値 = 母集団が増えても分割点が動かない
# (_FAVORITE_RATIO_THRESHOLD と同じ流儀)。分位数を見て歪んでいたらここを直して再実行する。
FEATURE_GRID: dict[str, dict[str, list[float]]] = {
    "gap12":         {"min": [3, 5, 8, 12], "max": [2, 4]},
    "gap23":         {"min": [3, 5, 8],     "max": [2]},
    "gap34":         {"min": [3, 5, 8],     "max": [2]},
    "top3_rank_gap": {"min": [1, 2, 3, 5],  "max": [0]},
    "top3_idx_diff": {"min": [3, 5, 10],    "max": [0]},
    "fav_odds":      {"min": [2.5, 3.0, 4.0], "max": [1.6, 2.0]},
    "top3_conc":     {"min": [0.60, 0.70],  "max": [0.50, 0.55]},
    # FL バイアス (単勝/複勝 オッズ比, 2026-07-06): 高い = 市場が「絡むが勝ち切らない」(3着型)
    # と見る馬。複勝はレンジ下限なので比は系統的に高め — 閾値は分位を見て調整。
    "pw_top1":       {"min": [3.0, 3.5, 4.0], "max": [2.2, 2.6]},
    "pw_top2":       {"min": [3.5, 4.0, 4.5], "max": [2.6, 3.0]},
    "pw_top3":       {"min": [3.5, 4.0, 4.5], "max": [2.6, 3.0]},
}


def _quantiles(vals: list[float]) -> str:
    if not vals:
        return "(なし)"
    vs = sorted(vals)
    q = lambda p: vs[min(len(vs) - 1, int(p * len(vs)))]
    return (f"n={len(vs)} p10={q(0.10):.2f} p25={q(0.25):.2f} p50={q(0.50):.2f} "
            f"p75={q(0.75):.2f} p90={q(0.90):.2f}")


def _stats(pairs: list[tuple[int, int]]) -> dict:
    n = len(pairs)
    roi = _roi_of(pairs) if pairs else 0.0
    lo, hi = _roi_ci(pairs) if pairs else (0.0, 0.0)
    drop = 0.0
    if n >= 2:
        best_i = max(range(n), key=lambda i: pairs[i][1] - pairs[i][0])
        drop = _roi_of([p for i, p in enumerate(pairs) if i != best_i])
    half = n // 2
    first = _roi_of(pairs[:half]) if half else 0.0
    second = _roi_of(pairs[half:]) if half else 0.0
    hits = sum(1 for p in pairs if p[1] > 0)
    return {"n": n, "roi": roi, "lo": lo, "hi": hi, "drop": drop,
            "first": first, "second": second, "hits": hits}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=15, help="表示する最小レース数")
    ap.add_argument("--top", type=int, default=45, help="ROI 上位の表示行数")
    args = ap.parse_args()

    records = _tagged_eval_races()
    print(f"対象レース (市場非依存・結果確定・指数3頭以上): {len(records)}")
    strat_keys = [d[0] for d in STRATEGY_DEFS]

    print("\n== 特徴量の分布 ==")
    for name in FEATURE_GRID:
        vals = [r["features"][name] for r in records if r["features"].get(name) is not None]
        print(f"  {name:>14}: {_quantiles(vals)}")

    # 無条件ベースライン (条件の付加価値を測る基準)。
    print("\n== 無条件ベースライン (全レース) ==")
    base_roi: dict[str, float] = {}
    for sk in strat_keys:
        pairs = [got[0] for r in records if (got := _agreement_pairs([r["per"]], [sk]))]
        s = _stats(pairs)
        base_roi[sk] = s["roi"]
        print(f"  {sk:>12}: n={s['n']:>3} ROI {s['roi']*100:6.1f}% "
              f"CI[{s['lo']*100:5.1f},{s['hi']*100:6.1f}] drop-best {s['drop']*100:6.1f}%")

    # 条件 × 戦略のスイープ。
    rows = []
    for name, sides in FEATURE_GRID.items():
        conds = ([("min", t) for t in sides.get("min", [])]
                 + [("max", t) for t in sides.get("max", [])])
        for side, thr in conds:
            match = [r for r in records
                     if r["features"].get(name) is not None
                     and ((side == "min" and r["features"][name] >= thr)
                          or (side == "max" and r["features"][name] <= thr))]
            for sk in strat_keys:
                pairs = [got[0] for r in match if (got := _agreement_pairs([r["per"]], [sk]))]
                if len(pairs) < args.min_n:
                    continue
                s = _stats(pairs)
                mpairs = [got[0] for r in match if r["mper"] is not None
                          and (got := _agreement_pairs([r["mper"]], [sk]))]
                mroi = _roi_of(mpairs) if mpairs else 0.0
                rows.append({"cond": f"{name} {'≥' if side == 'min' else '≤'} {thr}",
                             "strategy": sk, "mkt_roi": mroi,
                             "edge_vs_base": s["roi"] - base_roi[sk], **s})

    rows.sort(key=lambda r: -r["roi"])
    print(f"\n== 条件 × 券種 スイープ (n≥{args.min_n}, ROI 上位 {args.top}) ==")
    print(f"{'条件':<22} {'券種':>12} {'n':>4} {'hit%':>5} {'ROI':>7} {'CI':>15} "
          f"{'drop':>7} {'前半':>7} {'後半':>7} {'市場基準':>7} {'対無条件':>8}")
    for r in rows[:args.top]:
        print(f"{r['cond']:<22} {r['strategy']:>12} {r['n']:>4} "
              f"{r['hits']/r['n']*100:>4.0f}% {r['roi']*100:>6.1f}% "
              f"[{r['lo']*100:>5.1f},{r['hi']*100:>6.1f}] {r['drop']*100:>6.1f}% "
              f"{r['first']*100:>6.1f}% {r['second']*100:>6.1f}% {r['mkt_roi']*100:>6.1f}% "
              f"{r['edge_vs_base']*100:>+7.1f}pt")

    # 逆側 (最悪セル) も見送り規律候補として観察。
    rows.sort(key=lambda r: r["roi"])
    print(f"\n== 同・ROI 下位 15 (見送り規律候補) ==")
    for r in rows[:15]:
        print(f"{r['cond']:<22} {r['strategy']:>12} {r['n']:>4} "
              f"{r['roi']*100:>6.1f}% drop {r['drop']*100:>6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
