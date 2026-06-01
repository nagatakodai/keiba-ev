"""NAR ダート限定で「人気/オッズを完全無視して速度図表に従う」戦略を検証する。

選抜は pre-race の図表のみ (leakage 無し)、払戻は確定 win_odds (≈ settled)。
比較として「市場1番人気」も出す。bootstrap 95%CI 付き。判定: ROI 95%CI 下限>100% で +EV。

使い方: python scripts/nar_dirt_speed_strategy.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
V2 = ROOT / "data" / "datasets" / "v2_features.parquet"
NAR_MODEL = ROOT / "data" / "models" / "lgbm_nar.txt"
JRA = {f"{i:02d}" for i in range(1, 11)}


def _roi_ci(profit, stake, n=4000, seed=12345):
    profit, stake = np.asarray(profit, float), np.asarray(stake, float)
    if len(profit) == 0 or stake.sum() == 0:
        return 0.0, (0.0, 0.0), 0
    roi = profit.sum() / stake.sum() * 100
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(profit), size=(n, len(profit)))
    r = np.where(stake[idx].sum(1) > 0, profit[idx].sum(1) / stake[idx].sum(1) * 100, 0)
    return roi, (float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))), int((profit > 0).sum())


def main() -> int:
    df = pd.read_parquet(ALL)
    df = df.merge(pd.read_parquet(V2), on=["race_id", "horse_number"], how="left")
    isj = df.race_id.astype(str).str[4:6].isin(JRA)
    df = df[(~isj) & (df["surface"] == "ダート")].copy()
    df = df[df.race_id.isin(df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    booster = lgb.Booster(model_file=str(NAR_MODEL))
    nmeta = json.loads((ROOT / "data" / "models" / "lgbm_nar_metadata.json").read_text())
    df["model_score"] = booster.predict(df[nmeta["feature_cols"]].values)

    print(f"=== NAR dart speed-figure (ignore odds) — races={df.race_id.nunique():,} ===")
    print("  select by figure only, payout at win_odds. +EV = ROI 95%CI lower > 100%.\n")

    selectors = {
        "market favorite": lambda g: g["win_odds"].idxmin(),
        "speed_v2_best": lambda g: g["speed_v2_best"].idxmax(),
        "speed_v2_wavg": lambda g: g["speed_v2_wavg"].idxmax(),
        "speed_idx_best": lambda g: g["speed_idx_best"].idxmax(),
        "speed_idx_weighted": lambda g: g["speed_idx_weighted"].idxmax(),
        "model_score(NAR,b=0)": lambda g: g["model_score"].idxmax(),
    }
    print(f"{'strategy':<22} {'ROI':>7} {'95%CI':>16} {'hit/n':>12}")
    print("-" * 62)
    for name, sel in selectors.items():
        prof, stake = [], []
        for _rid, g in df.groupby("race_id", sort=False):
            g = g[g["win_odds"] > 0]
            if len(g) < 3:
                continue
            row = g.loc[sel(g)]
            stake.append(100)
            prof.append(int(100 * row["win_odds"]) if row["target_top1"] == 1 else 0)
        roi, (lo, hi), hit = _roi_ci(prof, stake)
        flag = " <-+EV" if lo > 100 else ""
        print(f"{name:<22} {roi:>6.1f}% [{lo:>6.1f},{hi:>6.1f}] {hit:>5}/{len(prof):<5}{flag}")

    print("\n=== speed_v2_best top-1 != market favorite only (overlay) ===")
    prof, stake, agree = [], [], 0
    for _rid, g in df.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 3:
            continue
        fig_top, fav = g["speed_v2_best"].idxmax(), g["win_odds"].idxmin()
        if fig_top == fav:
            agree += 1
            continue
        row = g.loc[fig_top]
        stake.append(100)
        prof.append(int(100 * row["win_odds"]) if row["target_top1"] == 1 else 0)
    roi, (lo, hi), hit = _roi_ci(prof, stake)
    print(f"  disagree only ({len(prof)} races, {agree} agree excluded): ROI {roi:.1f}% "
          f"95%CI[{lo:.1f},{hi:.1f}] hit {hit}/{len(prof)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
