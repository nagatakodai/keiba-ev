"""top-3 を直接学習したモデルが市場(win オッズ由来)を上回るか beta-MLE で検証。
3-fold: A学習 / B で beta-MLE / C hold-out。使い方: python scripts/test_position_prob.py [nar|jra|all]
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.ev import power_method_overround  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
FEATS = json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())["feature_cols"]
JRA = {f"{i:02d}" for i in range(1, 11)}
LAM3 = 0.61


def _ri(rid):
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _market_top3(odds):
    raw = 1.0 / np.asarray(odds, float); raw = raw / raw.sum()
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    w = np.array([d[i] for i in range(len(raw))], float); w = w / w.sum()
    s = w ** LAM3; p3 = s / s.sum() * 3.0
    return np.clip(p3, 1e-6, 1 - 1e-6)


def main():
    seg = sys.argv[1] if len(sys.argv) > 1 else "nar"
    df = pd.read_parquet(ALL)
    isj = df.race_id.astype(str).str[4:6].isin(JRA)
    if seg == "nar":
        df = df[(~isj) & (df["surface"] == "ダート")].copy()
    elif seg == "jra":
        df = df[isj].copy()
    df = df[df.race_id.isin(df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    rids = sorted(df.race_id.unique().tolist(), key=_ri)
    n = len(rids)
    A = set(rids[:int(n * .6)]); B = set(rids[int(n * .6):int(n * .8)]); C = set(rids[int(n * .8):])
    da, db, dc = (df[df.race_id.isin(s)] for s in (A, B, C))
    print(f"=== top-3 direct model vs market [{seg}] races={n} (A{len(A)}/B{len(B)}/C{len(C)}) ===")
    params = {"objective": "binary", "metric": "binary_logloss", "learning_rate": 0.03,
              "num_leaves": 24, "min_data_in_leaf": 20, "feature_fraction": 0.9,
              "bagging_fraction": 0.8, "bagging_freq": 5, "verbose": -1}
    dtr = lgb.Dataset(da[FEATS].values, label=da["target_top3"].values)
    dva = lgb.Dataset(db[FEATS].values, label=db["target_top3"].values, reference=dtr)
    booster = lgb.train(params, dtr, num_boost_round=800, valid_sets=[dva],
                        callbacks=[lgb.early_stopping(80, verbose=False), lgb.log_evaluation(0)])

    def arrays(d):
        out = []
        for _r, g in d.groupby("race_id", sort=False):
            g = g[g["win_odds"] > 0]
            if len(g) < 4:
                continue
            mp = np.clip(booster.predict(g[FEATS].values), 1e-6, 1 - 1e-6)
            kp = _market_top3(g["win_odds"].to_numpy())
            y = g["target_top3"].to_numpy().astype(float)
            out.append((mp, kp, y))
        return out

    rb, rc = arrays(db), arrays(dc)

    def bern_ll(beta, races):
        s = 0.0
        for mp, kp, y in races:
            p = np.clip((1 - beta) * mp + beta * kp, 1e-9, 1 - 1e-9)
            s -= np.sum(y * np.log(p) + (1 - y) * np.log(1 - p))
        return s
    beta = float(minimize_scalar(lambda b: bern_ll(b, rb), bounds=(0, 1), method="bounded").x)

    def ll(races, mode):
        s = nn = 0.0
        for mp, kp, y in races:
            p = {"model": mp, "market": kp, "blend": (1 - beta) * mp + beta * kp}[mode]
            p = np.clip(p, 1e-9, 1 - 1e-9)
            s -= np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)); nn += len(y)
        return s / nn
    print(f"  beta-MLE (top3, B) = {beta:.3f}  (1.0 = model cannot beat market)")
    print(f"  hold-out C log-loss: model {ll(rc,'model'):.4f} / market {ll(rc,'market'):.4f} / blend {ll(rc,'blend'):.4f}")
    print("  ->", "model beats market (beta<1)" if beta < 0.9 else "no edge (beta~1 = top3 is market copy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
