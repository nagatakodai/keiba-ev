"""既存 LGBM model から feature importance と softmax temperature を抽出して
metadata.json に追記する。

train.py の最新版は metadata に `top_features_by_gain` / `softmax_temperature` を
保存するが、本リポジトリの現 production model は metadata 更新前に保存された
もの。再訓練せずに既存 model + 既存 dataset から計算して metadata を補強する。

使い方:
  python scripts/augment_metadata.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"
DATASET = ROOT / "data" / "datasets" / "all.parquet"

NON_FEATURE_COLS = {
    "race_id", "race_date", "venue", "race_no", "distance", "surface", "going",
    "horse_number", "n_horses",
    "finish_pos", "target_top1", "target_top3", "target_rank",
    "win_odds", "absent",
}


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _fit_temperature(booster, valid_df, feature_cols) -> tuple[float, float]:
    """valid set で softmax(score/T) の log loss を最小化する T を sweep。"""
    v = valid_df[valid_df["target_top1"].notna()].copy()
    v = v.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    v = v[v["race_id"].isin(
        v.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )].copy()
    X = v[feature_cols].astype("float64").fillna(0.0)
    v["_score"] = booster.predict(X.values, num_iteration=booster.best_iteration)
    t_grid = [round(t, 2) for t in np.arange(0.20, 2.51, 0.05)]
    best_T = 1.0
    best_ll = float("inf")
    for T in t_grid:
        ll_sum = 0.0
        n_r = 0
        for _rid, g in v.groupby("race_id", sort=False):
            scaled = g["_score"].to_numpy() / T
            m = scaled.max()
            exps = np.exp(scaled - m)
            probs = exps / exps.sum()
            winner_mask = (g["target_top1"] == 1).to_numpy()
            if winner_mask.sum() != 1:
                continue
            p = float(probs[winner_mask][0])
            ll_sum += -math.log(max(p, 1e-12))
            n_r += 1
        if n_r == 0:
            continue
        ll = ll_sum / n_r
        if ll < best_ll:
            best_ll = ll
            best_T = T
    return best_T, best_ll


def main() -> int:
    if not MODEL.exists() or not META.exists():
        print(f"model or meta not found: {MODEL} / {META}")
        return 1
    meta = json.loads(META.read_text(encoding="utf-8"))
    feature_cols = meta.get("feature_cols", [])
    if not feature_cols:
        print("metadata に feature_cols が無い")
        return 1
    booster = lgb.Booster(model_file=str(MODEL))

    # 1) feature importance
    gains = booster.feature_importance(importance_type="gain")
    pairs = sorted(zip(feature_cols, gains), key=lambda x: -x[1])
    top = [{"name": n, "gain": float(g)} for n, g in pairs[:10]]
    meta["top_features_by_gain"] = top

    # 2) softmax temperature (valid set sweep)
    if DATASET.exists():
        df = pd.read_parquet(DATASET)
        rids = sorted(df["race_id"].unique().tolist(), key=_race_id_to_int)
        n_valid = max(int(len(rids) * 0.2), 1)
        valid_rids = set(rids[-n_valid:])
        valid_df = df[df["race_id"].isin(valid_rids)].copy()
        best_T, best_ll = _fit_temperature(booster, valid_df, feature_cols)
        meta["softmax_temperature"] = best_T
        meta["softmax_temperature_valid_log_loss"] = best_ll
        print(f"calibrated softmax_temperature: T={best_T} (valid log loss {best_ll:.4f})")
    else:
        print("dataset not found, skipping T calibration")

    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"updated {META}:")
    print("  top 10 features by gain:")
    for f in top:
        print(f"    {f['name']:30s} gain={f['gain']:10.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
