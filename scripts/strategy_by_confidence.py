#!/usr/bin/env python3
"""Claude 指数の「自信度」(1位の指数値・1-2位の開き) で券種別 ROI がどう変わるか分析する。

ユーザ質問 (2026-06-30): 「Claude指数1位の数値の大きさ・2位との開きなどを考慮して、どの券種が
最も回収率が高いか求められる?」

各 shobu 評価レース (市場非依存=β除外) について Claude 指数の top1値 / gap12(=top1-top2) を求め、
全戦略 (STRATEGY_DEFS) の per-race 収支を `_strategy_race_legs` (本番と同ロジック) で取り、
①全体 ROI ランキング ②自信度との相関 ③自信度 median 分割での条件付き ROI を出す。

**注意**: 標本は ~70 レースと小さく、3連単/3連複系は的中が稀で ROI は1発で大きく振れる。
結論は CI と標本数を併読し、過剰一般化しないこと (CLAUDE.md の overfit 戒め参照)。読み取り専用。

    .venv/bin/python scripts/strategy_by_confidence.py
"""
from __future__ import annotations

import json
import random
from statistics import median

from api.store import (
    MARKET_INDEPENDENT_CUTOFF_ISO_JST,
    PRED_DIR,
    RESULT_DIR,
    STRATEGY_DEFS,
    _bootstrap_roi_ci,
    _claude_index_by_number,
    _safe_race_id,
    _scored_at,
    _shobu_eval_races,
    _strategy_race_legs,
)

LABEL = {k: lbl for k, lbl, _bt in STRATEGY_DEFS}


def _load() -> list[dict]:
    """市場非依存レースを [{top1, gap12, per:{strategy:(stake,payout,hit)}}] で集める。"""
    out: list[dict] = []
    for rid in _shobu_eval_races(False):
        safe = _safe_race_id(rid)
        if safe is None:
            continue
        p = PRED_DIR / f"{safe}.json"
        r = RESULT_DIR / f"{safe}.json"
        if not p.exists() or not r.exists():
            continue
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
            res = json.loads(r.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _scored_at(snap) < MARKET_INDEPENDENT_CUTOFF_ISO_JST:
            continue   # 市場由来 (β) は除外
        idx = _claude_index_by_number(snap)
        if len(idx) < 3:
            continue
        detail, reason = _strategy_race_legs(snap, res, point_cost=100, meta={})
        if reason != "ok" or detail is None:
            continue
        vals = sorted(idx.values(), reverse=True)

        def _g(i: int, j: int) -> float | None:
            return (vals[i] - vals[j]) if len(vals) > j else None

        out.append({
            "n_runners": snap.get("n_runners") or len(idx),
            "top1": vals[0],
            "top3": vals[2],
            "top4": vals[3] if len(vals) > 3 else None,
            "gap12": vals[0] - vals[1],
            "gap23": vals[1] - vals[2],
            "gap34": _g(2, 3),
            "gap45": _g(3, 4),
            "per": detail["per"],
        })
    return out


# 頭数バケット (3連複BOX は少頭数ほど当たる — 頭数交絡の確認用)。
FIELD_BUCKETS: list[tuple[str, "callable"]] = [
    ("≤9頭", lambda nr: nr <= 9),
    ("10-11頭", lambda nr: 10 <= nr <= 11),
    ("12頭+", lambda nr: nr >= 12),
]


def _agg(rows: list[dict], key: str) -> tuple[int, int, int, int, float]:
    """戦略 key の (races, races_hit, stake, payout, roi) を rows から集計 (bets>0 のみ)。"""
    races = hit = stake = payout = 0
    for row in rows:
        s = row["per"].get(key) or {}
        if not s.get("bets"):
            continue
        races += 1
        hit += 1 if s.get("hit") else 0
        stake += s.get("stake", 0)
        payout += s.get("payout", 0)
    roi = payout / stake if stake else 0.0
    return races, hit, stake, payout, roi


def _per_race_pairs(rows: list[dict], key: str) -> list[tuple[int, int]]:
    return [(row["per"][key]["stake"], row["per"][key]["payout"])
            for row in rows if row["per"].get(key, {}).get("bets")]


def _roi(pairs: list[tuple[int, int]]) -> float:
    s = sum(p[0] for p in pairs)
    return sum(p[1] for p in pairs) / s if s else 0.0


def _roi_delta_ci(high: list[tuple[int, int]], low: list[tuple[int, int]],
                  n_iter: int = 2000, seed: int = 42) -> tuple[float, float, float]:
    """ROI 差 Δ = ROI(high) − ROI(low) と、その bootstrap 95%CI (高低を各々再標本化)。

    Δ の CI が 0 を跨がなければ「自信度で回収率が有意に変わる」候補 (小標本なので参考)。
    """
    if not high or not low:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    base = _roi(high) - _roi(low)
    nh, nl = len(high), len(low)
    deltas = []
    for _ in range(n_iter):
        hs = [high[rng.randrange(nh)] for _ in range(nh)]
        ls = [low[rng.randrange(nl)] for _ in range(nl)]
        deltas.append(_roi(hs) - _roi(ls))
    deltas.sort()
    return base, deltas[int(0.025 * n_iter)], deltas[int(0.975 * n_iter)]


def _corr(xs: list[float], ys: list[float]) -> float:
    """Pearson 相関 (n<3 や分散0は 0)。"""
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def main() -> None:
    rows = _load()
    n = len(rows)
    print(f"市場非依存 (β除外) の集計対象: {n} レース\n")
    if n == 0:
        return

    # ---- ① 全体 ROI ランキング ----
    print("① 全戦略の全体 ROI (対象レース多→ROI降順) — 母数=レース数")
    print(f"  {'戦略':<22}{'対象R':>6}{'的中R':>6}{'ROI':>7}   95%CI")
    summ = []
    for key, _lbl, _bt in STRATEGY_DEFS:
        races, hit, stake, payout, roi = _agg(rows, key)
        lo, hi = _bootstrap_roi_ci(_per_race_pairs(rows, key))
        summ.append((key, races, hit, roi, lo, hi))
    for key, races, hit, roi, lo, hi in sorted(summ, key=lambda x: x[3], reverse=True):
        print(f"  {LABEL[key]:<20}{races:>6}{hit:>6}{roi*100:>6.0f}%   {lo*100:.0f}-{hi*100:.0f}%")

    # ---- ①' 自信度 高/低 で全券種の ROI がどう変わるか (回収率重視・差Δ降順) ----
    for feat, fname in (("gap12", "1-2位差(#1の抜け)"), ("top1", "1位の指数値")):
        med = median(r[feat] for r in rows)
        hi_rows = [r for r in rows if r[feat] >= med]
        lo_rows = [r for r in rows if r[feat] < med]
        print(f"\n①' {fname} 高/低 の券種別 ROI と差Δ (median={med:.0f}・高{len(hi_rows)}R/低{len(lo_rows)}R)"
              "  ★=Δの95%CIが0を跨がない")
        print(f"  {'戦略':<20}{'高ROI':>7}{'低ROI':>8}{'Δ(高-低)':>10}   95%CI(Δ)")
        res = []
        for key, _lbl, _bt in STRATEGY_DEFS:
            hp = _per_race_pairs(hi_rows, key)
            lp = _per_race_pairs(lo_rows, key)
            if len(hp) < 3 or len(lp) < 3:
                continue
            delta, dlo, dhi = _roi_delta_ci(hp, lp)
            res.append((key, _roi(hp), _roi(lp), delta, dlo, dhi))
        for key, hroi, lroi, delta, dlo, dhi in sorted(res, key=lambda x: x[3], reverse=True):
            sig = "★" if (dlo > 0 or dhi < 0) else " "
            print(f"  {LABEL[key]:<20}{hroi*100:>6.0f}%{lroi*100:>7.0f}%{delta*100:>+9.0f}pt"
                  f"   {dlo*100:+.0f}〜{dhi*100:+.0f}pt {sig}")

    # ---- ② 自信度 (top1 / gap12) と per-race net の相関 ----
    print("\n② 自信度と per-race 純益 (payout-stake) の相関 (正=自信高でその券種が伸びる)")
    print(f"  {'戦略':<22}{'r(top1)':>9}{'r(gap12)':>10}")
    for key, _lbl, _bt in STRATEGY_DEFS:
        sub = [r for r in rows if r["per"].get(key, {}).get("bets")]
        if len(sub) < 5:
            continue
        nets = [r["per"][key]["payout"] - r["per"][key]["stake"] for r in sub]
        c1 = _corr([r["top1"] for r in sub], nets)
        cg = _corr([r["gap12"] for r in sub], nets)
        print(f"  {LABEL[key]:<20}{c1:>+9.2f}{cg:>+10.2f}")

    # ---- ③ 自信度 median 分割での条件付き ROI ----
    for feat, fname in (("top1", "1位の指数値"), ("gap12", "1-2位の開き")):
        vals = [r[feat] for r in rows]
        med = median(vals)
        hi_rows = [r for r in rows if r[feat] >= med]
        lo_rows = [r for r in rows if r[feat] < med]
        print(f"\n③ {fname} で2分割 (median={med:.0f}) — 高 {len(hi_rows)}R / 低 {len(lo_rows)}R")
        print(f"  {'戦略':<22}{'高ROI':>7}{'(的中R)':>8}{'低ROI':>8}{'(的中R)':>8}")
        for key, _lbl, _bt in STRATEGY_DEFS:
            hr, hh, _hs, _hp, hroi = _agg(hi_rows, key)
            lr, lh, _ls, _lp, lroi = _agg(lo_rows, key)
            if hr == 0 and lr == 0:
                continue
            print(f"  {LABEL[key]:<20}{hroi*100:>6.0f}%{f'({hh}/{hr})':>8}"
                  f"{lroi*100:>7.0f}%{f'({lh}/{lr})':>8}")

    # ---- ④ 頭数別 の券種別 ROI / 的中率 ----
    print("\n④ 頭数別の券種 ROI / 的中率 (3連複BOX 等は少頭数ほど当たる=頭数交絡)")
    hdr = "".join(f"{lab:>14}" for lab, _ in FIELD_BUCKETS)
    print(f"  {'戦略':<20}{hdr}")
    for key, _lbl, _bt in STRATEGY_DEFS:
        cells = []
        any_data = False
        for _lab, pred in FIELD_BUCKETS:
            sub = [r for r in rows if pred(r["n_runners"])]
            races, hit, _s, _p, roi = _agg(sub, key)
            if races == 0:
                cells.append(f"{'-':>14}")
            else:
                any_data = True
                cells.append(f"{f'{roi*100:.0f}%({hit}/{races})':>14}")
        if any_data:
            print(f"  {LABEL[key]:<20}" + "".join(cells))

    # ---- ⑤ 頭数 × 自信度(gap12 median) の 3連複BOX/ワイドBOX 的中率・ROI ----
    gmed = median(r["gap12"] for r in rows)
    for key in ("trio1234box", "wide123box"):
        print(f"\n⑤ 頭数 × 1-2位差 (median={gmed:.0f}) の {LABEL[key]} 的中率/ROI")
        print(f"  {'頭数':<8}{'1-2位差 高 (的中/R・ROI)':>26}{'1-2位差 低 (的中/R・ROI)':>26}")
        for lab, pred in FIELD_BUCKETS:
            band = [r for r in rows if pred(r["n_runners"])]
            hi = [r for r in band if r["gap12"] >= gmed]
            lo = [r for r in band if r["gap12"] < gmed]
            hr, hh, _hs, _hp, hroi = _agg(hi, key)
            lr, lh, _ls, _lp, lroi = _agg(lo, key)
            if hr == 0 and lr == 0:
                continue
            hcell = f"{hh}/{hr} ({hroi*100:.0f}%)" if hr else "-"
            lcell = f"{lh}/{lr} ({lroi*100:.0f}%)" if lr else "-"
            print(f"  {lab:<8}{hcell:>26}{lcell:>26}")

    print("\n※ 標本 ~70R と小。3連単/3連複系は的中稀で ROI は1発で振れる → CI と的中Rを併読。"
          " 単発の高 ROI を戦略採用根拠にしない (CLAUDE.md の overfit 戒め)。"
          "\n※ 3連複BOX の的中は **頭数 と #1の抜け具合 の両方** が効き交互作用がある:"
          " ≤9頭は自信度に関係なく当たりやすい(小フィールド) / 12頭+はほぼ当たらない(0/20) /"
          " その間の 10-11頭で 1-2位差が大きい(=#1が抜けている)と的中が集中 (例 5/24 vs 0/10)。"
          " 3位以下の指数値・BOX境界(#4-#5差)は的中とほぼ無相関。")


if __name__ == "__main__":
    main()
