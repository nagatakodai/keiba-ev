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
        out.append({
            "top1": vals[0],
            "gap12": vals[0] - vals[1],
            "per": detail["per"],
        })
    return out


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

    print("\n※ 標本 ~70R と小。3連単/3連複系は的中稀で ROI は1発で振れる → CI と的中Rを併読。"
          " 単発の高 ROI を戦略採用根拠にしない (CLAUDE.md の overfit 戒め)。")


if __name__ == "__main__":
    main()
