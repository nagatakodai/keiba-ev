"""JRA / NAR セグメント別モデルを学習し、Benter 二段の正しい実装で β・λ を MLE 推定する。

研究メモ (data/research/profitable_betting.md) の提案1+2 を実装:
  - 時系列 3-fold: A=[0,60%) で fundamental LGBM 学習、B=[60,80%) で β/λ/T を MLE 推定
    (fundamental を学習した fold とは別 partition で 1 回だけ → overfit を避ける)、
    C=[80,100%) は完全 hold-out で frozen パラメータを評価。
  - β (market_blend): conditional-logit 勝者 log-lik を最大化 (α=1-β 制約)。
  - λ2,λ3 (Harville place 指数): 3連単の着順 PL log-lik を最大化 (= 公衆が下手な
    2-3着条件付き確率を自前データで校正。3連単 overlay の前提条件)。
  - T (softmax 温度): 勝者 log loss 最小化。

segment ∈ {jra, nar}。race_id[4:6] が 01-10 = JRA、それ以外 = NAR。
出力: data/models/lgbm_<seg>.txt + lgbm_<seg>_metadata.json
  (feature_cols, softmax_temperature, market_blend_mle, lambda_2_mle, lambda_3_mle,
   segment, n_*_races, holdout_eval{...})。ev.py がこれを読んで segment 別にルーティング。

使い方: python scripts/train_segment_models.py [--segment jra|nar|all|both]
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import permutations
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import minimize, minimize_scalar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ev import power_method_overround  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
MODELS = ROOT / "data" / "models"
META0 = json.loads((MODELS / "lgbm_metadata.json").read_text())
FEATS = META0["feature_cols"]
PARAMS = dict(META0["params"])
JRA_CODES = {f"{i:02d}" for i in range(1, 11)}


def _race_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _seg_mask(df: pd.DataFrame, segment: str) -> pd.Series:
    is_jra = df["race_id"].astype(str).str[4:6].isin(JRA_CODES)
    if segment == "jra":
        return is_jra
    if segment == "nar":
        return ~is_jra
    return pd.Series(True, index=df.index)


def _softmax(x, t):
    z = np.asarray(x) / max(t, 1e-6)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def _devig(odds):
    # power_method_overround には **未正規化** 1/odds (Σ=overround>1) を渡す。
    # 以前は raw/raw.sum() を渡しており k=1 の恒等写像 (no-op) だった (2026-06-10 修正)。
    raw = 1.0 / np.asarray(odds, float)
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    v = np.array([d[i] for i in range(len(raw))], float)
    s = v.sum()
    return v / s if s > 0 else raw / raw.sum()


def _race_arrays(df):
    """race ごとに (scores用feat, win_odds, winner_idx, finish_order_idx) を貯める。"""
    out = []
    for rid, g in df.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 3:
            continue
        fp = g["finish_pos"].to_numpy()
        # 1/2/3 着馬の行 index (g 内の位置)
        pos = {int(p): i for i, p in enumerate(fp) if p in (1, 2, 3)}
        if not all(k in pos for k in (1, 2, 3)):
            continue
        out.append({
            "X": g[FEATS].values,
            "odds": g["win_odds"].to_numpy(float),
            "winner": pos[1],
            "order": (pos[1], pos[2], pos[3]),
        })
    return out


def _fit_T(booster, races):
    best_T, best = 0.5, 1e18
    for T in [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.5]:
        ll, n = 0.0, 0
        for r in races:
            p = _softmax(booster.predict(r["X"]), T)
            ll -= np.log(max(p[r["winner"]], 1e-12))
            n += 1
        ll /= max(n, 1)
        if ll < best:
            best, best_T = ll, T
    return best_T


def _fit_beta(booster, races, T):
    pre = [(_softmax(booster.predict(r["X"]), T), _devig(r["odds"]), r["winner"]) for r in races]

    def neg_ll(beta):
        a = 1.0 - beta
        s = 0.0
        for mp, mk, w in pre:
            z = a * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
            z = z - z.max()
            e = np.exp(z)
            bp = e / e.sum()
            s -= np.log(max(bp[w], 1e-12))
        return s
    res = minimize_scalar(neg_ll, bounds=(0.0, 1.0), method="bounded")
    return float(res.x)


def _fit_lambda(booster, races, T, beta):
    # blended win 確率を precompute
    blended = []
    for r in races:
        mp = _softmax(booster.predict(r["X"]), T)
        mk = _devig(r["odds"])
        z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
        z = z - z.max()
        e = np.exp(z)
        blended.append((e / e.sum(), r["order"]))

    def neg_ll(params):
        l2, l3 = params
        s = 0.0
        for w, (a, b, c) in blended:
            w2 = w ** l2
            w3 = w ** l3
            W, W2, W3 = w.sum(), w2.sum(), w3.sum()
            pa = w[a] / W
            pb = w2[b] / max(W2 - w2[a], 1e-12)
            pc = w3[c] / max(W3 - w3[a] - w3[b], 1e-12)
            s -= np.log(max(pa * pb * pc, 1e-300))
        return s
    res = minimize(neg_ll, x0=[0.81, 0.65], bounds=[(0.2, 1.5), (0.1, 1.3)], method="L-BFGS-B")
    return float(res.x[0]), float(res.x[1])


def _eval_holdout(booster, races, T, beta, l2, l3):
    """fold C で frozen パラメータの単勝 ROI / 市場 ROI / 3連単H1(K=3) hit を評価。"""
    sw_stake = sw_pay = mk_stake = mk_pay = 0
    sw_hit = mk_hit = 0
    tri_n = tri_hit = 0
    for r in races:
        mp = _softmax(booster.predict(r["X"]), T)
        mk = _devig(r["odds"])
        z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
        z = z - z.max()
        bp = np.exp(z); bp = bp / bp.sum()
        odds = r["odds"]; w = r["winner"]
        # 単勝 (blended top-1)
        top = int(np.argmax(bp)); sw_stake += 100
        if top == w:
            sw_hit += 1; sw_pay += int(100 * odds[w])
        # 市場 top-1
        topm = int(np.argmax(mk)); mk_stake += 100
        if topm == w:
            mk_hit += 1; mk_pay += int(100 * odds[w])
        # 3連単 H1 K=3 (PL with fitted λ)
        win_d = bp; w2 = win_d ** l2; w3 = win_d ** l3
        W, W2, W3 = win_d.sum(), w2.sum(), w3.sum()
        topM = np.argsort(-win_d)[:8]
        cand = []
        for a, b, c in permutations(topM.tolist(), 3):
            pa = win_d[a] / W
            pb = w2[b] / max(W2 - w2[a], 1e-12)
            pc = w3[c] / max(W3 - w3[a] - w3[b], 1e-12)
            cand.append(((a, b, c), pa * pb * pc))
        cand.sort(key=lambda x: -x[1])
        picks = [t for t, _ in cand[:3]]
        tri_n += 1
        if r["order"] in picks:
            tri_hit += 1
    return {
        "n": sw_stake // 100,
        "tansho_roi": (sw_pay / sw_stake * 100) if sw_stake else 0.0,
        "tansho_hit": sw_hit,
        "market_roi": (mk_pay / mk_stake * 100) if mk_stake else 0.0,
        "market_hit": mk_hit,
        "tri_h1k3_hit": tri_hit,
        "tri_h1k3_hitrate": (tri_hit / tri_n * 100) if tri_n else 0.0,
    }


def train_segment(df_all: pd.DataFrame, segment: str) -> dict:
    df = df_all[_seg_mask(df_all, segment)].copy()
    df = df[df["race_id"].isin(
        df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    rids = sorted(df["race_id"].unique().tolist(), key=_race_int)
    n = len(rids)
    A = set(rids[: int(n * 0.60)])
    B = set(rids[int(n * 0.60): int(n * 0.80)])
    C = set(rids[int(n * 0.80):])
    print(f"\n[{segment}] races={n} (A={len(A)} train / B={len(B)} fit / C={len(C)} holdout)", flush=True)

    dfa = df[df["race_id"].isin(A)].sort_values(["race_id", "horse_number"])
    dfb = df[df["race_id"].isin(B)].sort_values(["race_id", "horse_number"])
    dfc = df[df["race_id"].isin(C)].sort_values(["race_id", "horse_number"])

    ga = dfa.groupby("race_id", sort=False).size().to_numpy()
    gb = dfb.groupby("race_id", sort=False).size().to_numpy()
    dtr = lgb.Dataset(dfa[FEATS].values, label=dfa["target_rank"].values, group=ga)
    dva = lgb.Dataset(dfb[FEATS].values, label=dfb["target_rank"].values, group=gb, reference=dtr)
    booster = lgb.train(PARAMS, dtr, num_boost_round=800, valid_sets=[dva],
                        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)])

    races_B = _race_arrays(dfb)
    races_C = _race_arrays(dfc)
    T = _fit_T(booster, races_B)
    beta = _fit_beta(booster, races_B, T)
    l2, l3 = _fit_lambda(booster, races_B, T, beta)
    print(f"  MLE: T={T:.2f}  β={beta:.3f}  λ2={l2:.3f}  λ3={l3:.3f}", flush=True)
    ev = _eval_holdout(booster, races_C, T, beta, l2, l3)
    print(f"  holdout C (n={ev['n']}): 単勝 ROI {ev['tansho_roi']:.1f}% (hit {ev['tansho_hit']}) "
          f"vs 市場 {ev['market_roi']:.1f}% (hit {ev['market_hit']}) | "
          f"3連単H1K3 hit {ev['tri_h1k3_hitrate']:.1f}%", flush=True)

    mp = MODELS / f"lgbm_{segment}.txt"
    booster.save_model(str(mp), num_iteration=booster.best_iteration)
    meta = {
        "segment": segment,
        "feature_cols": FEATS,
        "params": PARAMS,
        "best_iteration": booster.best_iteration,
        "softmax_temperature": T,
        "market_blend_mle": beta,
        "lambda_2_mle": l2,
        "lambda_3_mle": l3,
        "n_train_races": len(A),
        "n_fit_races": len(B),
        "n_holdout_races": len(C),
        "holdout_eval": ev,
    }
    (MODELS / f"lgbm_{segment}_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  saved {mp.name} + metadata", flush=True)
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default="both", choices=["jra", "nar", "all", "both"])
    args = ap.parse_args()
    df = pd.read_parquet(ALL)
    segs = ["jra", "nar"] if args.segment == "both" else [args.segment]
    results = {}
    for seg in segs:
        results[seg] = train_segment(df, seg)
    print("\n=== summary (frozen MLE params, hold-out C eval) ===")
    for seg, m in results.items():
        e = m["holdout_eval"]
        print(f"{seg:>4}: β={m['market_blend_mle']:.3f} λ2={m['lambda_2_mle']:.3f} "
              f"λ3={m['lambda_3_mle']:.3f} T={m['softmax_temperature']:.2f} | "
              f"単勝 {e['tansho_roi']:.1f}% vs 市場 {e['market_roi']:.1f}% (n={e['n']})")
    print("\n注: 全 ROI が 100% 未満なら依然 -EV。β は別 partition(B) で MLE 凍結 = overfit 回避済。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
