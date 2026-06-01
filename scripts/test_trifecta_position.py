"""位置別(1着/2着/3着)モデルで 3連単を組み NAR ダートでバックテスト。
P(1着)/P(2着)/P(3着) を別々に binary 学習 → P(a,b,c)=p1[a]*p2[b]*p3[c]。
比較: 純モデル win の Harville 3連単。OOS(last20%) で確定払戻 ROI + bootstrap CI。
使い方: python scripts/test_trifecta_position.py
"""
from __future__ import annotations
import json, sys
from itertools import permutations
from pathlib import Path
import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.ev import DEFAULT_LAMBDA_2, DEFAULT_LAMBDA_3  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
SETTLED = ROOT / "data" / "datasets" / "settled_odds.parquet"
FEATS = json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())["feature_cols"]
JRA = {f"{i:02d}" for i in range(1, 11)}
TOPM = 8


def _ri(rid):
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _binary(da, db, label):
    p = {"objective": "binary", "metric": "binary_logloss", "learning_rate": 0.03,
         "num_leaves": 24, "min_data_in_leaf": 20, "feature_fraction": 0.9,
         "bagging_fraction": 0.8, "bagging_freq": 5, "verbose": -1}
    dtr = lgb.Dataset(da[FEATS].values, label=da[label].values)
    dva = lgb.Dataset(db[FEATS].values, label=db[label].values, reference=dtr)
    return lgb.train(p, dtr, num_boost_round=800, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(80, verbose=False), lgb.log_evaluation(0)])


def _roi_ci(prof, stake, seed=12345, n=4000):
    prof, stake = np.asarray(prof, float), np.asarray(stake, float)
    if stake.sum() == 0:
        return 0.0, (0.0, 0.0), 0
    roi = prof.sum() / stake.sum() * 100
    rng = np.random.default_rng(seed); idx = rng.integers(0, len(prof), size=(n, len(prof)))
    r = prof[idx].sum(1) / stake[idx].sum(1) * 100
    return roi, (float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))), int((prof > 0).sum())


def main():
    df = pd.read_parquet(ALL)
    isj = df.race_id.astype(str).str[4:6].isin(JRA)
    df = df[(~isj) & (df["surface"] == "ダート")].copy()
    df = df[df.race_id.isin(df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    df["t1"] = (df["finish_pos"] == 1).astype(int)
    df["t2"] = (df["finish_pos"] == 2).astype(int)
    df["t3"] = (df["finish_pos"] == 3).astype(int)
    rids = sorted(df.race_id.unique().tolist(), key=_ri)
    n = len(rids)
    A = set(rids[:int(n * .6)]); B = set(rids[int(n * .6):int(n * .8)]); C = set(rids[int(n * .8):])
    da, db, dc = (df[df.race_id.isin(s)] for s in (A, B, C))
    print(f"=== NAR dirt position trifecta vs win-Harville (OOS C) races={n} C={len(C)} ===")

    m1, m2, m3 = (_binary(da, db, lab) for lab in ("t1", "t2", "t3"))

    settled = pd.read_parquet(SETTLED)
    tri = {}
    for rid, bt, key, odds in settled.itertuples(index=False):
        if bt == "trifecta":
            tri[rid] = (key, odds)

    Ks = [3, 6, 12, 24]
    pos = {k: ([], []) for k in Ks}
    har = {k: ([], []) for k in Ks}
    for rid, g in dc.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 4 or rid not in tri:
            continue
        nums = g["horse_number"].to_numpy()
        fp = g.set_index("horse_number")["finish_pos"]
        order = fp[fp.isin([1, 2, 3])].sort_values()
        if len(order) < 3:
            continue
        win_triple = tuple(int(x) for x in order.index[:3])
        tkey, todds = tri[rid]
        if tkey != "-".join(map(str, win_triple)):
            continue
        p1 = np.clip(m1.predict(g[FEATS].values), 1e-9, 1)
        p2 = np.clip(m2.predict(g[FEATS].values), 1e-9, 1)
        p3 = np.clip(m3.predict(g[FEATS].values), 1e-9, 1)
        d1 = {int(nums[i]): p1[i] for i in range(len(nums))}
        d2 = {int(nums[i]): p2[i] for i in range(len(nums))}
        d3 = {int(nums[i]): p3[i] for i in range(len(nums))}
        w = dict(d1)
        w2 = {k: v ** DEFAULT_LAMBDA_2 for k, v in w.items()}
        w3 = {k: v ** DEFAULT_LAMBDA_3 for k, v in w.items()}
        top = sorted(d1, key=lambda k: -d1[k])[:TOPM]
        W, W2, W3 = sum(w.values()), sum(w2.values()), sum(w3.values())
        pos_probs, har_probs = [], []
        for a, b, c in permutations(top, 3):
            pos_probs.append(((a, b, c), d1[a] * d2[b] * d3[c]))
            ha = w[a] / W
            hb = w2[b] / max(W2 - w2[a], 1e-9)
            hc = w3[c] / max(W3 - w3[a] - w3[b], 1e-9)
            har_probs.append(((a, b, c), ha * hb * hc))
        pos_probs.sort(key=lambda x: -x[1]); har_probs.sort(key=lambda x: -x[1])
        for K in Ks:
            for store, probs in ((pos, pos_probs), (har, har_probs)):
                picks = [t for t, _ in probs[:K]]
                store[K][1].append(100 * K)
                store[K][0].append(int(100 * todds) if win_triple in picks else 0)

    print(f"{'K':>4} | {'position ROI':>24} | {'win-Harville ROI':>24}")
    for K in Ks:
        rp, (lp, hp), hitp = _roi_ci(*pos[K])
        rh, (lh, hh), hith = _roi_ci(*har[K])
        fp = " +EV" if lp > 100 else ""
        print(f"{K:>4} | {rp:6.1f}% [{lp:5.1f},{hp:6.1f}] h{hitp:<3}{fp:4} | {rh:6.1f}% [{lh:5.1f},{hh:6.1f}] h{hith}")
    print("\n注: 確定3連単払戻。ROI 95%CI 下限>100% で +EV。位置別が Harville を有意に上回れば")
    print("    『1/2/3着を別々に判断』が効く証拠。両方 -EV なら市場が織込済。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
