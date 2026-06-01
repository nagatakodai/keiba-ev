"""全レース (~7,000) で確率ベース戦略をバックテストする。

データ:
  - data/datasets/all.parquet      : 特徴量 + target + win_odds (= 締切前市場)
  - data/datasets/settled_odds.parquet : 結果HTML由来の確定払戻 (当たり組番のみ)
  - data/models/lgbm_lambdarank.txt : 本番モデル

確定オッズは「当たった組番」だけだが、ROI は当たり目の払戻だけで決まるので、
**確率で買い目を選ぶ戦略 (単勝 β-sweep / 3連単 Plan H1 / ワイド top など)** はフル評価できる。
EV/Kelly 選抜 (全組オッズが要る) は対象外 (別途 N=291 の trifecta cache 評価に委ねる)。

評価窓:
  - --valid-frac 0.2 (既定) で sorted race_id の後ろ20%
  - --all で全レース (in-sample 寄り含むが N 最大、傾向把握用)

使い方: python scripts/full_history_backtest.py [--valid-frac 0.2] [--all]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from itertools import permutations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402

from src.ev import DEFAULT_LAMBDA_2, DEFAULT_LAMBDA_3  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
SETTLED = ROOT / "data" / "datasets" / "settled_odds.parquet"
MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"


def _race_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _softmax(x: np.ndarray, t: float) -> np.ndarray:
    z = x / max(t, 1e-6)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def _blend(model_p: np.ndarray, market_p: np.ndarray, beta: float) -> np.ndarray:
    """loglinear: softmax((1-β)·log model + β·log market) — ev.estimate_probs と同代数。"""
    a = max(1.0 - beta, 0.0)
    b = max(beta, 0.0)
    lm = np.log(np.clip(model_p, 1e-9, None))
    lk = np.log(np.clip(market_p, 1e-9, None))
    z = a * lm + b * lk
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--valid-frac", type=float, default=0.2)
    ap.add_argument("--all", action="store_true", help="全レースで評価 (in-sample 寄り)")
    ap.add_argument("--top-m", type=int, default=8, help="3連単候補を上位M頭の順列に限定")
    args = ap.parse_args()

    df = pd.read_parquet(ALL)
    meta = json.loads(META.read_text())
    feat_cols = meta["feature_cols"]
    T = float(meta.get("softmax_temperature", 0.5))
    booster = lgb.Booster(model_file=str(MODEL))

    settled = pd.read_parquet(SETTLED)
    # (race_id, bet_type) -> {key: odds}
    settled_idx: dict[tuple[str, str], dict[str, float]] = {}
    for rid, bt, key, odds in settled.itertuples(index=False):
        settled_idx.setdefault((rid, bt), {})[key] = odds

    rids = sorted(df["race_id"].unique().tolist(), key=_race_int)
    if args.all:
        valid_rids = set(rids)
        label = f"ALL races (in-sample 寄り) n={len(valid_rids)}"
    else:
        n_valid = max(int(len(rids) * args.valid_frac), 1)
        valid_rids = set(rids[-n_valid:])
        label = f"chronological last {args.valid_frac:.0%} n={len(valid_rids)}"

    # 予測対象は finish 確定 race のみ
    g_win = df.groupby("race_id")["target_top1"].max()
    labeled = set(g_win[g_win == 1].index)
    valid_rids = valid_rids & labeled

    df = df[df["race_id"].isin(valid_rids)].copy()
    df["score"] = booster.predict(df[feat_cols].values, num_iteration=booster.best_iteration)

    betas = [round(b, 2) for b in np.arange(0.0, 1.01, 0.1)]

    # ---- 1) 単勝 top-1 β-sweep ----
    win_stat = {b: {"stake": 0, "payout": 0, "hits": 0, "n": 0} for b in betas}
    # ---- 2) 3連単 Plan H1 (確率上位K点、EV不問) ----
    tri_K = [1, 3, 6, 12]
    tri_stat = {k: {"stake": 0, "payout": 0, "hits": 0, "n": 0} for k in tri_K}
    # ---- 3) ワイド top-1 (確率最上位ペア) ----
    wide_stat = {"stake": 0, "payout": 0, "hits": 0, "n": 0}
    # market baseline (単勝 top-1 by market)
    mkt_stat = {"stake": 0, "payout": 0, "hits": 0, "n": 0}

    for rid, g in df.groupby("race_id"):
        g = g[g["win_odds"] > 0]
        if len(g) < 3:
            continue
        nums = g["horse_number"].to_numpy()
        scores = g["score"].to_numpy()
        odds = g["win_odds"].to_numpy()
        finish = g.set_index("horse_number")["finish_pos"]
        # 着順 1,2,3 の馬番
        order = finish[finish.isin([1, 2, 3])].sort_values()
        if len(order) < 3:
            continue
        win_triple = tuple(int(x) for x in order.index[:3])  # (1着,2着,3着)
        winner = win_triple[0]

        model_p = _softmax(scores, T)
        market_p = (1.0 / odds) / np.sum(1.0 / odds)

        tri_payout = settled_idx.get((rid, "trifecta"), {})
        wide_payout = settled_idx.get((rid, "wide"), {})

        # 単勝 β-sweep
        for b in betas:
            bp = _blend(model_p, market_p, b)
            top = int(nums[int(np.argmax(bp))])
            win_stat[b]["n"] += 1
            win_stat[b]["stake"] += 100
            if top == winner:
                win_stat[b]["hits"] += 1
                win_stat[b]["payout"] += int(100 * odds[int(np.where(nums == top)[0][0])])
        # market baseline
        topm = int(nums[int(np.argmax(market_p))])
        mkt_stat["n"] += 1
        mkt_stat["stake"] += 100
        if topm == winner:
            mkt_stat["hits"] += 1
            mkt_stat["payout"] += int(100 * odds[int(np.where(nums == topm)[0][0])])

        # 3連単 Plan H1: production の β=0.78 ブレンドで確率化 → 上位K点
        bp = _blend(model_p, market_p, 0.78)
        win_d = {int(nums[i]): float(bp[i]) for i in range(len(nums))}
        # Discounted Harville で place2/place3 を作り PL 連鎖
        w2 = {k: v ** DEFAULT_LAMBDA_2 for k, v in win_d.items()}
        w3 = {k: v ** DEFAULT_LAMBDA_3 for k, v in win_d.items()}
        # 上位M頭の順列だけ評価 (高確率順列を網羅)
        topM = [k for k, _ in sorted(win_d.items(), key=lambda kv: -kv[1])[:args.top_m]]
        W = sum(win_d.values())
        W2 = sum(w2.values())
        W3 = sum(w3.values())
        tri_probs = []
        for a, b2, c in permutations(topM, 3):
            pa = win_d[a] / W
            pb = w2[b2] / max(W2 - w2[a], 1e-9)
            pc = w3[c] / max(W3 - w3[a] - w3[b2], 1e-9)
            tri_probs.append(((a, b2, c), pa * pb * pc))
        tri_probs.sort(key=lambda x: -x[1])
        for K in tri_K:
            picks = [t for t, _ in tri_probs[:K]]
            tri_stat[K]["n"] += 1
            tri_stat[K]["stake"] += 100 * K
            if win_triple in picks:
                key = "-".join(str(x) for x in win_triple)
                o = tri_payout.get(key)
                if o:
                    tri_stat[K]["hits"] += 1
                    tri_stat[K]["payout"] += int(100 * o)

        # ワイド top-1: 確率最上位ペア (順不同)。当たり = top3 のうち2頭
        place3_set = set(win_triple)
        # ペア確率 ~ win_d[a]*win_d[b] 上位
        pair_rank = sorted(
            ((tuple(sorted((nums[i], nums[j]))), win_d[int(nums[i])] * win_d[int(nums[j])])
             for i in range(len(nums)) for j in range(i + 1, len(nums))),
            key=lambda x: -x[1],
        )
        if pair_rank:
            pick = pair_rank[0][0]
            wide_stat["n"] += 1
            wide_stat["stake"] += 100
            if pick[0] in place3_set and pick[1] in place3_set:
                key1 = f"{pick[0]}-{pick[1]}"
                key2 = f"{pick[1]}-{pick[0]}"
                o = wide_payout.get(key1) or wide_payout.get(key2)
                if o:
                    wide_stat["hits"] += 1
                    wide_stat["payout"] += int(100 * o)

    def roi(s):
        return (s["payout"] / s["stake"] * 100) if s["stake"] else 0.0

    print(f"\n=== Full-history backtest — {label} ===\n")
    print("[1] 単勝 top-1 β-sweep (blended model+market)")
    print(f"{'β':>5} {'hit%':>7} {'ROI':>8} {'hits/n':>12}")
    for b in betas:
        s = win_stat[b]
        h = s["hits"] / s["n"] * 100 if s["n"] else 0
        print(f"{b:>5.2f} {h:>6.1f}% {roi(s):>7.1f}% {s['hits']:>5}/{s['n']:<5}")
    s = mkt_stat
    print(f"{'mkt':>5} {s['hits']/s['n']*100:>6.1f}% {roi(s):>7.1f}% {s['hits']:>5}/{s['n']:<5}  (市場1番人気)")

    print("\n[2] 3連単 Plan H1 (β=0.78 確率上位K点、EV不問)")
    print(f"{'K点':>5} {'hit%':>7} {'ROI':>8} {'hits/n':>12}")
    for K in tri_K:
        s = tri_stat[K]
        h = s["hits"] / s["n"] * 100 if s["n"] else 0
        print(f"{K:>5} {h:>6.1f}% {roi(s):>7.1f}% {s['hits']:>5}/{s['n']:<5}")

    print("\n[3] ワイド top-1 (確率最上位ペア)")
    s = wide_stat
    h = s["hits"] / s["n"] * 100 if s["n"] else 0
    print(f"hit% {h:.1f}%  ROI {roi(s):.1f}%  hits {s['hits']}/{s['n']}")
    print("\n注: 確定オッズ(当たり払戻)ベース。控除率 ~20-25% → ROI 77.5-80% が市場効率の目安。")
    print("    ROI が安定して 80%+ かつ市場baselineを上回る戦略のみ実用候補。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
