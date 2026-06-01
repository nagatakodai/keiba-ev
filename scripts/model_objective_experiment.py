"""モデル目的関数の比較実験 (本番コード/モデルは変更しない・読み取りのみ)。

Exp1 (objective 比較):
    objective='binary' (label=target_top1) で確率を直出しする LightGBM を学習し、
    現行 lambdarank+温度スケーリング と比較する。binary は native に校正される仮説の検証。
    指標: valid の log loss / Brier / ndcg@1 / 単勝 top-1 ROI。
    余裕があれば multiclass softmax (race=1 サンプル) も。

Exp2 (market-as-feature, Benter 流):
    市場暗黙率 market_implied = (1/win_odds を race 内正規化) を特徴量に1列追加して
    lambdarank を学習し、無し版と比較。単に市場を再現するだけか、市場+α を出すか。

split: chronological last-20% を valid、残り train (train.py / eval_holdout.py と同じ)。
feature_cols / params は lgbm_metadata.json + train.py から流用。

実験モデルは data/models/_experiment/ に別名保存 (本番 lgbm_lambdarank.txt は上書きしない)。

CLI:
    .venv/bin/python scripts/model_objective_experiment.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "data" / "datasets" / "all.parquet"
META_PATH = ROOT / "data" / "models" / "lgbm_metadata.json"
OUT_DIR = ROOT / "data" / "models" / "_experiment"
OUT_DIR.mkdir(parents=True, exist_ok=True)

VALID_FRAC = 0.2
SEED = 42


# --- production と揃えた lambdarank パラメータ (train.py より) ---
LAMBDARANK_PARAMS = {
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
    "seed": SEED,
}
NUM_BOOST_ROUND = 800
EARLY_STOP = 100


def race_id_to_unix(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def split_train_valid(df: pd.DataFrame, valid_frac: float = VALID_FRAC):
    rids = df["race_id"].unique().tolist()
    rids.sort(key=race_id_to_unix)
    n_valid = max(int(len(rids) * valid_frac), 1)
    train_rids = set(rids[:-n_valid])
    valid_rids = set(rids[-n_valid:])
    return (
        df[df["race_id"].isin(train_rids)].copy(),
        df[df["race_id"].isin(valid_rids)].copy(),
    )


def keep_races_with_result(df: pd.DataFrame) -> pd.DataFrame:
    """結果ありレース (target_top1 が立つ) のみ残す (train.py と同じ)。"""
    ones = df.groupby("race_id")["target_top1"].sum()
    keep = ones[ones > 0].index
    df = df[df["race_id"].isin(keep)].copy()
    return df.sort_values(["race_id", "horse_number"]).reset_index(drop=True)


def race_softmax(scores: np.ndarray, T: float = 1.0) -> np.ndarray:
    s = scores / T
    s = s - s.max()
    ex = np.exp(s)
    return ex / ex.sum()


def add_market_feature(df: pd.DataFrame) -> pd.DataFrame:
    """market_implied = 1/win_odds を race 内正規化した列を追加 (Σ=1 per race)。"""
    df = df.copy()
    wo = df["win_odds"].to_numpy()
    raw = np.divide(1.0, wo, out=np.zeros_like(wo, dtype=float), where=wo > 0)
    df["_raw_inv"] = raw
    sums = df.groupby("race_id")["_raw_inv"].transform("sum").to_numpy()
    df["market_implied"] = np.divide(
        raw, sums, out=np.zeros_like(raw), where=sums > 0
    )
    df.drop(columns=["_raw_inv"], inplace=True)
    return df


# ======================================================================
# 共通評価: race 内確率列 (sum=1 per race) を渡して指標を出す
# ======================================================================
def evaluate(valid: pd.DataFrame, prob_col: str, label: str) -> dict:
    """valid に prob_col (race 内 Σ=1) がある前提で各指標を計算。

    - log loss (winner): 各レースの 1 着馬の prob で -log の平均
    - Brier (winner-vs-rest): 全馬で (prob - target_top1)^2 の平均
    - ndcg@1: モデル top-1 が実 1 着馬か (= top-1 hit rate と一致)
    - 単勝 ROI: モデル top-1 馬に ¥100、的中で win_odds*100
    """
    ll_sum = 0.0
    ll_n = 0
    brier_sum = 0.0
    brier_n = 0
    top1_hits = 0
    n_races = 0
    stake_total = 0
    payout_total = 0
    for _rid, g in valid.groupby("race_id", sort=False):
        winner = g[g["target_top1"] == 1]
        if len(winner) != 1:
            continue
        n_races += 1
        # log loss
        p_win = float(winner[prob_col].iloc[0])
        ll_sum += -math.log(max(p_win, 1e-12))
        ll_n += 1
        # Brier (per horse, this race)
        p = g[prob_col].to_numpy()
        y = g["target_top1"].to_numpy().astype(float)
        brier_sum += float(np.sum((p - y) ** 2))
        brier_n += len(g)
        # top-1
        top_idx = g[prob_col].idxmax()
        is_hit = g.loc[top_idx, "target_top1"] == 1
        if is_hit:
            top1_hits += 1
        # 単勝 ROI
        stake_total += 100
        if is_hit:
            wo = float(g.loc[top_idx, "win_odds"])
            if wo > 0:
                payout_total += int(100 * wo)
    return {
        "label": label,
        "n_races": n_races,
        "log_loss": ll_sum / ll_n if ll_n else float("nan"),
        "brier": brier_sum / brier_n if brier_n else float("nan"),
        "ndcg1": top1_hits / n_races if n_races else 0.0,
        "tansho_hits": top1_hits,
        "tansho_roi": payout_total / stake_total if stake_total else 0.0,
    }


def calibration_report(valid: pd.DataFrame, prob_col: str, label: str, n_bins: int = 10) -> str:
    """確率の校正診断: prob を bin にして平均予測 vs 実 1 着率を比べる。
    ECE (expected calibration error) も出す。"""
    p = valid[prob_col].to_numpy()
    y = valid["target_top1"].to_numpy().astype(float)
    edges = np.linspace(0, p.max() + 1e-9, n_bins + 1)
    lines = [f"  [{label}] calibration (pred vs actual top1 rate):"]
    ece = 0.0
    N = len(p)
    for i in range(n_bins):
        m = (p >= edges[i]) & (p < edges[i + 1])
        if m.sum() == 0:
            continue
        pred = p[m].mean()
        act = y[m].mean()
        cnt = int(m.sum())
        ece += cnt / N * abs(pred - act)
        lines.append(f"    bin {edges[i]:.3f}-{edges[i+1]:.3f}: n={cnt:5d} pred={pred:.3f} actual={act:.3f}")
    lines.append(f"    ECE = {ece:.4f}")
    return "\n".join(lines)


# ======================================================================
# モデル学習ヘルパ
# ======================================================================
def train_lambdarank(X_tr, y_rank_tr, g_tr, X_va, y_rank_va, g_va):
    train_set = lgb.Dataset(X_tr, label=y_rank_tr, group=g_tr)
    valid_set = lgb.Dataset(X_va, label=y_rank_va, group=g_va, reference=train_set)
    model = lgb.train(
        LAMBDARANK_PARAMS, train_set,
        num_boost_round=NUM_BOOST_ROUND,
        valid_sets=[valid_set], valid_names=["valid"],
        callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)],
    )
    return model


def train_binary(X_tr, y_bin_tr, X_va, y_bin_va):
    params = {
        "objective": "binary",
        "metric": ["binary_logloss"],
        "learning_rate": 0.03,
        "num_leaves": 24,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "seed": SEED,
    }
    train_set = lgb.Dataset(X_tr, label=y_bin_tr)
    valid_set = lgb.Dataset(X_va, label=y_bin_va, reference=train_set)
    model = lgb.train(
        params, train_set,
        num_boost_round=NUM_BOOST_ROUND,
        valid_sets=[valid_set], valid_names=["valid"],
        callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)],
    )
    return model


def calibrate_temperature(model, X_va, valid_df, feature_cols):
    """valid で softmax 温度 T を sweep し log loss 最小の T を返す (train.py と同じ手順)。"""
    scores = model.predict(X_va, num_iteration=model.best_iteration)
    vdf = valid_df.copy()
    vdf["_score"] = scores
    grid = [round(t, 2) for t in np.arange(0.2, 2.51, 0.05)]
    best_T, best_ll = 1.0, float("inf")
    for T in grid:
        ll_sum, n = 0.0, 0
        for _rid, g in vdf.groupby("race_id", sort=False):
            probs = race_softmax(g["_score"].to_numpy(), T)
            wm = (g["target_top1"] == 1).to_numpy()
            if wm.sum() != 1:
                continue
            ll_sum += -math.log(max(float(probs[wm][0]), 1e-12))
            n += 1
        if n == 0:
            continue
        ll = ll_sum / n
        if ll < best_ll:
            best_ll, best_T = ll, T
    return best_T, best_ll


def main():
    print(f"loading {DATASETS}")
    df = pd.read_parquet(DATASETS)
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    feature_cols = list(meta["feature_cols"])
    print(f"rows={len(df):,} races={df['race_id'].nunique():,} base_features={len(feature_cols)}")

    train_df, valid_df = split_train_valid(df, VALID_FRAC)
    train_df = keep_races_with_result(train_df)
    valid_df = keep_races_with_result(valid_df)
    print(
        f"split: train={train_df['race_id'].nunique()} races / "
        f"valid={valid_df['race_id'].nunique()} races "
        f"(valid rows={len(valid_df)})"
    )

    # 共通: 基本特徴量 X / rank ラベル / binary ラベル / group
    def build(df_, fcols):
        df_ = df_.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
        X = df_[fcols].astype("float64").fillna(0.0)
        y_rank = df_["target_rank"].astype("int32")
        y_bin = df_["target_top1"].astype("int32")
        groups = df_.groupby("race_id", sort=False).size().to_numpy()
        return df_, X, y_rank, y_bin, groups

    tr, Xtr, ytr_rank, ytr_bin, gtr = build(train_df, feature_cols)
    va, Xva, yva_rank, yva_bin, gva = build(valid_df, feature_cols)

    results = []

    # ==================================================================
    # Exp1 — objective 比較
    # ==================================================================
    print("\n" + "=" * 70)
    print("Exp1: objective 比較 (lambdarank+温度 vs binary vs multiclass)")
    print("=" * 70)

    # --- A) 現行同等 lambdarank + 温度スケーリング ---
    print("[A] training lambdarank ...")
    m_lr = train_lambdarank(Xtr, ytr_rank, gtr, Xva, yva_rank, gva)
    m_lr.save_model(str(OUT_DIR / "exp_lambdarank.txt"))
    best_T, best_T_ll = calibrate_temperature(m_lr, Xva, va, feature_cols)
    print(f"    calibrated softmax T = {best_T} (valid winner log loss {best_T_ll:.4f})")
    va["_lr_score"] = m_lr.predict(Xva, num_iteration=m_lr.best_iteration)
    # T=0.5 (metadata 本番値) と best_T 両方で評価
    for Tname, Tval in (("T=0.5 (prod)", 0.5), (f"T={best_T} (best)", best_T)):
        col = f"_lr_prob_{Tval}"
        va[col] = va.groupby("race_id", sort=False)["_lr_score"].transform(
            lambda s, t=Tval: pd.Series(race_softmax(s.to_numpy(), t), index=s.index)
        )
        results.append(evaluate(va, col, f"lambdarank softmax {Tname}"))

    # --- B) binary objective (確率直出し), race 内正規化のみ (温度なし) ---
    print("[B] training binary ...")
    m_bin = train_binary(Xtr, ytr_bin, Xva, yva_bin)
    m_bin.save_model(str(OUT_DIR / "exp_binary.txt"))
    va["_bin_raw"] = m_bin.predict(Xva, num_iteration=m_bin.best_iteration)
    # raw sigmoid 確率をそのまま (校正診断用) と、race 内正規化 (Σ=1) 両方
    va["_bin_norm"] = va.groupby("race_id", sort=False)["_bin_raw"].transform(
        lambda s: s / s.sum() if s.sum() > 0 else s
    )
    results.append(evaluate(va, "_bin_norm", "binary (race-normalized, no temp)"))

    # --- C) multiclass softmax: race=1 サンプル, クラス=馬位置 ---
    # LightGBM multiclass は固定クラス数を要求するので、頭数可変レースには直接不向き。
    # ここでは「全レース共通の最大頭数 K にパディングして multiclass」は歪むため、
    # 代替として binary の確率を race softmax で温度 sweep する形 (multinomial 的) を見る。
    # 厳密な multiclass は頭数可変で実装困難 → binary を race softmax 正規化したものを近似採用。
    # （注: 余裕枠。本筋は A vs B）
    try:
        # binary raw を logit に戻して race softmax (multinomial 風) — 温度1
        eps = 1e-6
        p = np.clip(va["_bin_raw"].to_numpy(), eps, 1 - eps)
        va["_bin_logit"] = np.log(p / (1 - p))
        va["_mc_prob"] = va.groupby("race_id", sort=False)["_bin_logit"].transform(
            lambda s: pd.Series(race_softmax(s.to_numpy(), 1.0), index=s.index)
        )
        results.append(evaluate(va, "_mc_prob", "binary-logit race-softmax (multinomial風)"))
    except Exception as e:
        print(f"    multinomial 風 skip: {e}")

    print("\n--- Exp1 results ---")
    print(f"{'model':<42} {'logloss':>9} {'brier':>9} {'ndcg@1':>8} {'tansho_ROI':>11} {'hits':>6}")
    for r in results:
        print(
            f"{r['label']:<42} {r['log_loss']:>9.4f} {r['brier']:>9.5f} "
            f"{r['ndcg1']*100:>7.2f}% {r['tansho_roi']*100:>10.2f}% {r['tansho_hits']:>6}"
        )

    # 校正診断 (binary raw vs lambdarank prod)
    print("\n--- calibration ---")
    print(calibration_report(va, "_bin_raw", "binary raw sigmoid"))
    print(calibration_report(va, "_lr_prob_0.5", "lambdarank T=0.5"))

    # ==================================================================
    # Exp2 — market-as-feature (Benter 流)
    # ==================================================================
    print("\n" + "=" * 70)
    print("Exp2: market-as-feature (lambdarank: 無し vs market_implied 特徴量追加)")
    print("=" * 70)

    df_m = add_market_feature(df)
    train_m, valid_m = split_train_valid(df_m, VALID_FRAC)
    train_m = keep_races_with_result(train_m)
    valid_m = keep_races_with_result(valid_m)
    feat_with_market = feature_cols + ["market_implied"]

    tr2, Xtr2, ytr2_rank, _, gtr2 = build(train_m, feat_with_market)
    va2, Xva2, yva2_rank, _, gva2 = build(valid_m, feat_with_market)

    print("[no-market] = Exp1 [A] lambdarank を再利用 (同 split)")
    # baseline は Exp1 の lambdarank (best_T と prod T) を流用。va は同じ valid races。

    print("[with-market] training lambdarank + market_implied ...")
    m_mk = train_lambdarank(Xtr2, ytr2_rank, gtr2, Xva2, yva2_rank, gva2)
    m_mk.save_model(str(OUT_DIR / "exp_lambdarank_market.txt"))
    bestT_mk, bestT_mk_ll = calibrate_temperature(m_mk, Xva2, va2, feat_with_market)
    print(f"    calibrated softmax T = {bestT_mk} (valid winner log loss {bestT_mk_ll:.4f})")

    va2["_mk_score"] = m_mk.predict(Xva2, num_iteration=m_mk.best_iteration)
    for Tname, Tval in (("T=0.5", 0.5), (f"T={bestT_mk}(best)", bestT_mk)):
        col = f"_mk_prob_{Tval}"
        va2[col] = va2.groupby("race_id", sort=False)["_mk_score"].transform(
            lambda s, t=Tval: pd.Series(race_softmax(s.to_numpy(), t), index=s.index)
        )

    # 市場単独 baseline (de-overround なしの単純 1/win_odds 正規化 = market_implied 列)
    def eval_market_only(vdf):
        ll_sum = ll_n = 0
        ll_sum = 0.0
        brier_sum = 0.0; brier_n = 0
        top1 = 0; n = 0; stake = 0; payout = 0
        for _rid, g in vdf.groupby("race_id", sort=False):
            w = g[g["target_top1"] == 1]
            if len(w) != 1:
                continue
            n += 1
            pw = float(w["market_implied"].iloc[0])
            ll_sum += -math.log(max(pw, 1e-12))
            p = g["market_implied"].to_numpy(); y = g["target_top1"].to_numpy().astype(float)
            brier_sum += float(np.sum((p - y) ** 2)); brier_n += len(g)
            ti = g["market_implied"].idxmax()
            hit = g.loc[ti, "target_top1"] == 1
            if hit:
                top1 += 1
            stake += 100
            if hit:
                wo = float(g.loc[ti, "win_odds"])
                if wo > 0:
                    payout += int(100 * wo)
        return {
            "label": "Market only (1/win_odds norm)", "n_races": n,
            "log_loss": ll_sum / n if n else float("nan"),
            "brier": brier_sum / brier_n if brier_n else float("nan"),
            "ndcg1": top1 / n if n else 0.0, "tansho_hits": top1,
            "tansho_roi": payout / stake if stake else 0.0,
        }

    results2 = []
    # baseline lambdarank (no market) at prod T and best T — Exp1 の va から再計算
    results2.append(evaluate(va, "_lr_prob_0.5", "no-market lambdarank T=0.5"))
    results2.append(evaluate(va, f"_lr_prob_{best_T}", f"no-market lambdarank T={best_T}"))
    results2.append(evaluate(va2, "_mk_prob_0.5", "with-market lambdarank T=0.5"))
    results2.append(evaluate(va2, f"_mk_prob_{bestT_mk}", f"with-market lambdarank T={bestT_mk}"))
    results2.append(eval_market_only(va2))
    # 参考: 本番ブレンド β=0.78 (no-market lambdarank prob と market_implied の loglinear)
    beta = 0.78
    va2["_lr_prob_for_blend"] = va["_lr_prob_0.5"].values if len(va) == len(va2) else va2.get("_lr_prob_0.5")
    # va と va2 は同じ valid race set・同じ並びなので index 整合する想定だが念のため再計算
    # （with-market モデルの prob ではなく no-market lambdarank prob を使う = 本番相当）
    # ここでは va2 上で no-market lambdarank を再 predict して厳密に揃える
    va2["_nm_score"] = m_lr.predict(va2[feature_cols].astype("float64").fillna(0.0).values,
                                     num_iteration=m_lr.best_iteration)
    va2["_nm_prob"] = va2.groupby("race_id", sort=False)["_nm_score"].transform(
        lambda s: pd.Series(race_softmax(s.to_numpy(), 0.5), index=s.index)
    )

    def blend_loglinear(g, b):
        a = max(1.0 - b, 0.0)
        f = np.maximum(g["_nm_prob"].to_numpy(), 1e-9)
        pi = np.maximum(g["market_implied"].to_numpy(), 1e-9)
        logs = a * np.log(f) + b * np.log(pi)
        ex = np.exp(logs - logs.max())
        return pd.Series(ex / ex.sum(), index=g.index)

    va2["_blend078"] = va2.groupby("race_id", sort=False, group_keys=False).apply(
        lambda g: blend_loglinear(g, beta)
    )
    results2.append(evaluate(va2, "_blend078", "prod blend β=0.78 (no-market model × market)"))

    print("\n--- Exp2 results ---")
    print(f"{'model':<46} {'logloss':>9} {'brier':>9} {'ndcg@1':>8} {'tansho_ROI':>11} {'hits':>6}")
    for r in results2:
        print(
            f"{r['label']:<46} {r['log_loss']:>9.4f} {r['brier']:>9.5f} "
            f"{r['ndcg1']*100:>7.2f}% {r['tansho_roi']*100:>10.2f}% {r['tansho_hits']:>6}"
        )

    # market_implied の特徴量重要度 (with-market モデル内のランク)
    imp = sorted(
        zip(feat_with_market, m_mk.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    print("\n--- with-market model: top feature importance (gain) ---")
    for i, (name, gain) in enumerate(imp[:12]):
        mark = "  <== MARKET" if name == "market_implied" else ""
        print(f"  {i+1:2d}. {name:28s} {gain:12.1f}{mark}")
    mk_rank = next(i for i, (n, _) in enumerate(imp) if n == "market_implied") + 1
    print(f"  market_implied rank: {mk_rank}/{len(imp)}")

    print("\n" + "=" * 70)
    print("⚠ 結論の限界 (検証レビュー指摘):")
    print("  - Exp2 の market-feature +ROI は **単一 chronological window の in-sample 値**。")
    print("    CV/sliding-window 未検証。CLAUDE.md の Plan G/Sprint/confidence band と同じく")
    print("    in-sample +EV が OOS で破綻する常習があるため、未確証。winner logloss は")
    print("    market-only より僅かに悪く、校正面では純市場を超えていない。")
    print("  - no-market 基線も popularity_outperformance (市場由来) を含み完全な market-free")
    print("    ではない。温度 (with-market T≈0.65) も同 valid で post-hoc fit。")
    print("  - 採用前に scripts/sliding_window_eval.py 流の独立2窓で再現を確認すること。")
    print("  - そもそも単勝 ROI は全戦略 break-even(100%) 未満 = どれも -EV (利益ではない)。")
    print("=" * 70)
    print("\nexperiment models saved to", OUT_DIR)


if __name__ == "__main__":
    main()
