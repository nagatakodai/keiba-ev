"""data/datasets/all.parquet の chronological split (train.py と同じ) で
学習済 LightGBM モデルを評価する。

train.py の ndcg だけでは実投票相当の収支が見えないので、こちらでは:
  - top-1 / top-3 / top-5 hit rate  (1 着馬がモデル top-K に入った割合)
  - 単勝 ROI: 毎レース モデル top-1 1 頭に ¥100 ベット → win_odds × 100 if 的中
  - 市場ベースライン: win_odds 最低 (= 1 番人気) で同じ計算 → モデルが市場を超えたか
  - **ev.py 実運用形態の loglinear blend**: 複数の market_blend β で同じ指標を出し、
    本番設定 (β=0.4) が市場単独を超えているかを確認する。

CLI:
  python -m src.eval_holdout
  python -m src.eval_holdout --valid-frac 0.2
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .ev import power_method_overround

ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT / "data" / "datasets"
MODELS_DIR = ROOT / "data" / "models"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


def _race_id_to_unix(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _split_valid(df: pd.DataFrame, valid_frac: float) -> pd.DataFrame:
    rids = df["race_id"].unique().tolist()
    rids.sort(key=_race_id_to_unix)
    n_valid = max(int(len(rids) * valid_frac), 1)
    valid_rids = set(rids[-n_valid:])
    return df[df["race_id"].isin(valid_rids)].copy()


@app.command()
def main(
    valid_frac: float = typer.Option(0.2, "--valid-frac"),
    input_path: Path = typer.Option(DATASETS_DIR / "all.parquet", "--input", "-i"),
    model_path: Path = typer.Option(MODELS_DIR / "lgbm_lambdarank.txt", "--model"),
    meta_path: Path = typer.Option(MODELS_DIR / "lgbm_metadata.json", "--meta"),
    stake_per_race: int = typer.Option(100, "--stake", help="1 レースあたりの単勝賭け金"),
):
    if not input_path.exists():
        console.print(f"[red]not found: {input_path}[/red]")
        raise typer.Exit(1)
    if not model_path.exists() or not meta_path.exists():
        console.print(f"[red]model or meta not found[/red]")
        raise typer.Exit(1)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    feature_cols: list[str] = list(meta["feature_cols"])

    df = pd.read_parquet(input_path)
    valid = _split_valid(df, valid_frac=valid_frac)
    n_total_races = valid["race_id"].nunique()
    valid = valid[valid["race_id"].isin(
        valid.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )].copy()
    n_eval_races = valid["race_id"].nunique()
    console.print(
        f"valid set: {n_eval_races}/{n_total_races} races have finish_pos (others dropped)"
    )

    booster = lgb.Booster(model_file=str(model_path))

    valid = valid.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    X = valid[feature_cols].astype("float64").fillna(0.0)
    valid["score"] = booster.predict(X.values, num_iteration=booster.best_iteration)

    # レース内 softmax: LightGBM score → モデル確率 (sum=1 per race)
    def _race_softmax(s: pd.Series) -> pd.Series:
        m = s.max()
        ex = np.exp(s - m)
        return ex / ex.sum()
    valid["lgbm_prob"] = valid.groupby("race_id", sort=False)["score"].transform(_race_softmax)

    # 市場暗黙率: 1/win_odds をレース内正規化 → power_method_overround を 1 レースずつ適用
    def _market_prob(g: pd.DataFrame) -> pd.Series:
        raw = {int(row.horse_number): (1.0 / row.win_odds) if row.win_odds and row.win_odds > 0 else 0.0
               for row in g.itertuples()}
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
        return pd.Series([corrected.get(int(row.horse_number), 0.0) for row in g.itertuples()], index=g.index)
    valid["market_prob"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(_market_prob, include_groups=False)
    )

    def _blend_loglinear(g: pd.DataFrame, beta: float) -> pd.Series:
        alpha = max(1.0 - beta, 0.0)
        logs = []
        for row in g.itertuples():
            f = max(row.lgbm_prob, 1e-9)
            pi = max(row.market_prob, 1e-9)
            logs.append(alpha * math.log(f) + beta * math.log(pi))
        arr = np.array(logs)
        ex = np.exp(arr - arr.max())
        return pd.Series(ex / ex.sum(), index=g.index)

    blend_cols: dict[str, float] = {
        "blend_b00": 0.0,
        "blend_b02": 0.2,
        "blend_b04": 0.4,
        "blend_b06": 0.6,
        "blend_b07": 0.7,
        "blend_b075": 0.75,
        "blend_b08": 0.8,
        "blend_b085": 0.85,
        "blend_b09": 0.9,
        "blend_b095": 0.95,
        "blend_b10": 1.0,
    }
    for col, beta in blend_cols.items():
        valid[col] = (
            valid.groupby("race_id", sort=False, group_keys=False)
            .apply(lambda g, b=beta: _blend_loglinear(g, b), include_groups=False)
        )

    def metrics_for(score_col: str, label: str) -> dict:
        top_hits = {1: 0, 3: 0, 5: 0}
        stake_total = 0
        payout_total = 0
        wins_on_top1 = 0
        for _rid, g in valid.groupby("race_id", sort=False):
            g_sorted = g.sort_values(score_col, ascending=False).reset_index(drop=True)
            winner_idx = g_sorted.index[g_sorted["target_top1"] == 1]
            if len(winner_idx) == 0:
                continue
            rank = int(winner_idx[0]) + 1  # 1-based
            for k in top_hits:
                if rank <= k:
                    top_hits[k] += 1
            # 単勝 ROI: top-1 馬に stake
            top1_row = g_sorted.iloc[0]
            stake_total += stake_per_race
            if top1_row["target_top1"] == 1:
                wo = float(top1_row["win_odds"]) if top1_row["win_odds"] and top1_row["win_odds"] > 0 else 0.0
                payout_total += int(stake_per_race * wo)
                wins_on_top1 += 1
        n = n_eval_races
        return {
            "label": label,
            "top1": top_hits[1] / n,
            "top3": top_hits[3] / n,
            "top5": top_hits[5] / n,
            "tansho_hits": wins_on_top1,
            "tansho_stake": stake_total,
            "tansho_payout": payout_total,
            "tansho_roi": (payout_total / stake_total) if stake_total else 0.0,
        }

    rows: list[dict] = []
    rows.append(metrics_for("lgbm_prob", "LightGBM (pure, β=0.0)"))
    rows.append(metrics_for("market_prob", "Market only (β=1.0, de-overround)"))
    for col, beta in blend_cols.items():
        if beta in (0.0, 1.0):  # 既に出してるのでスキップ
            continue
        rows.append(metrics_for(col, f"Loglinear blend β={beta:.2f}"))

    tbl = Table(title=f"Holdout evaluation — n={n_eval_races} races")
    tbl.add_column("Model", style="bold")
    tbl.add_column("top-1", justify="right")
    tbl.add_column("top-3", justify="right")
    tbl.add_column("top-5", justify="right")
    tbl.add_column("単勝 hits", justify="right")
    tbl.add_column("単勝 ROI", justify="right")
    tbl.add_column("payout", justify="right")
    for m in rows:
        roi = m["tansho_roi"]
        if roi >= 1.0:
            roi_str = f"[bold green]{roi*100:.1f}%[/]"
        elif roi >= 0.80:
            roi_str = f"[green]{roi*100:.1f}%[/]"
        else:
            roi_str = f"[red]{roi*100:.1f}%[/]"
        tbl.add_row(
            m["label"],
            f"{m['top1']*100:.1f}%",
            f"{m['top3']*100:.1f}%",
            f"{m['top5']*100:.1f}%",
            f"{m['tansho_hits']}",
            roi_str,
            f"¥{m['tansho_payout']:,}",
        )
    console.print(tbl)

    market_row = next(r for r in rows if r["label"].startswith("Market only"))
    best = max(rows, key=lambda r: r["tansho_roi"])
    console.print(
        f"\nbest ROI variant: [bold]{best['label']}[/bold] "
        f"ROI={best['tansho_roi']*100:.1f}% "
        f"(Δ vs Market only: {(best['tansho_roi']-market_row['tansho_roi'])*100:+.2f} pt)"
    )
    prod = next((r for r in rows if r["label"].startswith("Loglinear blend β=0.40")), None)
    if prod:
        console.print(
            f"production setting (β=0.4): ROI={prod['tansho_roi']*100:.1f}% "
            f"(Δ vs Market only: {(prod['tansho_roi']-market_row['tansho_roi'])*100:+.2f} pt)"
        )
    console.print(
        "[dim]控除率 20% より単勝 ROI 期待値は ~80%。"
        "blend β を上げる = 市場寄り。モデル寄り (β 小) が市場単独を上回るなら学習が効いている。[/dim]"
    )


if __name__ == "__main__":
    app()
