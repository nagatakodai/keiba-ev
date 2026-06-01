"""v2 速度図表が「市場を OOS で上回る (β-MLE < 1.0)」かを検証する。

β を別 partition で正しく MLE 推定すると、モデルが市場に何も足せなければ β=1.0 (純市場)、
市場を上回る直交情報があれば β<1.0 になる (= モデル重みが立つ)。baseline (既存特徴量) と
v2 (既存 + speed_v2/pace_v2/trip) で β を比べ、v2 が β を 1.0 から下げれば本物の edge。

3-fold: A=[0,60%) 学習 / B=[60,80%) で T・β を MLE / C=[80,100%) 完全 hold-out で評価。

使い方: python scripts/test_speed_v2.py [--segment all|nar|jra]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ev import power_method_overround  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
V2 = ROOT / "data" / "datasets" / "v2_features.parquet"
META = json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())
BASE_FEATS = META["feature_cols"]
V2_FEATS = ["speed_v2_wavg", "speed_v2_best", "pace_v2_wavg", "trip_gain_avg", "front_rate", "v2_n_runs"]
PARAMS = dict(META["params"])
JRA = {f"{i:02d}" for i in range(1, 11)}


def _ri(rid):
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _softmax(x, t):
    z = np.asarray(x) / max(t, 1e-6); z = z - z.max(); e = np.exp(z); return e / e.sum()


def _devig(odds):
    raw = 1.0 / np.asarray(odds, float); raw = raw / raw.sum()
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    v = np.array([d[i] for i in range(len(raw))], float); s = v.sum()
    return v / s if s > 0 else raw


def _races(df, feats):
    out = []
    for _rid, g in df.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 3 or g["target_top1"].sum() != 1:
            continue
        out.append({
            "X": g[feats].values,
            "odds": g["win_odds"].to_numpy(float),
            "winner": int(np.argmax(g["target_top1"].to_numpy())),
        })
    return out


def _fit_T(b, races):
    best_T, best = 0.5, 1e18
    for T in [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.5]:
        ll = sum(-np.log(max(_softmax(b.predict(r["X"]), T)[r["winner"]], 1e-12)) for r in races)
        if ll < best:
            best, best_T = ll, T
    return best_T


def _fit_beta(b, races, T):
    pre = [(_softmax(b.predict(r["X"]), T), _devig(r["odds"]), r["winner"]) for r in races]

    def neg(beta):
        s = 0.0
        for mp, mk, w in pre:
            z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
            z = z - z.max(); e = np.exp(z); bp = e / e.sum()
            s -= np.log(max(bp[w], 1e-12))
        return s
    return float(minimize_scalar(neg, bounds=(0, 1), method="bounded").x)


def _eval(b, races, T, beta):
    sw_p = sw_s = mk_p = mk_s = sw_hit = ll = 0.0
    for r in races:
        mp = _softmax(b.predict(r["X"]), T); mk = _devig(r["odds"])
        z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
        z = z - z.max(); bp = np.exp(z); bp = bp / bp.sum()
        w = r["winner"]; odds = r["odds"]
        ll -= np.log(max(bp[w], 1e-12))
        top = int(np.argmax(bp)); sw_s += 100
        if top == w:
            sw_hit += 1; sw_p += 100 * odds[w]
        tm = int(np.argmax(mk)); mk_s += 100
        if tm == w:
            mk_p += 100 * odds[w]
    n = len(races)
    return {"roi": sw_p / sw_s * 100 if sw_s else 0, "hit": int(sw_hit), "n": n,
            "ll": ll / max(n, 1), "mkt_roi": mk_p / mk_s * 100 if mk_s else 0}


def run(df, feats, label):
    rids = sorted(df["race_id"].unique().tolist(), key=_ri)
    n = len(rids)
    A = set(rids[:int(n * .6)]); B = set(rids[int(n * .6):int(n * .8)]); C = set(rids[int(n * .8):])
    da = df[df.race_id.isin(A)].sort_values(["race_id", "horse_number"])
    db = df[df.race_id.isin(B)].sort_values(["race_id", "horse_number"])
    dc = df[df.race_id.isin(C)].sort_values(["race_id", "horse_number"])
    ga = da.groupby("race_id", sort=False).size().to_numpy()
    gb = db.groupby("race_id", sort=False).size().to_numpy()
    dtr = lgb.Dataset(da[feats].values, label=da["target_rank"].values, group=ga)
    dva = lgb.Dataset(db[feats].values, label=db["target_rank"].values, group=gb, reference=dtr)
    b = lgb.train(PARAMS, dtr, num_boost_round=800, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)])
    rb, rc = _races(db, feats), _races(dc, feats)
    T = _fit_T(b, rb); beta = _fit_beta(b, rb, T)
    ev = _eval(b, rc, T, beta)
    print(f"  {label:>9}: β-MLE={beta:.3f} T={T:.2f} | holdout 単勝 {ev['roi']:.1f}% "
          f"vs 市場 {ev['mkt_roi']:.1f}% (hit {ev['hit']}/{ev['n']}, ll {ev['ll']:.3f})", flush=True)
    return beta, ev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default="all", choices=["all", "nar", "jra"])
    args = ap.parse_args()
    df = pd.read_parquet(ALL)
    v2 = pd.read_parquet(V2)
    df = df.merge(v2, on=["race_id", "horse_number"], how="left")
    for c in V2_FEATS:
        df[c] = df[c].fillna(0.0)
    if args.segment != "all":
        isj = df.race_id.astype(str).str[4:6].isin(JRA)
        df = df[isj if args.segment == "jra" else ~isj]
    df = df[df.race_id.isin(df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    print(f"=== speed v2 検証 [{args.segment}] races={df.race_id.nunique():,} ===")
    print("  β-MLE が 1.0 = モデルは市場に何も足せない。1.0 未満 = 直交情報で市場を上回る。\n")
    bb, _ = run(df, BASE_FEATS, "baseline")
    bv, _ = run(df, BASE_FEATS + V2_FEATS, "v2")
    print(f"\n  → β: baseline {bb:.3f} → v2 {bv:.3f}  ({'改善 (市場超え方向)' if bv < bb - 0.02 else '変化なし = v2 は edge にならず'})")
    print("  注: β が 1.0 に張り付いたままなら、v2 図表も市場に織り込み済 = 本物の edge ではない。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
