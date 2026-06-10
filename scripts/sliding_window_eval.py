"""Sliding-window time-series CV で Phase 19-22 の finding を独立検証する。

現状の eval (make holdout) は単一の chronological split (train 0-80%, valid 80-100%)
で全 β / T / Plan を選んでいる。CV (cv_beta.py / cv_temperature.py) は同じ valid
内で fold を切り出すだけなので、LGBM モデルそのものは変わらない。

本スクリプトは **異なる LGBM** を訓練して独立検証する:
  - Window 3 (現状): train 0-80%, valid 80-100%  (n_valid ≒ 291)
  - Window 4 (新規): train 0-90%, valid 90-100% (n_valid ≒ 146)

新規モデルを訓練、production 設定で Plan G/H1/H2/A/B/C を eval、ROI を出す。
trifecta odds / aptitudes は既にキャッシュ済みなので追加 scrape 不要。

使い方:
  python scripts/sliding_window_eval.py --valid-frac 0.10
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.ev import (  # noqa: E402
    BLEND_APTITUDE_GATE,
    BLEND_DEFAULT,
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
RAW_DIR = ROOT / "data" / "raw"
TRIFECTA_DIR = ROOT / "data" / "cache" / "trifecta_odds"
APT_DIR = ROOT / "data" / "cache" / "aptitudes"


NON_FEATURE_COLS = {
    "race_id", "venue", "race_no", "distance", "surface", "going",
    "horse_number", "n_horses",
    "finish_pos", "target_top1", "target_top3", "target_rank",
    "win_odds",
    "absent",
}


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


def _train_lgbm(train_df: pd.DataFrame, *, internal_valid_frac: float = 0.15):
    """train_df をさらに内部 train/valid に切り、early stopping 付きで訓練。"""
    df = train_df[train_df["target_top1"].notna()].copy()
    df = df.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    races_with_result = df.groupby("race_id")["target_top1"].sum().reset_index(name="ones")
    keep = races_with_result[races_with_result["ones"] > 0]["race_id"].tolist()
    df = df[df["race_id"].isin(keep)].copy()

    # 内部 train/valid split (時系列順、後ろを valid に)
    inner_rids = df["race_id"].unique().tolist()
    inner_rids.sort(key=_race_id_to_int)
    n_inner_valid = max(int(len(inner_rids) * internal_valid_frac), 1)
    inner_train_rids = set(inner_rids[:-n_inner_valid])
    inner_valid_rids = set(inner_rids[-n_inner_valid:])
    train_df_i = df[df["race_id"].isin(inner_train_rids)].sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    valid_df_i = df[df["race_id"].isin(inner_valid_rids)].sort_values(["race_id", "horse_number"]).reset_index(drop=True)

    feature_cols = [c for c in df.columns if c not in NON_FEATURE_COLS]
    Xt = train_df_i[feature_cols].astype("float64").fillna(0.0)
    yt = train_df_i["target_rank"].astype("int32")
    gt = train_df_i.groupby("race_id", sort=False).size().to_numpy()
    Xv = valid_df_i[feature_cols].astype("float64").fillna(0.0)
    yv = valid_df_i["target_rank"].astype("int32")
    gv = valid_df_i.groupby("race_id", sort=False).size().to_numpy()
    train_set = lgb.Dataset(Xt, label=yt, group=gt)
    valid_set = lgb.Dataset(Xv, label=yv, group=gv, reference=train_set)
    params = {
        "objective": "lambdarank",
        "metric": ["ndcg"],
        "ndcg_eval_at": [1, 3, 5],
        "learning_rate": 0.03,
        "num_leaves": 24,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
    }
    booster = lgb.train(
        params, train_set, num_boost_round=800,
        valid_sets=[valid_set], valid_names=["valid"],
        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(period=0)],
    )
    return booster, feature_cols


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--valid-frac", type=float, default=0.10,
                    help="validation 比率 (default 0.10 = Window 4)")
    args = ap.parse_args()

    df = pd.read_parquet(DATASETS)
    rids = sorted(df["race_id"].unique().tolist(), key=_race_id_to_int)
    n_valid = max(int(len(rids) * args.valid_frac), 1)
    train_rids = set(rids[:-n_valid])
    valid_rids = rids[-n_valid:]
    print(f"valid_frac={args.valid_frac}: train {len(train_rids)} races, valid {len(valid_rids)} races", flush=True)
    train_df = df[df["race_id"].isin(train_rids)]
    valid = df[df["race_id"].isin(valid_rids)].copy()
    has_result = (
        valid.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )
    valid = valid[valid["race_id"].isin(has_result)].copy()
    n_eval = valid["race_id"].nunique()
    print(f"valid with results: {n_eval}", flush=True)

    t0 = time.time()
    booster, feature_cols = _train_lgbm(train_df)
    print(f"trained in {time.time()-t0:.1f}s, best_iter={booster.best_iteration}", flush=True)

    valid = valid.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    X = valid[feature_cols].astype("float64").fillna(0.0)
    valid["score"] = booster.predict(X.values)

    # T sweep: 別 LGBM で T=0.4 が本当に best か検証
    print()
    print("=== T sweep on new LGBM (W4 model, n=149 races) ===", flush=True)
    print(f"{'T':>5} {'log loss':>10} {'top-1 hit':>10}", flush=True)
    print("-" * 30, flush=True)
    t_grid = (0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.5, 2.0)
    best_T_w4 = 1.0
    best_ll = float("inf")
    def _race_softmax_T(s: pd.Series, t: float) -> pd.Series:
        scaled = s / t
        m = scaled.max()
        ex = np.exp(scaled - m)
        return ex / ex.sum()
    for T_test in t_grid:
        col = f"lp_T{T_test}"
        valid[col] = valid.groupby("race_id", sort=False)["score"].transform(
            lambda s, t=T_test: _race_softmax_T(s, t)
        )
        ll = 0.0
        top1_hits = 0
        n_r = 0
        for _rid, g in valid.groupby("race_id", sort=False):
            winner = g[g["target_top1"] == 1]
            if len(winner) != 1: continue
            p = float(winner[col].iloc[0])
            ll += -math.log(max(p, 1e-12))
            top_idx = g[col].idxmax()
            if g.loc[top_idx, "target_top1"] == 1:
                top1_hits += 1
            n_r += 1
        ll_mean = ll / n_r if n_r else 0.0
        marker = ""
        if ll_mean < best_ll:
            best_ll = ll_mean
            best_T_w4 = T_test
            marker = " ←"
        print(f"{T_test:>5.2f}{marker} {ll_mean:>9.4f} {top1_hits/n_r*100:>9.1f}%", flush=True)
    # 注: LGBM_TEMPERATURE constant は default 値で、実際の production T は
    # data/models/lgbm_metadata.json の softmax_temperature を見る (再 tune 時に
    # constant と乖離する)。ここでは W4 (本スクリプトで再訓練した booster) の
    # 評価のため LGBM_TEMPERATURE constant を baseline として使う。
    print(f"W4 best T = {best_T_w4} (constant LGBM_TEMPERATURE = {LGBM_TEMPERATURE})", flush=True)
    print()

    T = LGBM_TEMPERATURE
    def _softmax(s: pd.Series) -> pd.Series:
        scaled = s / T
        m = scaled.max()
        ex = np.exp(scaled - m)
        return ex / ex.sum()
    valid["lgbm_prob"] = valid.groupby("race_id", sort=False)["score"].transform(_softmax)

    # market_prob via power_method de-overround
    def _market_prob(g: pd.DataFrame) -> pd.Series:
        raw = {int(r.horse_number): (1.0 / r.win_odds) if r.win_odds and r.win_odds > 0 else 0.0
               for r in g.itertuples()}
        # 未正規化 1/odds (Σ=overround>1) のまま de-vig へ —
        # 正規化すると power_method が k=1 の恒等写像に縮退する (2026-06-10 修正)
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

    valid["prob_default"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(lambda g: _blend(g, BLEND_DEFAULT), include_groups=False)
    )
    valid["prob_apt"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(lambda g: _blend(g, BLEND_APTITUDE_GATE), include_groups=False)
    )

    # caches
    payout_cache: dict[str, int] = {}
    odds_cache: dict[str, dict] = {}
    apt_cache: dict[str, list[int]] = {}
    for rid in valid["race_id"].unique():
        rp = RAW_DIR / f"{rid}-result.html.gz"
        if rp.exists():
            try:
                html = gzip.open(rp, "rt", encoding="utf-8").read()
                parsed = parse_result(html)
                if parsed and parsed.get("payout"):
                    payout_cache[rid] = int(parsed["payout"])
            except Exception:
                pass
        oj = TRIFECTA_DIR / f"{rid}.json"
        if oj.exists():
            try:
                d = json.loads(oj.read_text(encoding="utf-8"))
                mp = {}
                for t in d.get("trifecta") or []:
                    key = tuple(t["key"])
                    if len(key) != 3: continue
                    try:
                        mp[(int(key[0]), int(key[1]), int(key[2]))] = (
                            float(t["odds"]), int(t.get("popularity") or 0)
                        )
                    except Exception:
                        continue
                odds_cache[rid] = mp
            except Exception:
                pass
        aj = APT_DIR / f"{rid}.json"
        if aj.exists():
            try:
                d = json.loads(aj.read_text(encoding="utf-8"))
                apt_cache[rid] = [int(h) for h in d.get("aptitude_top_horses") or []]
            except Exception:
                pass

    n_complete = sum(1 for rid in valid["race_id"].unique()
                     if rid in payout_cache and rid in odds_cache)
    print(f"complete races (payout + odds): {n_complete}")
    print()

    # ==== 単勝 ROI β sweep on W4 model (Phase 18 verification) ====
    print("=== 単勝 ROI by β on W4 model (n=149) ===", flush=True)
    print(f"{'β':>5} {'top-1 hit':>10} {'tansho ROI':>11} {'hits':>5}", flush=True)
    print("-" * 35, flush=True)
    beta_grid_sb = [round(b, 2) for b in np.arange(0.0, 1.01, 0.1)]
    for beta in beta_grid_sb:
        col_sb = f"sb_{int(beta*100):03d}"
        valid[col_sb] = (
            valid.groupby("race_id", sort=False, group_keys=False)
            .apply(lambda g, b=beta: _blend(g, b), include_groups=False)
        )
        top_hits = 0
        stake = 0
        payout = 0
        wins = 0
        for _rid, g in valid.groupby("race_id", sort=False):
            top_idx = g[col_sb].idxmax()
            top_row = g.loc[top_idx]
            stake += 100
            if top_row["target_top1"] == 1:
                top_hits += 1
                wo = float(top_row["win_odds"]) if top_row["win_odds"] > 0 else 0.0
                payout += int(100 * wo)
                wins += 1
        roi = payout / stake if stake else 0
        n_r = valid["race_id"].nunique()
        print(f"{beta:>5.2f} {top_hits/n_r*100:>9.1f}% {roi*100:>10.1f}% {wins:>5}", flush=True)
    print()

    # ==== 単勝 confidence-based bet filter @ β=BLEND_DEFAULT ====
    # 「モデル top horse の予測 prob が高いほど実際当たりやすいか」を確認。
    # 当たりやすいなら confidence 下限で skip 戦略 (race を打たない) が有効。
    # 旧実装は 0.78 literal で BLEND_DEFAULT 変更時に silent drift する pattern。
    print(f"=== 単勝 ROI by top-1 confidence bin @ β={BLEND_DEFAULT} (n=149) ===", flush=True)
    print(f"{'bin':>15} {'n races':>8} {'hit rate':>9} {'ROI':>8} {'avg odds':>10}", flush=True)
    print("-" * 55, flush=True)
    valid["sb_prod"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(lambda g: _blend(g, BLEND_DEFAULT), include_groups=False)
    )
    # 各 race の top-1 row を集める
    race_picks: list[tuple[str, float, bool, float]] = []
    for rid, g in valid.groupby("race_id", sort=False):
        top_idx = g["sb_prod"].idxmax()
        top = g.loc[top_idx]
        race_picks.append((
            rid,
            float(top["sb_prod"]),
            bool(top["target_top1"] == 1),
            float(top["win_odds"]) if top["win_odds"] > 0 else 0.0,
        ))
    bins = [
        ("< 0.15",  0.00, 0.15),
        ("0.15-0.25", 0.15, 0.25),
        ("0.25-0.35", 0.25, 0.35),  # ← W3 で +EV 105.7% を示した band
        ("0.35-0.45", 0.35, 0.45),
        ("≥ 0.45",   0.45, 1.01),
    ]
    for label, lo, hi in bins:
        bucket = [p for p in race_picks if lo <= p[1] < hi]
        if not bucket:
            print(f"{label:>15} {'0':>8} — — —", flush=True)
            continue
        n_b = len(bucket)
        hits_b = sum(1 for p in bucket if p[2])
        stake_b = n_b * 100
        payout_b = sum(int(100 * p[3]) for p in bucket if p[2])
        roi_b = payout_b / stake_b if stake_b else 0
        avg_odds_b = sum(p[3] for p in bucket) / n_b
        print(
            f"{label:>15} {n_b:>8} {hits_b/n_b*100:>8.1f}% {roi_b*100:>7.1f}% {avg_odds_b:>9.2f}",
            flush=True,
        )
    print()

    plan_codes = ("A", "B", "C", "G", "H1", "H2")
    stake = {c: 0 for c in plan_codes}
    payout = {c: 0 for c in plan_codes}
    hits = {c: 0 for c in plan_codes}
    points = {c: 0 for c in plan_codes}
    n_used = 0

    for rid, g in valid.groupby("race_id", sort=False):
        if rid not in odds_cache or rid not in payout_cache:
            continue
        n = len(g)
        if n < 3: continue
        horse_numbers = g["horse_number"].to_numpy().astype(int)
        show = g["shrunk_show_rate"].to_numpy()
        avg_show = float(np.mean(show)) if n > 0 else 0.0
        bias = np.clip(show / avg_show, 0.1, None) if avg_show > 0 else np.ones(n)

        def _build_evrows(prob_col: str) -> list[EvRow]:
            w = np.maximum(g[prob_col].to_numpy(), 1e-12)
            s1 = w
            s2 = (s1 ** DEFAULT_LAMBDA_2) * bias
            s3 = (s1 ** DEFAULT_LAMBDA_3) * bias
            S1 = float(s1.sum()); S2 = float(s2.sum()); S3 = float(s3.sum())
            if S1 <= 0 or S2 <= 0 or S3 <= 0:
                return []
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
            ev: list[EvRow] = []
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
                        ev.append(EvRow(key=key, odds=odds, popularity=popu, prob=p, px_o=pxo, tier=_tier(pxo)))
            ev.sort(key=lambda r: r.px_o, reverse=True)
            return ev

        ev_def = _build_evrows("prob_default")
        ev_apt = _build_evrows("prob_apt")
        if not ev_def or not ev_apt:
            continue

        finish = g["finish_pos"].to_numpy()
        a_idx = np.where(finish == 1.0)[0]
        b_idx = np.where(finish == 2.0)[0]
        c_idx = np.where(finish == 3.0)[0]
        if len(a_idx) != 1 or len(b_idx) != 1 or len(c_idx) != 1:
            continue
        actual_key = (int(horse_numbers[a_idx[0]]), int(horse_numbers[b_idx[0]]), int(horse_numbers[c_idx[0]]))
        real_payout = payout_cache[rid]
        apt_top = apt_cache.get(rid, [])
        n_used += 1

        picks_by = {
            "A": plan_balanced(ev_def),
            "B": plan_max_ev(ev_def),
            "C": plan_wide(ev_def),
            "G": plan_aptitude_ev(ev_apt, apt_top) if apt_top else [],
            "H1": plan_hit_pure(ev_def, target=3),
            "H2": plan_hit_safe(ev_def, target=3),
        }
        for code, picks in picks_by.items():
            n_pts = len(picks)
            if n_pts == 0: continue
            stake[code] += n_pts * 100
            points[code] += n_pts
            if any(r.key == actual_key for r in picks):
                payout[code] += real_payout
                hits[code] += 1

    print(f"=== Plan ROI for valid_frac={args.valid_frac} (n_used={n_used}) ===")
    print(f"{'Plan':>4} {'ROI':>7} {'hits':>5} {'avg pt':>7} {'stake':>10} {'payout':>10}")
    print("-" * 50)
    for code in plan_codes:
        if stake[code] == 0:
            print(f"{code:>4} {'—':>7} {hits[code]:>5} {0.0:>7.1f} {'—':>10} {'—':>10}")
            continue
        roi = payout[code] / stake[code]
        avg_pts = points[code] / n_used
        print(f"{code:>4} {roi*100:>6.1f}% {hits[code]:>5} {avg_pts:>7.1f} ¥{stake[code]:>8,} ¥{payout[code]:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
