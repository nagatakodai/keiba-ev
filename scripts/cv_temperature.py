"""LGBM softmax 温度 T を validation set 5-fold CV で robust に推定する。

Phase 21 で T=0.4 を採用したが、これは validation 291 races そのもので
log loss 最小化した in-sample fit だった。本スクリプトは validation を
5 つの chronological fold に分け、各 fold で残り 4 fold (= 232 races) を
fit set として T_best を求め、hold-out fold で metric を評価する。

各 fold の T_best が安定しているか (平均と分散) を見ることで、
production 既定 T=0.4 の robustness を確認する。
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


DATASETS = ROOT / "data" / "datasets" / "all.parquet"
MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _get_valid(valid_frac: float = 0.2) -> pd.DataFrame:
    df = pd.read_parquet(DATASETS)
    rids = df["race_id"].unique().tolist()
    rids.sort(key=_race_id_to_int)
    n_valid = max(int(len(rids) * valid_frac), 1)
    valid_rids = rids[-n_valid:]
    v = df[df["race_id"].isin(valid_rids)]
    has_result = v.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    return v[v["race_id"].isin(has_result)].copy()


def _race_softmax_with_T(scores: pd.Series, T: float) -> pd.Series:
    scaled = scores / max(T, 1e-3)
    m = scaled.max()
    ex = np.exp(scaled - m)
    return ex / ex.sum()


def main() -> int:
    meta = json.loads(META.read_text(encoding="utf-8"))
    feature_cols = list(meta["feature_cols"])
    valid = _get_valid()
    valid = valid.sort_values(["race_id", "horse_number"]).reset_index(drop=True)

    booster = lgb.Booster(model_file=str(MODEL))
    X = valid[feature_cols].astype("float64").fillna(0.0)
    valid["score"] = booster.predict(X.values, num_iteration=booster.best_iteration)

    rids = sorted(valid["race_id"].unique().tolist(), key=_race_id_to_int)
    n_folds = 5
    fold_size = len(rids) // n_folds

    t_grid = [round(t, 2) for t in np.arange(0.10, 1.51, 0.05)]

    def log_loss_for_T(sub: pd.DataFrame, T: float) -> float:
        s = sub.copy()
        s["p"] = s.groupby("race_id", sort=False)["score"].transform(
            lambda x, t=T: _race_softmax_with_T(x, t)
        )
        ll_sum = 0.0
        n_r = 0
        for _rid, g in s.groupby("race_id", sort=False):
            w = g[g["target_top1"] == 1]
            if len(w) != 1:
                continue
            p = float(w["p"].iloc[0])
            ll_sum += -math.log(max(p, 1e-12))
            n_r += 1
        return ll_sum / n_r if n_r else 0.0

    print(f"validation races: {len(rids)}, folds: {n_folds}, T grid {t_grid[0]}-{t_grid[-1]}")
    print()
    print(f"{'fold':>4} {'fit_n':>6} {'eval_n':>6} {'T_best':>7} {'fit_ll':>9} {'eval_ll_T_best':>15} {'eval_ll_T_1':>12}")
    print("-" * 70)

    t_bests: list[float] = []
    fit_lls: list[float] = []
    eval_ll_bests: list[float] = []
    eval_ll_1s: list[float] = []

    for fi in range(n_folds):
        lo = fi * fold_size
        hi = (fi + 1) * fold_size if fi < n_folds - 1 else len(rids)
        eval_rids = set(rids[lo:hi])
        fit_rids = set(rids[:lo] + rids[hi:])
        fit_sub = valid[valid["race_id"].isin(fit_rids)]
        eval_sub = valid[valid["race_id"].isin(eval_rids)]

        best_T = 1.0
        best_ll = float("inf")
        for T in t_grid:
            ll = log_loss_for_T(fit_sub, T)
            if ll < best_ll:
                best_ll = ll
                best_T = T
        eval_ll_best = log_loss_for_T(eval_sub, best_T)
        eval_ll_1 = log_loss_for_T(eval_sub, 1.0)

        t_bests.append(best_T)
        fit_lls.append(best_ll)
        eval_ll_bests.append(eval_ll_best)
        eval_ll_1s.append(eval_ll_1)

        print(
            f"{fi:>4d} {len(fit_rids):>6d} {len(eval_rids):>6d} "
            f"{best_T:>7.2f} {best_ll:>9.4f} {eval_ll_best:>15.4f} {eval_ll_1:>12.4f}"
        )

    print("-" * 70)
    mean_T = float(np.mean(t_bests))
    std_T = float(np.std(t_bests))
    mean_eval_best = float(np.mean(eval_ll_bests))
    mean_eval_1 = float(np.mean(eval_ll_1s))
    print(f"T_best: mean={mean_T:.3f}, std={std_T:.3f}, values={t_bests}")
    print(
        f"eval log loss: at T_best mean={mean_eval_best:.4f}, "
        f"at T=1 mean={mean_eval_1:.4f}, improvement={mean_eval_1-mean_eval_best:+.4f}"
    )
    print()
    print(
        "解釈: T_best が安定 (std 小) かつ平均 ~0.4 なら Phase 21 既定 (T=0.4) は robust。"
        " eval ll improvement が正なら out-of-sample でも T_best が log loss を下げている。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
