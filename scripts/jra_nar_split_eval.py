"""JRA(中央) vs NAR(地方) のモデル性能差 + セグメント別モデルの有効性検証。

問い:
  (a) global モデルは NAR 偏重ゆえ JRA で劣るか?
  (b) JRA 専用モデルは JRA valid で global を上回るか?
  (c) transfer: NAR-only モデルを JRA に当てた時 vs JRA-only?

注意 (重要):
  - sorted(race_id) 後半 20% (時系列 valid) は **ほぼ全部 NAR** (JRA-valid N≒0)。
    → 時系列 split では JRA 比較が不可能。NAR は時系列で評価する。
  - JRA 比較は **ランダム split (race 単位)** で補助的に行う (in-sample 寄り
    = train と同時期の JRA を valid にするので楽観バイアス込み、と明記)。
  - N が小さい結論は「不確実」と正直に書く (本プロジェクトの哲学)。

本番コード (src/*.py, data/models/lgbm_lambdarank.txt) は変更しない。
実験モデルを保存する場合のみ別名 (data/models/_exp_*.txt)。

使い方:
  .venv/bin/python scripts/jra_nar_split_eval.py
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

from src.ev import (  # noqa: E402
    BLEND_DEFAULT,
    power_method_overround,
)

DATASETS = ROOT / "data" / "datasets" / "all.parquet"
# 本番 metadata の softmax 温度 (= production と一致。旧版は LGBM_TEMPERATURE=0.4 を使っていた)。
try:
    _SOFT_T = float(json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())
                    .get("softmax_temperature", 0.5))
except Exception:
    _SOFT_T = 0.5

# train.py / sliding_window_eval.py と同一の non-feature 列。
NON_FEATURE_COLS = {
    "race_id", "race_date", "venue", "race_no", "distance", "surface", "going",
    "horse_number", "n_horses",
    "finish_pos", "target_top1", "target_top3", "target_rank",
    "win_odds", "absent",
}

JRA_CODES = {f"{i:02d}" for i in range(1, 11)}


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _is_jra_series(race_id: pd.Series) -> pd.Series:
    return race_id.astype(str).str[4:6].isin(JRA_CODES)


def _train_lgbm(train_df: pd.DataFrame, *, internal_valid_frac: float = 0.15):
    """train.py / sliding_window_eval._train_lgbm と同一手順で学習。

    train_df をさらに内部 train/valid (時系列後ろ 15%) に切り early stopping。
    """
    df = train_df[train_df["target_top1"].notna()].copy()
    df = df.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    ones = df.groupby("race_id")["target_top1"].sum().reset_index(name="ones")
    keep = ones[ones["ones"] > 0]["race_id"].tolist()
    df = df[df["race_id"].isin(keep)].copy()

    inner_rids = df["race_id"].unique().tolist()
    inner_rids.sort(key=_race_id_to_int)
    n_inner_valid = max(int(len(inner_rids) * internal_valid_frac), 1)
    inner_train = set(inner_rids[:-n_inner_valid])
    inner_valid = set(inner_rids[-n_inner_valid:])
    tr = df[df["race_id"].isin(inner_train)].sort_values(
        ["race_id", "horse_number"]).reset_index(drop=True)
    va = df[df["race_id"].isin(inner_valid)].sort_values(
        ["race_id", "horse_number"]).reset_index(drop=True)

    feature_cols = [c for c in df.columns if c not in NON_FEATURE_COLS]
    Xt = tr[feature_cols].astype("float64").fillna(0.0)
    yt = tr["target_rank"].astype("int32")
    gt = tr.groupby("race_id", sort=False).size().to_numpy()
    Xv = va[feature_cols].astype("float64").fillna(0.0)
    yv = va["target_rank"].astype("int32")
    gv = va.groupby("race_id", sort=False).size().to_numpy()

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
        callbacks=[lgb.early_stopping(100, verbose=False),
                   lgb.log_evaluation(period=0)],
    )
    return booster, feature_cols


# ---- evaluation helpers (sliding_window_eval と同じ式) ----

def _softmax_T(s: np.ndarray, T: float) -> np.ndarray:
    scaled = s / T
    ex = np.exp(scaled - scaled.max())
    return ex / ex.sum()


def _market_prob(g: pd.DataFrame) -> dict[int, float]:
    raw = {int(r.horse_number): (1.0 / r.win_odds) if r.win_odds and r.win_odds > 0 else 0.0
           for r in g.itertuples()}
    # 未正規化 1/odds のまま de-vig へ (正規化すると k=1 no-op, 2026-06-10 修正)
    try:
        corrected = power_method_overround(raw)
    except Exception:
        corrected = raw
    s2 = sum(corrected.values())
    if s2 > 0:
        corrected = {k: v / s2 for k, v in corrected.items()}
    return corrected


def evaluate(valid: pd.DataFrame, booster, feature_cols,
             *, T: float = _SOFT_T, beta: float = BLEND_DEFAULT) -> dict:
    """valid を booster で評価 (T は production metadata=0.5)。

    返す: n_races, top1_acc (raw model top-1 が実1着か = top-1 accuracy。NDCG ではない),
          top1_hit% (blended), tansho_roi (blended top-1 単勝), model_only_hit%。
    """
    valid = valid.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    has = valid.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    valid = valid[valid["race_id"].isin(has)].copy()
    if valid.empty:
        return {"n_races": 0}
    X = valid[feature_cols].astype("float64").fillna(0.0)
    valid["score"] = booster.predict(X.values)

    n_r = 0
    ndcg1 = 0          # raw model top-1 == winner (= ndcg@1 with binary win rel)
    model_hits = 0     # model-only softmax top-1
    blend_hits = 0     # blended top-1
    stake = 0.0
    payout = 0.0
    ll = 0.0
    for _rid, g in valid.groupby("race_id", sort=False):
        win = g[g["target_top1"] == 1]
        if len(win) != 1:
            continue
        n_r += 1
        sc = g["score"].to_numpy()
        hn = g["horse_number"].to_numpy()
        win_hn = int(win["horse_number"].iloc[0])
        win_odds = float(win["win_odds"].iloc[0]) if win["win_odds"].iloc[0] and win["win_odds"].iloc[0] > 0 else 0.0

        # raw model ranking top-1 (ndcg@1)
        if int(hn[sc.argmax()]) == win_hn:
            ndcg1 += 1

        # model-only softmax top-1
        mp = _softmax_T(sc, T)
        if int(hn[mp.argmax()]) == win_hn:
            model_hits += 1
        # log loss (model-only)
        p_win = float(mp[np.where(hn == win_hn)[0][0]])
        ll += -math.log(max(p_win, 1e-12))

        # blended (Benter loglinear): c = softmax(alpha*log f + beta*log pi)
        mkt = _market_prob(g)
        alpha = max(1.0 - beta, 0.0)
        logs = []
        for i, h in enumerate(hn):
            f = max(mp[i], 1e-9)
            pi = max(mkt.get(int(h), 0.0), 1e-9)
            logs.append(alpha * math.log(f) + beta * math.log(pi))
        bl = _softmax_T(np.array(logs), 1.0)  # logs already in log-space; T=1
        top_hn = int(hn[bl.argmax()])
        stake += 100.0
        if top_hn == win_hn:
            blend_hits += 1
            payout += 100.0 * win_odds

    return {
        "n_races": n_r,
        "ndcg1": ndcg1 / n_r if n_r else 0.0,
        "model_hit": model_hits / n_r if n_r else 0.0,
        "blend_hit": blend_hits / n_r if n_r else 0.0,
        "log_loss": ll / n_r if n_r else 0.0,
        "tansho_roi": payout / stake if stake else 0.0,
    }


def _fmt(r: dict) -> str:
    if r.get("n_races", 0) == 0:
        return "n=0 (評価不能)"
    return (f"n={r['n_races']:>4}  top1acc={r["ndcg1"]*100:5.1f}%  "
            f"model_hit={r['model_hit']*100:5.1f}%  blend_hit={r['blend_hit']*100:5.1f}%  "
            f"tansho_ROI={r['tansho_roi']*100:6.1f}%  ll={r['log_loss']:.3f}")


def run_split(df: pd.DataFrame, valid_rids: set, label: str) -> None:
    train_df = df[~df["race_id"].isin(valid_rids)]
    valid_df = df[df["race_id"].isin(valid_rids)].copy()

    tr_is_jra = _is_jra_series(train_df["race_id"])
    jra_train = train_df[tr_is_jra]
    nar_train = train_df[~tr_is_jra]

    v_is_jra = _is_jra_series(valid_df["race_id"])
    jra_valid = valid_df[v_is_jra]
    nar_valid = valid_df[~v_is_jra]

    print(f"\n{'='*78}\n[{label}]")
    print(f"train races: {train_df['race_id'].nunique()} "
          f"(JRA {jra_train['race_id'].nunique()} / NAR {nar_train['race_id'].nunique()})")
    print(f"valid races: {valid_df['race_id'].nunique()} "
          f"(JRA {jra_valid['race_id'].nunique()} / NAR {nar_valid['race_id'].nunique()})")

    print("\ntraining 3 models (global / JRA-only / NAR-only)...", flush=True)
    t0 = time.time()
    g_booster, fcols = _train_lgbm(train_df)
    print(f"  global  best_iter={g_booster.best_iteration} ({time.time()-t0:.0f}s)", flush=True)
    j_booster = n_booster = None
    if jra_train["race_id"].nunique() >= 50:
        t0 = time.time()
        j_booster, _ = _train_lgbm(jra_train)
        print(f"  JRA     best_iter={j_booster.best_iteration} ({time.time()-t0:.0f}s)", flush=True)
    else:
        print("  JRA     SKIP (train < 50 races)")
    if nar_train["race_id"].nunique() >= 50:
        t0 = time.time()
        n_booster, _ = _train_lgbm(nar_train)
        print(f"  NAR     best_iter={n_booster.best_iteration} ({time.time()-t0:.0f}s)", flush=True)
    else:
        print("  NAR     SKIP")

    def ev(vdf, booster):
        if booster is None or vdf.empty:
            return {"n_races": 0}
        return evaluate(vdf, booster, fcols)

    print(f"\n--- JRA-valid ---")
    print(f"  global model : {_fmt(ev(jra_valid, g_booster))}")
    print(f"  JRA-only     : {_fmt(ev(jra_valid, j_booster))}")
    print(f"  NAR-only     : {_fmt(ev(jra_valid, n_booster))}  (transfer NAR->JRA)")
    # 芝/ダート別 (JRA valid 内)
    for surf, name in [("芝", "芝"), ("ダート", "ダート")]:
        sub = jra_valid[jra_valid["surface"] == surf]
        if sub["race_id"].nunique() >= 20:
            print(f"    JRA-{name:>3} global : {_fmt(ev(sub, g_booster))}")
            print(f"    JRA-{name:>3} JRA    : {_fmt(ev(sub, j_booster))}")

    print(f"\n--- NAR-valid ---")
    print(f"  global model : {_fmt(ev(nar_valid, g_booster))}")
    print(f"  NAR-only     : {_fmt(ev(nar_valid, n_booster))}")
    print(f"  JRA-only     : {_fmt(ev(nar_valid, j_booster))}  (transfer JRA->NAR)")


def main() -> int:
    df = pd.read_parquet(DATASETS)
    print(f"loaded {len(df):,} rows / {df['race_id'].nunique():,} races")
    rids_sorted = sorted(df["race_id"].unique().tolist(), key=_race_id_to_int)

    # === Split 1: chronological (時系列, 後ろ 20%) ===
    # → JRA-valid はほぼ 0 になる見込み (背景の通り)。NAR を本筋で評価。
    n_valid = max(int(len(rids_sorted) * 0.2), 1)
    chrono_valid = set(rids_sorted[-n_valid:])
    run_split(df, chrono_valid, "Split 1: 時系列 chronological (last 20% valid) — out-of-time, robust")

    # === Split 2: ランダム race-unit (JRA 比較のため) ===
    # in-sample 寄り (train と同時期の JRA を valid にする) と明記。
    rng = np.random.default_rng(42)
    shuffled = rids_sorted.copy()
    rng.shuffle(shuffled)
    n_rand_valid = max(int(len(shuffled) * 0.2), 1)
    rand_valid = set(shuffled[:n_rand_valid])
    run_split(df, rand_valid, "Split 2: ランダム race-unit (seed=42, 20% valid) — IN-SAMPLE 寄り (楽観バイアス込み)")

    print(f"\n{'='*78}")
    print("注意: Split 2 は train/valid が同時期混在 (時系列リークではないが分布が一致)")
    print("      → 絶対 ROI は楽観側。global vs segment の *相対* 比較に使う。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
