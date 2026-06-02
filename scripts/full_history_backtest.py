"""全レース (~7,000) で確率ベース戦略をバックテストする。

データ:
  - data/datasets/all.parquet          : 特徴量 + target + win_odds (= ほぼ確定単勝オッズ)
  - data/datasets/settled_odds.parquet : 結果HTML由来の確定払戻 (当たり組番のみ)
  - data/models/lgbm_lambdarank.txt    : 本番モデル (--fresh-model で窓外学習の別モデルを使う)

確定オッズは「当たった組番」だけだが、ROI は当たり目の払戻だけで決まるので、確率で買い目を
選ぶ戦略 (単勝 β-sweep / 3連単 Plan H1 / ワイド top) はフル評価できる。EV/Kelly 選抜
(全組オッズが要る) は対象外。

**+EV の正しい定義 (重要)**: pari-mutuel は自分の賭け金も控除原資に入るので、利益が出る
(= +EV) のは **長期 ROI > 100%** の戦略だけ。市場平均 (控除率ぶん ~80%) を上回ることは
「損が市場より小さい」に過ぎず依然 -EV。よって本スクリプトは break-even=100% で判定し、
市場 baseline は「同じ -EV の中でのエッジ診断 (相対比較)」としてのみ使う。点推定だけでは
事後選択バイアスに弱いので **bootstrap 95%CI** を併記し、CI 下限が 100% を超えた戦略のみ
を「+EV 候補」と呼ぶ。

**検証窓の注意**: 既定 (--valid-frac 0.2) の後20%窓は、本番モデルの early-stopping と
softmax 温度フィットに使われた窓そのもの (= mild in-sample / peeking)。真の out-of-sample が
要るときは `--fresh-model`: 前70%で学習・[70,80)%で早期停止 (eval 窓を一切見ない) し
後20%で評価する。

**speed_v2 並列合成の比較 (--speed-v2-blend)**: live (ev.estimate_probs) は LightGBM
fundamental と v2 速度図表 (実データ par+pace+trip) を幾何 (loglinear) 平均する。本スクリプトは
同じ式で「LGBM 単独 (base, sv2=0)」と「LGBM⊗speed_v2 並列 (sv2=W)」の単勝 top-1 ROI を
**同一 race で paired bootstrap** 比較し、差が有意か (CI が 0 を跨ぐか) を出す。注意: par table
(data/cache/par_times.json) は全 race 集計なので eval 窓の時計も含む = speed_v2 にはごく軽い
par leakage がある (個馬の結果ではなく condition 水準なので市場も同じ par を見る前提)。
それでも edge が出なければ「leakage 込みでも勝てない」= test_speed_v2 の β-MLE≈1.0 と整合。

使い方:
  python scripts/full_history_backtest.py                 # 本番モデル (peeking 開示つき)
  python scripts/full_history_backtest.py --fresh-model    # 真の out-of-sample
  python scripts/full_history_backtest.py --all            # 全レース (in-sample 上界)
  python scripts/full_history_backtest.py --fresh-model --speed-v2-blend 0.5  # 並列合成の効果
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import permutations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402

from src.ev import (  # noqa: E402
    DEFAULT_LAMBDA_2, DEFAULT_LAMBDA_3, SPEED_V2_BLEND_LIVE, SPEED_V2_TEMP,
    power_method_overround,
)

ALL = ROOT / "data" / "datasets" / "all.parquet"
V2 = ROOT / "data" / "datasets" / "v2_features.parquet"
SETTLED = ROOT / "data" / "datasets" / "settled_odds.parquet"
MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"

# bootstrap は決定的にしたい (Math.random 不可な環境制約とも整合) → 固定 seed。
BOOT_SEED = 12345
BOOT_N = 4000
BREAK_EVEN = 100.0  # +EV の閾値は 100% (市場の ~80% ではない)


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


def _blend(model_p: np.ndarray, market_devig: np.ndarray, beta: float) -> np.ndarray:
    """loglinear: softmax((1-β)·log model + β·log π) — ev.estimate_probs と同代数。
    market_devig は power_method de-vig 済の市場暗黙率 (本番と同一)。"""
    a = max(1.0 - beta, 0.0)
    b = max(beta, 0.0)
    lm = np.log(np.clip(model_p, 1e-9, None))
    lk = np.log(np.clip(market_devig, 1e-9, None))
    z = a * lm + b * lk
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def _speed_v2_probs(best: np.ndarray, n_runs: np.ndarray, t: float):
    """speed_chart.speed_v2_win_probs と同一: 有効馬の speed_v2_best を field 内 z-score →
    softmax(z/T)。no-data 馬は z=0 (中立)。coverage 不足 (有効馬<3 or field半数未満) は None。"""
    best = np.asarray(best, dtype=float)
    n_runs = np.asarray(n_runs)
    mask = n_runs > 0
    k = int(mask.sum())
    if k < 3 or k < 0.5 * len(best):
        return None
    xs = best[mask]
    mean = xs.mean()
    sd = xs.std(ddof=1) if k > 1 else 0.0
    z = np.zeros(len(best), dtype=float)
    if sd > 1e-6:
        z[mask] = (best[mask] - mean) / sd
    return _softmax(z, t)


def _loglinear(f: np.ndarray, g: np.ndarray, w: float) -> np.ndarray:
    """ev._loglinear_blend と同代数: c ∝ f^(1-w)·g^w。"""
    a = max(1.0 - w, 0.0)
    b = max(w, 0.0)
    z = a * np.log(np.clip(f, 1e-9, None)) + b * np.log(np.clip(g, 1e-9, None))
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def _devig(odds: np.ndarray) -> np.ndarray:
    """1/odds を power-method de-overround (本番 estimate_probs と同一)。"""
    raw = 1.0 / odds
    raw = raw / raw.sum()
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    v = np.array([d[i] for i in range(len(raw))], dtype=float)
    s = v.sum()
    return v / s if s > 0 else raw


def _bootstrap_roi_ci(profit: np.ndarray, stake: np.ndarray, n=BOOT_N, seed=BOOT_SEED):
    """per-race の (profit, stake) を race 単位で resample して ROI(%) の 95%CI を返す。"""
    rng = np.random.default_rng(seed)
    m = len(profit)
    if m == 0 or stake.sum() == 0:
        return (0.0, 0.0)
    idx = rng.integers(0, m, size=(n, m))
    pe = profit[idx].sum(axis=1)
    st = stake[idx].sum(axis=1)
    rois = np.where(st > 0, pe / st * 100, 0.0)
    return (float(np.percentile(rois, 2.5)), float(np.percentile(rois, 97.5)))


def _paired_diff_ci(p_a, p_b, stake, n=BOOT_N, seed=BOOT_SEED):
    """戦略A,B の per-race profit を同一 resample で差し引き、ROI差(pt) の 95%CI。"""
    rng = np.random.default_rng(seed)
    m = len(p_a)
    if m == 0 or stake.sum() == 0:
        return (0.0, 0.0, 0.0)
    idx = rng.integers(0, m, size=(n, m))
    sa = p_a[idx].sum(axis=1)
    sb = p_b[idx].sum(axis=1)
    st = stake[idx].sum(axis=1)
    diff = np.where(st > 0, (sa - sb) / st * 100, 0.0)
    return (float(np.percentile(diff, 2.5)), float(diff.mean()), float(np.percentile(diff, 97.5)))


def _train_fresh(df: pd.DataFrame, rids_sorted: list[str], feat_cols, params):
    """前70%で学習・[70,80)% で早期停止する『eval 窓を一切見ない』別モデルを訓練して返す。"""
    n = len(rids_sorted)
    tr = set(rids_sorted[: int(n * 0.70)])
    va = set(rids_sorted[int(n * 0.70): int(n * 0.80)])
    tdf = df[df["race_id"].isin(tr)].sort_values("race_id")
    vdf = df[df["race_id"].isin(va)].sort_values("race_id")
    gtr = tdf.groupby("race_id", sort=False).size().to_numpy()
    gva = vdf.groupby("race_id", sort=False).size().to_numpy()
    dtr = lgb.Dataset(tdf[feat_cols].values, label=tdf["target_rank"].values, group=gtr)
    dva = lgb.Dataset(vdf[feat_cols].values, label=vdf["target_rank"].values, group=gva, reference=dtr)
    booster = lgb.train(
        params, dtr, num_boost_round=800, valid_sets=[dva],
        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)],
    )
    # 温度も窓外で fit (valid=[70,80)% の log loss 最小化)
    best_T, best_ll = 0.5, float("inf")
    for T in [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0]:
        ll = 0.0
        nr = 0
        for _r, g in vdf.groupby("race_id", sort=False):
            sc = booster.predict(g[feat_cols].values, num_iteration=booster.best_iteration)
            p = _softmax(np.asarray(sc), T)
            y = g["target_top1"].to_numpy()
            if y.sum() == 0:
                continue
            ll -= float(np.log(max(p[int(np.argmax(y))], 1e-12)))
            nr += 1
        ll = ll / max(nr, 1)
        if ll < best_ll:
            best_ll, best_T = ll, T
    return booster, best_T


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--valid-frac", type=float, default=0.2)
    ap.add_argument("--all", action="store_true", help="全レースで評価 (in-sample 上界)")
    ap.add_argument("--fresh-model", action="store_true",
                    help="eval 窓を一切見ない別モデルを訓練して真の out-of-sample 評価")
    ap.add_argument("--top-m", type=int, default=8)
    ap.add_argument("--speed-v2-blend", type=float, default=SPEED_V2_BLEND_LIVE,
                    help="v2速度図表を LightGBM fundamental と並列合成する重み (0=base のみ)。"
                         "base(sv2=0) と paired 比較する")
    args = ap.parse_args()
    sv2_w = max(min(args.speed_v2_blend, 1.0), 0.0)

    df = pd.read_parquet(ALL)
    meta = json.loads(META.read_text())
    feat_cols = meta["feature_cols"]
    rids = sorted(df["race_id"].unique().tolist(), key=_race_int)

    if args.fresh_model:
        params = dict(meta["params"])
        booster, T = _train_fresh(df, rids, feat_cols, params)
        model_desc = f"fresh model (train [0,70)%, early-stop [70,80)%, T={T:.2f} fit on [70,80)%)"
        window_note = "真の out-of-sample (eval 窓 [80,100)% はモデルが一切見ていない)"
    else:
        booster = lgb.Booster(model_file=str(MODEL))
        T = float(meta.get("softmax_temperature", 0.5))
        model_desc = f"production model (T={T:.2f})"
        window_note = ("⚠ mild in-sample: 既定後20%窓は本番モデルの early-stop+温度フィット窓"
                       "そのもの (peeking)。真の OOS は --fresh-model")

    settled = pd.read_parquet(SETTLED)
    settled_idx: dict[tuple[str, str], dict[str, float]] = {}
    for rid, bt, key, odds in settled.itertuples(index=False):
        settled_idx.setdefault((rid, bt), {})[key] = odds

    if args.all:
        valid_rids = set(rids)
        label = f"ALL races (in-sample 上界) n={len(valid_rids)}"
        window_note = "in-sample 上界 (モデルが学習に使った race を含む)。傾向把握のみ"
    else:
        n_valid = max(int(len(rids) * args.valid_frac), 1)
        valid_rids = set(rids[-n_valid:])
        label = f"chronological last {args.valid_frac:.0%} n={len(valid_rids)}"

    g_win = df.groupby("race_id")["target_top1"].max()
    labeled = set(g_win[g_win == 1].index)
    valid_rids = valid_rids & labeled
    vdf = df[df["race_id"].isin(valid_rids)].copy()
    vdf["score"] = booster.predict(vdf[feat_cols].values, num_iteration=booster.best_iteration)
    # v2 速度図表 (speed_v2_best / v2_n_runs) を merge。無ければ 0 (= sv2 no-op)。
    if sv2_w > 0 and V2.exists():
        v2df = pd.read_parquet(V2)[["race_id", "horse_number", "speed_v2_best", "v2_n_runs"]]
        vdf = vdf.merge(v2df, on=["race_id", "horse_number"], how="left")
    if "speed_v2_best" not in vdf.columns:
        vdf["speed_v2_best"] = 0.0
        vdf["v2_n_runs"] = 0
    vdf["speed_v2_best"] = vdf["speed_v2_best"].fillna(0.0)
    vdf["v2_n_runs"] = vdf["v2_n_runs"].fillna(0)

    betas = [round(b, 2) for b in np.arange(0.0, 1.01, 0.1)]

    # per-race profit/stake を貯めて bootstrap CI を出す。
    # base = LGBM fundamental のみ / sv2 = LGBM⊗speed_v2 並列 fundamental。
    win_profit = {b: [] for b in betas}
    win_stake = {b: [] for b in betas}
    win_profit_sv2 = {b: [] for b in betas}
    mkt_profit, mkt_stake = [], []
    tri_K = [1, 3, 6, 12]
    tri_profit = {k: [] for k in tri_K}
    tri_stake = {k: [] for k in tri_K}
    wide_profit, wide_stake = [], []

    for rid, g in vdf.groupby("race_id"):
        g = g[g["win_odds"] > 0]
        if len(g) < 3:
            continue
        nums = g["horse_number"].to_numpy()
        scores = g["score"].to_numpy()
        odds = g["win_odds"].to_numpy()
        finish = g.set_index("horse_number")["finish_pos"]
        order = finish[finish.isin([1, 2, 3])].sort_values()
        if len(order) < 3:
            continue
        win_triple = tuple(int(x) for x in order.index[:3])
        winner = win_triple[0]
        widx = int(np.where(nums == winner)[0][0])

        model_p = _softmax(scores, T)
        market_devig = _devig(odds)

        # speed_v2 並列合成: fundamental = LGBM ⊗ speed_v2 (live ev.estimate_probs と同式)。
        # 図表が薄ければ sv2_p=None → fund_p=model_p (no-op, live と同じ縮退)。
        fund_p = model_p
        if sv2_w > 0:
            sv2_p = _speed_v2_probs(g["speed_v2_best"].to_numpy(),
                                    g["v2_n_runs"].to_numpy(), SPEED_V2_TEMP)
            if sv2_p is not None:
                fund_p = _loglinear(model_p, sv2_p, sv2_w)

        tri_payout = settled_idx.get((rid, "trifecta"), {})
        wide_payout = settled_idx.get((rid, "wide"), {})

        def _win_pay(fund, b):
            bp = _blend(fund, market_devig, b)
            top = int(nums[int(np.argmax(bp))])
            return int(100 * odds[int(np.where(nums == top)[0][0])]) if top == winner else 0

        for b in betas:
            win_stake[b].append(100)
            win_profit[b].append(_win_pay(model_p, b))      # base (sv2=0)
            win_profit_sv2[b].append(_win_pay(fund_p, b))    # sv2 並列
        # market baseline (β=1.0 相当だが素の市場で)
        topm = int(nums[int(np.argmax(market_devig))])
        mkt_stake.append(100)
        mkt_profit.append(int(100 * odds[int(np.where(nums == topm)[0][0])]) if topm == winner else 0)

        # 3連単 Plan H1: β=0.78 ブレンド → PL 連鎖、上位K点 (fund_p = sv2 並列 fundamental)
        bp = _blend(fund_p, market_devig, 0.78)
        win_d = {int(nums[i]): float(bp[i]) for i in range(len(nums))}
        w2 = {k: v ** DEFAULT_LAMBDA_2 for k, v in win_d.items()}
        w3 = {k: v ** DEFAULT_LAMBDA_3 for k, v in win_d.items()}
        topM = [k for k, _ in sorted(win_d.items(), key=lambda kv: -kv[1])[:args.top_m]]
        W, W2, W3 = sum(win_d.values()), sum(w2.values()), sum(w3.values())
        tri_probs = []
        for a, b2, c in permutations(topM, 3):
            pa = win_d[a] / W
            pb = w2[b2] / max(W2 - w2[a], 1e-9)
            pc = w3[c] / max(W3 - w3[a] - w3[b2], 1e-9)
            tri_probs.append(((a, b2, c), pa * pb * pc))
        tri_probs.sort(key=lambda x: -x[1])
        for K in tri_K:
            picks = [t for t, _ in tri_probs[:K]]
            tri_stake[K].append(100 * K)
            pay = 0
            if win_triple in picks:
                o = tri_payout.get("-".join(str(x) for x in win_triple))
                if o:
                    pay = int(100 * o)
            tri_profit[K].append(pay)

        # ワイド top-1
        place3_set = set(win_triple)
        pair_rank = sorted(
            ((tuple(sorted((int(nums[i]), int(nums[j])))), win_d[int(nums[i])] * win_d[int(nums[j])])
             for i in range(len(nums)) for j in range(i + 1, len(nums))),
            key=lambda x: -x[1],
        )
        if pair_rank:
            pick = pair_rank[0][0]
            wide_stake.append(100)
            pay = 0
            if pick[0] in place3_set and pick[1] in place3_set:
                o = wide_payout.get(f"{pick[0]}-{pick[1]}") or wide_payout.get(f"{pick[1]}-{pick[0]}")
                if o:
                    pay = int(100 * o)
            wide_profit.append(pay)

    def roi(p, s):
        p, s = np.asarray(p), np.asarray(s)
        return (p.sum() / s.sum() * 100) if s.sum() else 0.0

    def line(name, p, s):
        p, s = np.asarray(p), np.asarray(s)
        r = roi(p, s)
        lo, hi = _bootstrap_roi_ci(p, s)
        flag = "  ← CI下限>100% = +EV候補" if lo > BREAK_EVEN else ""
        n_hit = int((p > 0).sum())
        return f"{name:>7} {r:>6.1f}%  95%CI[{lo:>5.1f},{hi:>5.1f}]  hit {n_hit}/{len(p)}{flag}"

    print(f"\n=== Full-history backtest — {label} ===")
    print(f"model: {model_desc}")
    print(f"window: {window_note}")
    print(f"break-even (+EV ライン) = ROI {BREAK_EVEN:.0f}%。市場 baseline は -EV 内のエッジ診断用。\n")

    print("[1a] 単勝 top-1 β-sweep — base (LGBM fundamental のみ, speed_v2_blend=0)")
    for b in betas:
        print(line(f"β={b:.1f}", win_profit[b], win_stake[b]))
    print(line("market", mkt_profit, mkt_stake) + "  (de-vig 市場1番人気)")
    best_b = max(betas, key=lambda b: roi(win_profit[b], win_stake[b]))
    lo, mean, hi = _paired_diff_ci(np.asarray(win_profit[best_b]), np.asarray(mkt_profit), np.asarray(mkt_stake))
    sig = "有意 (CI が 0 を跨がない)" if (lo > 0 or hi < 0) else "有意でない (CI が 0 を跨ぐ)"
    print(f"  best β={best_b:.1f} vs market のROI差: {mean:+.1f}pt  95%CI[{lo:+.1f},{hi:+.1f}] → {sig}")

    if sv2_w > 0:
        print(f"\n[1b] 単勝 top-1 β-sweep — sv2 (LGBM ⊗ speed_v2 並列, speed_v2_blend={sv2_w:.2f})")
        for b in betas:
            print(line(f"β={b:.1f}", win_profit_sv2[b], win_stake[b]))
        # base vs sv2 の paired diff CI (同一 race・同 stake、sv2 − base)。
        # β=0.0 は live (市場無視) の設定そのもの。best_b は base の最良 β で揃える。
        print("\n  speed_v2 効果 (sv2 − base のROI差, 同一race paired):")
        for b in sorted({0.0, round(best_b, 2), 0.78}):
            if b not in win_profit:
                continue
            dlo, dmean, dhi = _paired_diff_ci(
                np.asarray(win_profit_sv2[b]), np.asarray(win_profit[b]), np.asarray(win_stake[b]))
            dsig = "有意" if (dlo > 0 or dhi < 0) else "有意でない (CI が 0 を跨ぐ)"
            tag = " ←live(市場無視)" if b == 0.0 else ""
            print(f"    β={b:.2f}{tag}: base {roi(win_profit[b], win_stake[b]):.1f}% → "
                  f"sv2 {roi(win_profit_sv2[b], win_stake[b]):.1f}%  "
                  f"Δ{dmean:+.1f}pt 95%CI[{dlo:+.1f},{dhi:+.1f}] → {dsig}")

    _fund_note = f"sv2 並列 fundamental (speed_v2_blend={sv2_w:.2f})" if sv2_w > 0 else "LGBM fundamental"
    print(f"\n[2] 3連単 Plan H1 (β=0.78 確率上位K点、EV不問) — {_fund_note}")
    for K in tri_K:
        print(line(f"K={K}", tri_profit[K], tri_stake[K]))

    print(f"\n[3] ワイド top-1 (確率最上位ペア) — {_fund_note}")
    print(line("wide", wide_profit, wide_stake))

    print("\n判定基準: ROI 95%CI 下限 > 100% の戦略のみ実弾候補。市場(80%)超えは『損が小さい』に過ぎず -EV。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
