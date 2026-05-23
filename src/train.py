"""LightGBM lambdarank で着順予測モデルを学習する。

Yurelu / R-bloggers 2026 / PC-KEIBA / Reddit lambdarank の合流地点設計:
  - **lambdarank**: pointwise ではなく race-relative ranking として学習 → ndcg@3 最大化
  - **時系列 split**: TimeSeriesSplit。race_id を datetime に変換して並び替え、
    過去 → 未来 を train / valid に
  - **race_id を group**: LightGBM の `group` パラメータに各レースの行数を渡す
  - **時間リーク防止**: 当該レースの単勝オッズ / 人気 は学習特徴量に **入れない**
    (umaro_ai 警告: pred が払戻率に収束する)
  - **欠損ハンドリング**: LightGBM は NaN を自然に処理する。past_runs 無い馬は
    feature 全 0 になる (build_features 経由) ので、別途 NaN にせず 0 のまま。

学習後:
  - data/models/lgbm_lambdarank.txt にモデル保存
  - data/models/lgbm_metadata.json に列順 / 学習日 / 件数を保存

CLI:
  python -m src.train  # data/datasets/all.parquet から学習
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT / "data" / "datasets"
MODELS_DIR = ROOT / "data" / "models"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


# 学習に使わない列 (リーク or 識別子)
NON_FEATURE_COLS = {
    "race_id", "venue", "race_no", "distance", "surface", "going",
    "horse_number", "n_horses",
    "finish_pos", "target_top1", "target_top3", "target_rank",
    # 当日のオッズ / 不要 (リーク回避)
    "win_odds",
    # absent はラベル前にフィルタ済
    "absent",
}


def _race_id_to_unix(rid: str) -> int:
    """race_id を時系列 split 用に整数化。
    JRA: YYYY+PP+回+日+RR → 開催回/日が時系列順なので yyyy+rr1+rr2+rr3+rr 順に並べる。
    NAR: YYYY+PP+MMDD+RR → yyyy+mm+dd+rr で並べられる。
    どちらも 12 桁を そのまま int 化すれば、近似的に時系列順になる。
    厳密ではないがレース日付の前後関係はおおむね保たれる。
    """
    try:
        return int(rid)
    except ValueError:
        return 0


def _make_splits(df: pd.DataFrame, valid_frac: float = 0.2):
    """時系列順に sort → 後ろ valid_frac を validation に。"""
    # race_id 単位で並べる
    unique_rids = df["race_id"].unique().tolist()
    unique_rids.sort(key=_race_id_to_unix)
    n_valid = max(int(len(unique_rids) * valid_frac), 1)
    train_rids = set(unique_rids[:-n_valid])
    valid_rids = set(unique_rids[-n_valid:])
    return (
        df[df["race_id"].isin(train_rids)].copy(),
        df[df["race_id"].isin(valid_rids)].copy(),
    )


def _prep_xy(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, np.ndarray]:
    """X, y, group を返す。group = 各 race_id の行数 (LightGBM 仕様)。"""
    # 着順未確定 (4 着以下) は target_rank=0 で含めて学習。
    # 結果が全く取れなかったレース (finish_pos が全 NaN かつ target_top3 が全 0) は除外。
    df = df.sort_values(["race_id", "horse_number"]).reset_index(drop=True)

    # 完走レース判定: そのレースに target_top1 が 1 つでもあるなら結果ありとみなす
    races_with_result = (
        df.groupby("race_id")["target_top1"].sum().reset_index(name="ones")
    )
    keep_races = races_with_result[races_with_result["ones"] > 0]["race_id"].tolist()
    if not keep_races:
        raise ValueError("結果ありのレースなし。dataset を確認してください")

    df = df[df["race_id"].isin(keep_races)].copy()
    df = df.sort_values(["race_id", "horse_number"]).reset_index(drop=True)

    feature_cols = [c for c in df.columns if c not in NON_FEATURE_COLS]
    X = df[feature_cols].astype("float64").fillna(0.0)
    y = df["target_rank"].astype("int32")
    # group: 各 race_id の連続行数
    group_sizes = df.groupby("race_id", sort=False).size().to_numpy()
    return X, y, group_sizes


@app.command()
def main(
    input_path: Path = typer.Option(
        DATASETS_DIR / "all.parquet", "--input", "-i",
    ),
    output_dir: Path = typer.Option(MODELS_DIR, "--output", "-o"),
    valid_frac: float = typer.Option(0.2, "--valid-frac"),
    num_boost_round: int = typer.Option(500, "--rounds"),
    learning_rate: float = typer.Option(0.05, "--lr"),
    num_leaves: int = typer.Option(31, "--leaves"),
    early_stopping: int = typer.Option(50, "--early-stop"),
):
    """LightGBM lambdarank 学習。"""
    if not input_path.exists():
        console.print(f"[red]not found: {input_path}[/red]")
        raise typer.Exit(1)
    df = pd.read_parquet(input_path)
    console.print(f"loaded {len(df):,} rows / {df['race_id'].nunique():,} races")

    train_df, valid_df = _make_splits(df, valid_frac=valid_frac)
    console.print(
        f"split: train={train_df['race_id'].nunique()} races / "
        f"valid={valid_df['race_id'].nunique()} races"
    )

    X_train, y_train, g_train = _prep_xy(train_df)
    X_valid, y_valid, g_valid = _prep_xy(valid_df)

    console.print(f"feature cols: {len(X_train.columns)}")
    for c in X_train.columns:
        console.print(f"  - {c}", highlight=False)

    train_set = lgb.Dataset(X_train, label=y_train, group=g_train)
    valid_set = lgb.Dataset(X_valid, label=y_valid, group=g_valid, reference=train_set)

    params = {
        "objective": "lambdarank",
        "metric": ["ndcg"],
        "ndcg_eval_at": [1, 3, 5],
        "learning_rate": learning_rate,
        "num_leaves": num_leaves,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
    }

    console.rule("[bold]training[/bold]")
    callbacks = [
        lgb.early_stopping(early_stopping, verbose=True),
        lgb.log_evaluation(20),
    ]
    model = lgb.train(
        params, train_set,
        num_boost_round=num_boost_round,
        valid_sets=[train_set, valid_set],
        valid_names=["train", "valid"],
        callbacks=callbacks,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "lgbm_lambdarank.txt"
    model.save_model(str(model_path))

    # === softmax temperature calibration ===
    # 訓練後の valid set で T を sweep し、log loss 最小の T を採用する。
    # T は model-specific (W3 model は T=0.4、W4 model は T=1-2 が最適だった経験あり)
    # ので、retrain ごとに自動で T を更新するため metadata に保存する。
    # _lgbm_predict は metadata の `softmax_temperature` を読んで適用。
    console.rule("[bold]softmax temperature calibration[/bold]")
    valid_scores = model.predict(X_valid, num_iteration=model.best_iteration)
    # valid_df を sort 順で再構築 (X_valid と同じ順)
    vdf = valid_df.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    vdf = vdf[vdf["race_id"].isin(
        vdf.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )].copy()
    # X_valid と整合するよう同 race set に絞り score 配列を作る
    vdf["_score"] = valid_scores[:len(vdf)] if len(valid_scores) == len(vdf) else None
    if vdf["_score"].isna().any():
        # 数が合わない場合 (rare)、計算し直す
        from src.train import _prep_xy as _prep
        Xv2, _, _ = _prep(vdf)
        vdf["_score"] = model.predict(Xv2, num_iteration=model.best_iteration)
    t_grid = [round(t, 2) for t in np.arange(0.20, 2.51, 0.05)]
    best_T = 1.0
    best_ll = float("inf")
    for T in t_grid:
        ll_sum = 0.0
        n_r = 0
        for _rid, g in vdf.groupby("race_id", sort=False):
            scaled = (g["_score"].to_numpy() / T)
            m = scaled.max()
            exps = np.exp(scaled - m)
            probs = exps / exps.sum()
            winner_mask = (g["target_top1"] == 1).to_numpy()
            if winner_mask.sum() != 1:
                continue
            p = float(probs[winner_mask][0])
            import math as _math
            ll_sum += -_math.log(max(p, 1e-12))
            n_r += 1
        if n_r == 0:
            continue
        ll = ll_sum / n_r
        if ll < best_ll:
            best_ll = ll
            best_T = T
    console.print(
        f"calibrated softmax temperature T = [bold]{best_T}[/bold] "
        f"(valid log loss {best_ll:.4f})"
    )

    # 特徴量重要度 (gain) も metadata に保存 → LLM が「モデルがどの特徴量に依存
    # しているか」を context として読める。最重要 top 10 のみ保存 (全 26 だと長い)。
    feature_importance_pairs = sorted(
        zip(X_train.columns, model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    top_features = [
        {"name": name, "gain": float(gain)}
        for name, gain in feature_importance_pairs[:10]
    ]

    meta = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "n_train_races": int(train_df["race_id"].nunique()),
        "n_valid_races": int(valid_df["race_id"].nunique()),
        "n_train_rows": int(len(X_train)),
        "feature_cols": list(X_train.columns),
        "params": params,
        "best_iteration": model.best_iteration,
        "best_scores": {k: dict(v) for k, v in model.best_score.items()},
        "softmax_temperature": best_T,
        "softmax_temperature_valid_log_loss": best_ll,
        "top_features_by_gain": top_features,
    }
    meta_path = output_dir / "lgbm_metadata.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]saved model: {model_path}[/green]")
    console.print(f"[green]saved meta:  {meta_path}[/green]")

    # 特徴量重要度
    imp = sorted(
        zip(X_train.columns, model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    console.rule("[bold]feature importance (gain)[/bold]")
    for name, gain in imp:
        console.print(f"  {name:30s} {gain:10.1f}")


if __name__ == "__main__":
    app()
