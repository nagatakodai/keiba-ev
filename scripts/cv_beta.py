"""validation 291 races を 5 fold に split、各 fold で Plan H1/H2/G の best β を
独立に求めて変動を検証する (Phase 19/20 の β 選択が robust かの確認)。

Phase 19/20/21 で採用した β:
  - Plan A/B/C/単勝/3連単 EV table: β=0.78
  - Plan H1 (確率最優先 3 点):     β=0
  - Plan H2 (確率 +EV≥1):          β=0
  - Plan G  (適性ゲート):           β=1.0

これらは validation set 全体で fit したものなので、5-fold で再現するかを確認。
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.ev import (  # noqa: E402
    DEFAULT_LAMBDA_2,
    DEFAULT_LAMBDA_3,
    LGBM_TEMPERATURE,
    PXO_CHUANA,
    PXO_FLOOR,
    PXO_HONSEN,
    EvRow,
    plan_aptitude_ev,
    plan_balanced,
    plan_hit_pure,
    plan_hit_safe,
    plan_max_ev,
    plan_wide,
    power_method_overround,
)
from src.parse import parse_result  # noqa: E402

DATASETS = ROOT / "data" / "datasets" / "all.parquet"
MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"
RAW_DIR = ROOT / "data" / "raw"
TRIFECTA_DIR = ROOT / "data" / "cache" / "trifecta_odds"
APT_DIR = ROOT / "data" / "cache" / "aptitudes"


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _tier(pxo: float) -> str:
    if pxo < PXO_FLOOR:
        return "minus"
    if pxo <= PXO_HONSEN[1]:
        return "honsen"
    if pxo <= PXO_CHUANA[1]:
        return "chuana"
    return "oana"


def main() -> int:
    meta = json.loads(META.read_text(encoding="utf-8"))
    feature_cols = list(meta["feature_cols"])
    df = pd.read_parquet(DATASETS)
    rids = df["race_id"].unique().tolist()
    rids.sort(key=_race_id_to_int)
    n_valid = max(int(len(rids) * 0.2), 1)
    valid_rids = rids[-n_valid:]
    valid = df[df["race_id"].isin(valid_rids)].copy()
    has_result = (
        valid.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )
    valid = valid[valid["race_id"].isin(has_result)].copy()
    valid = valid.sort_values(["race_id", "horse_number"]).reset_index(drop=True)

    booster = lgb.Booster(model_file=str(MODEL))
    X = valid[feature_cols].astype("float64").fillna(0.0)
    valid["score"] = booster.predict(X.values, num_iteration=booster.best_iteration)

    T = LGBM_TEMPERATURE
    def _race_softmax(s: pd.Series) -> pd.Series:
        scaled = s / T
        m = scaled.max()
        ex = np.exp(scaled - m)
        return ex / ex.sum()
    valid["lgbm_prob"] = valid.groupby("race_id", sort=False)["score"].transform(_race_softmax)

    def _market_prob(g: pd.DataFrame) -> pd.Series:
        raw = {int(r.horse_number): (1.0 / r.win_odds) if r.win_odds and r.win_odds > 0 else 0.0
               for r in g.itertuples()}
        s = sum(raw.values())
        if s > 0:
            raw = {k: v / s for k, v in raw.items()}
        try:
            corrected = power_method_overround(raw)
        except Exception:
            corrected = raw
        s2 = sum(corrected.values())
        if s2 > 0:
            corrected = {k: v / s2 for k, v in corrected.items()}
        return pd.Series([corrected.get(int(r.horse_number), 0.0) for r in g.itertuples()], index=g.index)
    valid["market_prob"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(_market_prob, include_groups=False)
    )

    def _blend(g: pd.DataFrame, beta: float) -> pd.Series:
        alpha = max(1.0 - beta, 0.0)
        logs = []
        for r in g.itertuples():
            f = max(r.lgbm_prob, 1e-9)
            pi = max(r.market_prob, 1e-9)
            logs.append(alpha * math.log(f) + beta * math.log(pi))
        a = np.array(logs)
        ex = np.exp(a - a.max())
        return pd.Series(ex / ex.sum(), index=g.index)

    beta_grid = [round(b, 2) for b in np.arange(0.0, 1.01, 0.05)]
    for beta in beta_grid:
        col = f"blend_{int(beta*100):03d}"
        valid[col] = (
            valid.groupby("race_id", sort=False, group_keys=False)
            .apply(lambda g, b=beta: _blend(g, b), include_groups=False)
        )

    # caches
    payout_cache: dict[str, int] = {}
    for rid in valid["race_id"].unique():
        rp = RAW_DIR / f"{rid}-result.html.gz"
        if not rp.exists():
            continue
        try:
            html = gzip.open(rp, "rt", encoding="utf-8").read()
            parsed = parse_result(html)
        except Exception:
            continue
        if parsed and parsed.get("payout"):
            payout_cache[rid] = int(parsed["payout"])

    odds_cache: dict[str, dict[tuple, tuple[float, int]]] = {}
    for j in TRIFECTA_DIR.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        rid = d.get("race_id") or j.stem
        mp = {}
        for t in d.get("trifecta") or []:
            key = tuple(t["key"])
            if len(key) != 3:
                continue
            try:
                mp[(int(key[0]), int(key[1]), int(key[2]))] = (float(t["odds"]), int(t.get("popularity") or 0))
            except Exception:
                continue
        odds_cache[rid] = mp

    apt_cache: dict[str, list[int]] = {}
    for j in APT_DIR.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        rid = d.get("race_id") or j.stem
        apt_cache[rid] = [int(h) for h in d.get("aptitude_top_horses") or []]

    rids_in_play = sorted(
        [r for r in valid["race_id"].unique() if r in odds_cache and r in payout_cache],
        key=_race_id_to_int,
    )

    def plan_roi(rids_sub: list[str], col: str, plan_name: str) -> tuple[float, int]:
        stake = 0
        payout = 0
        hits = 0
        for rid in rids_sub:
            g = valid[valid["race_id"] == rid]
            n = len(g)
            if n < 3:
                continue
            horse_numbers = g["horse_number"].to_numpy().astype(int)
            show = g["shrunk_show_rate"].to_numpy()
            avg_show = float(np.mean(show)) if n > 0 else 0.0
            bias = np.clip(show / avg_show, 0.1, None) if avg_show > 0 else np.ones(n)
            w = np.maximum(g[col].to_numpy(), 1e-12)
            s1 = w
            s2 = (s1 ** DEFAULT_LAMBDA_2) * bias
            s3 = (s1 ** DEFAULT_LAMBDA_3) * bias
            S1 = float(s1.sum()); S2 = float(s2.sum()); S3 = float(s3.sum())
            if S1 <= 0 or S2 <= 0 or S3 <= 0:
                continue
            w1 = s1 / S1
            w2 = s2[None, :] / np.maximum(S2 - s2[:, None], 1e-12)
            np.fill_diagonal(w2, 0.0)
            w3 = s3[None, None, :] / np.maximum(S3 - s3[:, None, None] - s3[None, :, None], 1e-12)
            for ii in range(n):
                w3[ii, ii, :] = 0.0
                w3[ii, :, ii] = 0.0
                w3[:, ii, ii] = 0.0
            p_cube = w1[:, None, None] * w2[:, :, None] * w3

            race_odds = odds_cache[rid]
            ev_rows: list[EvRow] = []
            for i in range(n):
                for jj in range(n):
                    if jj == i: continue
                    for k in range(n):
                        if k == i or k == jj: continue
                        key = (int(horse_numbers[i]), int(horse_numbers[jj]), int(horse_numbers[k]))
                        e = race_odds.get(key)
                        if e is None: continue
                        odds, popu = e
                        if odds <= 0: continue
                        p = float(p_cube[i, jj, k])
                        pxo = p * odds
                        ev_rows.append(EvRow(key=key, odds=odds, popularity=popu, prob=p, px_o=pxo, tier=_tier(pxo)))
            if not ev_rows:
                continue
            ev_rows.sort(key=lambda r: r.px_o, reverse=True)

            finish = g["finish_pos"].to_numpy()
            a_idx = np.where(finish == 1.0)[0]
            b_idx = np.where(finish == 2.0)[0]
            c_idx = np.where(finish == 3.0)[0]
            if len(a_idx) != 1 or len(b_idx) != 1 or len(c_idx) != 1:
                continue
            actual_key = (int(horse_numbers[a_idx[0]]), int(horse_numbers[b_idx[0]]), int(horse_numbers[c_idx[0]]))
            real_payout = payout_cache[rid]

            apt_top = apt_cache.get(rid, [])
            if plan_name == "H1":
                picks = plan_hit_pure(ev_rows, target=3)
            elif plan_name == "H2":
                picks = plan_hit_safe(ev_rows, target=3)
            elif plan_name == "G":
                picks = plan_aptitude_ev(ev_rows, apt_top) if apt_top else []
            elif plan_name == "A":
                picks = plan_balanced(ev_rows)
            elif plan_name == "C":
                picks = plan_wide(ev_rows)
            else:
                # 将来 Plan B 等を追加した時に silent 0 picks にならないよう明示 error。
                raise ValueError(f"unknown plan_name: {plan_name!r}")
            n_pts = len(picks)
            if n_pts == 0:
                continue
            stake += n_pts * 100
            if any(r.key == actual_key for r in picks):
                payout += real_payout
                hits += 1
        return ((payout / stake) if stake else 0.0, hits)

    print(f"n races in play: {len(rids_in_play)}, T={T}")
    print()

    n_folds = 5
    fold_size = len(rids_in_play) // n_folds
    print(f"{'plan':>4} {'fold':>4} {'best_β':>7} {'fit_ROI':>9} {'hold_ROI':>10} {'hold_hit':>9}")
    print("-" * 50)

    for plan_name in ("H1", "H2", "G"):
        fold_bestbetas = []
        fold_hold_rois = []
        for fi in range(n_folds):
            lo = fi * fold_size
            hi = (fi + 1) * fold_size if fi < n_folds - 1 else len(rids_in_play)
            hold_rids = rids_in_play[lo:hi]
            fit_rids = rids_in_play[:lo] + rids_in_play[hi:]
            best_beta = 0.0
            best_roi = -1.0
            for beta in beta_grid:
                col = f"blend_{int(beta*100):03d}"
                roi, _h = plan_roi(fit_rids, col, plan_name)
                if roi > best_roi:
                    best_roi = roi
                    best_beta = beta
            col = f"blend_{int(best_beta*100):03d}"
            hold_roi, hold_hit = plan_roi(hold_rids, col, plan_name)
            fold_bestbetas.append(best_beta)
            fold_hold_rois.append(hold_roi)
            print(f"{plan_name:>4} {fi:>4d} {best_beta:>7.2f} {best_roi*100:>8.1f}% {hold_roi*100:>9.1f}% {hold_hit:>9d}")
        print(
            f"  mean β = {np.mean(fold_bestbetas):.3f} (std {np.std(fold_bestbetas):.3f}), "
            f"mean hold ROI = {np.mean(fold_hold_rois)*100:.1f}%"
        )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
