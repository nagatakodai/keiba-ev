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

import gzip
import json
import math
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .ev import (
    DEFAULT_LAMBDA_2,
    DEFAULT_LAMBDA_3,
    LGBM_TEMPERATURE,
    PXO_CHUANA,
    PXO_FLOOR,
    PXO_HONSEN,
    EvRow,
    plan_aptitude_ev,
    plan_balanced,
    plan_final,
    plan_hit_pure,
    plan_hit_safe,
    plan_max_ev,
    plan_wide,
    power_method_overround,
)
from .parse import parse_result

ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT / "data" / "datasets"
MODELS_DIR = ROOT / "data" / "models"
RAW_DIR = ROOT / "data" / "raw"
TRIFECTA_CACHE_DIR = ROOT / "data" / "cache" / "trifecta_odds"
APTITUDE_CACHE_DIR = ROOT / "data" / "cache" / "aptitudes"

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
    temperature: float = typer.Option(
        LGBM_TEMPERATURE, "--temperature", "-T",
        help=(
            "LGBM softmax の温度 (T<1 で sharpening, T>1 で flattening, "
            "既定 = src.ev.LGBM_TEMPERATURE = production と同じ値)"
        ),
    ),
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
    # T (temperature) で sharpness 調整: probs = softmax(score / T)
    T_eff = max(temperature, 1e-3)
    console.print(
        f"[dim]temperature T = {T_eff} "
        f"({'sharpening' if T_eff < 1 else ('flattening' if T_eff > 1 else 'identity')})[/dim]"
    )
    def _race_softmax(s: pd.Series) -> pd.Series:
        scaled = s / T_eff
        m = scaled.max()
        ex = np.exp(scaled - m)
        return ex / ex.sum()
    valid["lgbm_prob"] = valid.groupby("race_id", sort=False)["score"].transform(_race_softmax)

    # --- 温度スケーリング (T sweep): softmax(score / T) で sharpness 調整 ---
    # 確率モデルの楽観バイアスを補正する単純な calibration。
    # T > 1 で分布が平坦化 (model 過信を抑制)、T < 1 で sharper。
    # T=1 が現状、T = ∞ で uniform に近づく。
    # 評価指標: log loss (winner の prob で評価)、top-1 hit rate。
    console.rule("[bold]Temperature scaling sweep (LGBM softmax sharpness)[/bold]")
    t_grid = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)
    t_rows: list[dict] = []
    for T in t_grid:
        col = f"lgbm_prob_T{T}"
        valid[col] = valid.groupby("race_id", sort=False)["score"].transform(
            lambda s, t=T: (lambda m: (lambda ex: ex / ex.sum())(np.exp((s - m) / t)))(s.max())
        )
        # log loss: 各レースの winner の prob で -log
        ll_sum = 0.0
        top1_hits = 0
        n_r = 0
        for _rid, g in valid.groupby("race_id", sort=False):
            winner = g[g["target_top1"] == 1]
            if len(winner) != 1:
                continue
            p = float(winner[col].iloc[0])
            ll_sum += -math.log(max(p, 1e-12))
            # top-1 hit
            top_idx = g[col].idxmax()
            if g.loc[top_idx, "target_top1"] == 1:
                top1_hits += 1
            n_r += 1
        t_rows.append({
            "T": T,
            "log_loss": ll_sum / n_r if n_r else 0.0,
            "top1": top1_hits / n_r if n_r else 0.0,
            "n": n_r,
        })

    tbl_t = Table(title="Temperature scaling — LGBM softmax sharpness")
    tbl_t.add_column("T", justify="right")
    tbl_t.add_column("log loss (winner)", justify="right")
    tbl_t.add_column("top-1 hit", justify="right")
    for r in t_rows:
        marker = " ←" if r["T"] == min(t_rows, key=lambda x: x["log_loss"])["T"] else ""
        tbl_t.add_row(
            f"{r['T']:.2f}{marker}",
            f"{r['log_loss']:.4f}",
            f"{r['top1']*100:.1f}%",
        )
    console.print(tbl_t)
    best_T = min(t_rows, key=lambda x: x["log_loss"])["T"]
    cur_ll = next(r for r in t_rows if r["T"] == 1.0)["log_loss"]
    best_ll = next(r for r in t_rows if r["T"] == best_T)["log_loss"]
    console.print(
        f"best T = [bold]{best_T}[/bold] (log loss {best_ll:.4f}, "
        f"T=1 比 {(cur_ll - best_ll):.4f} 改善)"
    )

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

    # ---- 3 連単 Plackett-Luce 連鎖 top-K hit rate ----
    # production の estimate_probs が 3 連単確率を生成する手順:
    #   place2[i] = win[i]^λ_2 * show_bias[i]
    #   place3[i] = win[i]^λ_3 * show_bias[i]
    #   p(a,b,c)  = s1[a]/Σs1 * s2[b]/(Σs2-s2[a]) * s3[c]/(Σs3-s3[a]-s3[b])
    # オッズは無いので ROI は出せない (※ 3 連単 odds は raw HTML にしかない)。
    # その代わり、実着順 triple がモデル確率ランクの top-K に入る割合を測る。
    # 単勝 ROI と一緒に β を選ぶ判断材料になる。
    console.rule("[bold]3 連単 top-K hit rate (Plackett-Luce, use_show_bias=True)[/bold]")

    # 3 連単 払戻金キャッシュ: race_id → payout (¥100 賭けに対する払戻)
    # raw HTML から事前に 1 度だけパースする。result HTML 無い / payout 不明は除外。
    payout_cache: dict[str, int] = {}
    for rid in valid["race_id"].unique():
        rp = RAW_DIR / f"{rid}-result.html.gz"
        if not rp.exists():
            continue
        try:
            html = gzip.open(rp, "rt", encoding="utf-8").read()
        except OSError:
            continue
        try:
            parsed = parse_result(html)
        except Exception:
            continue
        if parsed and parsed.get("payout"):
            payout_cache[rid] = int(parsed["payout"])
    console.print(
        f"trifecta payouts loaded: {len(payout_cache)}/{n_eval_races} races"
    )

    def trifecta_topk_for(score_col: str, label: str) -> dict:
        # K grid: K=1-7 で Plan H1 の target 最適化、K=10/100/1000 で曲線形を確認
        ks = (1, 2, 3, 4, 5, 6, 7, 10, 100, 1000)
        hits = {k: 0 for k in ks}
        # synthetic ROI: top-K 機械購入 (¥100/pt) — 的中時 payout
        roi_stake = {k: 0 for k in ks}
        roi_payout = {k: 0 for k in ks}
        rank_sum = 0
        rank_n = 0
        for rid, g in valid.groupby("race_id", sort=False):
            n = len(g)
            if n < 3:
                continue
            win_arr = g[score_col].to_numpy()
            show = g["shrunk_show_rate"].to_numpy()
            avg_show = float(np.mean(show)) if n > 0 else 0.0
            bias = np.clip(show / avg_show, 0.1, None) if avg_show > 0 else np.ones(n)
            s1 = np.maximum(win_arr, 1e-12)
            s2 = (s1 ** DEFAULT_LAMBDA_2) * bias
            s3 = (s1 ** DEFAULT_LAMBDA_3) * bias
            sum1 = float(s1.sum())
            sum2 = float(s2.sum())
            sum3 = float(s3.sum())
            if sum1 <= 0 or sum2 <= 0 or sum3 <= 0:
                continue
            # 全 ordered triple の PL prob (n,n,n) を vectorized で構築
            w1 = s1 / sum1
            denom2 = sum2 - s2[:, None]  # i 行: i を除いた時の Σ
            w2 = s2[None, :] / np.maximum(denom2, 1e-12)
            np.fill_diagonal(w2, 0.0)
            denom3 = sum3 - s3[:, None, None] - s3[None, :, None]
            w3 = s3[None, None, :] / np.maximum(denom3, 1e-12)
            # mask: i==j, i==k, j==k 全て無効
            for ii in range(n):
                w3[ii, ii, :] = 0.0
                w3[ii, :, ii] = 0.0
                w3[:, ii, ii] = 0.0
            p_cube = w1[:, None, None] * w2[:, :, None] * w3

            finish = g["finish_pos"].to_numpy()
            a_idx = np.where(finish == 1.0)[0]
            b_idx = np.where(finish == 2.0)[0]
            c_idx = np.where(finish == 3.0)[0]
            if len(a_idx) != 1 or len(b_idx) != 1 or len(c_idx) != 1:
                continue
            a, b, c = int(a_idx[0]), int(b_idx[0]), int(c_idx[0])
            actual_p = float(p_cube[a, b, c])
            # rank = 自分より prob が大きい triple の数 + 1
            higher = int((p_cube > actual_p).sum())
            rank = higher + 1
            rank_sum += rank
            rank_n += 1
            payout = payout_cache.get(rid, 0)
            for k in ks:
                if rank <= k:
                    hits[k] += 1
                # ROI 集計は payout が取れたレースのみ (公平な分母)
                if payout > 0:
                    roi_stake[k] += k * 100
                    if rank <= k:
                        roi_payout[k] += payout
        roi = {k: (roi_payout[k] / roi_stake[k] if roi_stake[k] else 0.0) for k in ks}
        return {
            "label": label,
            "n_races": rank_n,
            "topk": {k: (hits[k] / rank_n if rank_n else 0.0) for k in ks},
            "mean_rank": (rank_sum / rank_n) if rank_n else 0.0,
            "roi": roi,
            "roi_stake": roi_stake,
            "roi_payout": roi_payout,
        }

    trifecta_rows: list[dict] = []
    trifecta_rows.append(trifecta_topk_for("lgbm_prob", "LightGBM (pure, β=0.0)"))
    trifecta_rows.append(trifecta_topk_for("market_prob", "Market only (β=1.0)"))
    for col, beta in blend_cols.items():
        if beta in (0.0, 1.0):
            continue
        trifecta_rows.append(trifecta_topk_for(col, f"Loglinear blend β={beta:.2f}"))

    tbl3 = Table(title=f"3 連単 top-K hit (n={trifecta_rows[0]['n_races']} races)")
    tbl3.add_column("Model", style="bold")
    tbl3.add_column("top-1", justify="right")
    tbl3.add_column("top-3", justify="right")
    tbl3.add_column("top-5", justify="right")
    tbl3.add_column("top-10", justify="right")
    tbl3.add_column("top-100", justify="right")
    tbl3.add_column("mean rank", justify="right")
    for m in trifecta_rows:
        tbl3.add_row(
            m["label"],
            f"{m['topk'][1]*100:.2f}%",
            f"{m['topk'][3]*100:.2f}%",
            f"{m['topk'][5]*100:.2f}%",
            f"{m['topk'][10]*100:.1f}%",
            f"{m['topk'][100]*100:.1f}%",
            f"{m['mean_rank']:.1f}",
        )
    console.print(tbl3)

    market_t = next(r for r in trifecta_rows if r["label"].startswith("Market only"))
    best_t = max(trifecta_rows, key=lambda r: r["topk"][10])
    console.print(
        f"\nbest top-10 variant: [bold]{best_t['label']}[/bold] "
        f"top-10={best_t['topk'][10]*100:.2f}% "
        f"(Δ vs Market only: {(best_t['topk'][10]-market_t['topk'][10])*100:+.2f} pt)"
    )
    prod_t = next((r for r in trifecta_rows if r["label"].startswith("Loglinear blend β=0.40")), None)
    new_def_t = next((r for r in trifecta_rows if r["label"].startswith("Loglinear blend β=0.80")), None)
    if prod_t and new_def_t:
        console.print(
            f"旧既定 β=0.4: top-10={prod_t['topk'][10]*100:.2f}%, "
            f"新既定 β=0.8: top-10={new_def_t['topk'][10]*100:.2f}% "
            f"(Δ {(new_def_t['topk'][10]-prod_t['topk'][10])*100:+.2f} pt)"
        )
    console.print(
        "[dim]3 連単 top-10 hit rate ≒ 「10 点買えば 1 着」確率。"
        "市場 baseline を上回るほどモデルが triple ranking に効いている。[/dim]"
    )

    # ---- 3 連単 synthetic ROI (top-K 機械購入) ----
    # 各レースで「モデル確率の高い順 top-K triple」をそれぞれ ¥100 で買う想定。
    # 的中時 payout = result.html の 3 連単払戻金。
    # 注意: これは "Plan A/B/C" の本番ロジックではなく、純粋な top-K 機械購入。
    # 控除率 22.5% → 期待 ROI は ~77.5%。100% を超えれば長期黒字。
    if payout_cache:
        console.rule("[bold]3 連単 synthetic top-K 購入 ROI[/bold]")
        tbl4 = Table(
            title=f"3 連単 top-K 機械購入 ROI (¥100/pt, 払戻あり n={len(payout_cache)} races)"
        )
        tbl4.add_column("Model", style="bold")
        roi_ks = (1, 2, 3, 5, 10, 100)
        for k in roi_ks:
            tbl4.add_column(f"top-{k}", justify="right")
        for m in trifecta_rows:
            cells = [m["label"]]
            for k in roi_ks:
                roi = m["roi"][k]
                if roi >= 1.0:
                    s = f"[bold green]{roi*100:.1f}%[/]"
                elif roi >= 0.775:
                    s = f"[green]{roi*100:.1f}%[/]"
                else:
                    s = f"[red]{roi*100:.1f}%[/]"
                cells.append(s)
            tbl4.add_row(*cells)
        console.print(tbl4)

        best_roi_10 = max(trifecta_rows, key=lambda r: r["roi"][10])
        market_roi_10 = market_t["roi"][10]
        console.print(
            f"\nbest top-10 ROI: [bold]{best_roi_10['label']}[/bold] "
            f"ROI={best_roi_10['roi'][10]*100:.1f}% "
            f"(Δ vs Market only: {(best_roi_10['roi'][10]-market_roi_10)*100:+.2f} pt)"
        )
        if prod_t and new_def_t:
            console.print(
                f"旧既定 β=0.4: top-10 ROI={prod_t['roi'][10]*100:.1f}%, "
                f"新既定 β=0.8: top-10 ROI={new_def_t['roi'][10]*100:.1f}% "
                f"(Δ {(new_def_t['roi'][10]-prod_t['roi'][10])*100:+.2f} pt)"
            )
        console.print(
            "[dim]控除率 22.5% より理論期待 ROI は ~77.5%。"
            "実 ROI が 77.5% を超え、かつ市場単独より高い β が β=0.78 の妥当性。[/dim]"
        )

    # ---- Plan A/B/C/H1 synthetic ROI (real trifecta odds から production plan_*) ----
    # data/cache/trifecta_odds/{race_id}.json があれば、scrape 済の real odds で
    # production の plan_balanced / plan_max_ev / plan_wide / plan_hit_pure を回す。
    # この section だけが真の β 決定証拠になる (他は単勝/PL hit/合成 ROI の参考値)。
    odds_cache: dict[str, dict[tuple[int, int, int], tuple[float, int]]] = {}
    if TRIFECTA_CACHE_DIR.exists():
        for j in TRIFECTA_CACHE_DIR.glob("*.json"):
            try:
                d = json.loads(j.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rid = d.get("race_id") or j.stem
            triplets = d.get("trifecta") or []
            mp: dict[tuple[int, int, int], tuple[float, int]] = {}
            for t in triplets:
                key = tuple(t["key"])
                if len(key) != 3:
                    continue
                try:
                    mp[(int(key[0]), int(key[1]), int(key[2]))] = (
                        float(t["odds"]), int(t.get("popularity") or 0)
                    )
                except (ValueError, TypeError):
                    continue
            odds_cache[rid] = mp

    # Aptitude top horses キャッシュ (Plan G で使う)
    aptitude_cache: dict[str, list[int]] = {}
    if APTITUDE_CACHE_DIR.exists():
        for j in APTITUDE_CACHE_DIR.glob("*.json"):
            try:
                d = json.loads(j.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rid = d.get("race_id") or j.stem
            top = d.get("aptitude_top_horses") or []
            aptitude_cache[rid] = [int(h) for h in top]

    if odds_cache and payout_cache:
        n_odds = sum(1 for rid in valid["race_id"].unique() if rid in odds_cache)
        console.rule(
            f"[bold]Plan ROI (real trifecta odds, n={n_odds} races, "
            f"aptitude cached={len(aptitude_cache)})[/bold]"
        )

        def _tier(pxo: float) -> str:
            if pxo < PXO_FLOOR:
                return "minus"
            if pxo <= PXO_HONSEN[1]:
                return "honsen"
            if pxo <= PXO_CHUANA[1]:
                return "chuana"
            return "oana"

        def plan_roi_for(score_col: str, label: str) -> dict:
            # Plan G は適性ゲートが必要 (apt_cache 経由)。Plan F は other plans の union。
            plan_codes = ("A", "B", "C", "G", "H1", "H2", "F")
            stake: dict[str, int] = {c: 0 for c in plan_codes}
            payout: dict[str, int] = {c: 0 for c in plan_codes}
            hits: dict[str, int] = {c: 0 for c in plan_codes}
            points: dict[str, int] = {c: 0 for c in plan_codes}
            n_used = 0
            for rid, g in valid.groupby("race_id", sort=False):
                if rid not in odds_cache or rid not in payout_cache:
                    continue
                n = len(g)
                if n < 3:
                    continue
                horse_numbers = g["horse_number"].to_numpy().astype(int)
                show = g["shrunk_show_rate"].to_numpy()
                avg_show = float(np.mean(show)) if n > 0 else 0.0
                bias = (
                    np.clip(show / avg_show, 0.1, None)
                    if avg_show > 0 else np.ones(n)
                )

                # Model PL prob cube (β に依存)
                w = np.maximum(g[score_col].to_numpy(), 1e-12)
                s1 = w
                s2 = (s1 ** DEFAULT_LAMBDA_2) * bias
                s3 = (s1 ** DEFAULT_LAMBDA_3) * bias
                S1 = float(s1.sum()); S2 = float(s2.sum()); S3 = float(s3.sum())
                if S1 <= 0 or S2 <= 0 or S3 <= 0:
                    continue
                w1 = s1 / S1
                w2 = s2[None, :] / np.maximum(S2 - s2[:, None], 1e-12)
                np.fill_diagonal(w2, 0.0)
                w3 = s3[None, None, :] / np.maximum(
                    S3 - s3[:, None, None] - s3[None, :, None], 1e-12
                )
                for ii in range(n):
                    w3[ii, ii, :] = 0.0
                    w3[ii, :, ii] = 0.0
                    w3[:, ii, ii] = 0.0
                p_cube = w1[:, None, None] * w2[:, :, None] * w3

                # EvRow list (key, odds, popularity, prob, px_o)
                race_odds = odds_cache[rid]
                ev_rows: list[EvRow] = []
                for i in range(n):
                    for j in range(n):
                        if j == i:
                            continue
                        for k in range(n):
                            if k == i or k == j:
                                continue
                            key = (
                                int(horse_numbers[i]),
                                int(horse_numbers[j]),
                                int(horse_numbers[k]),
                            )
                            entry = race_odds.get(key)
                            if entry is None:
                                continue
                            odds, popularity = entry
                            if odds <= 0:
                                continue
                            p = float(p_cube[i, j, k])
                            pxo = p * odds
                            ev_rows.append(EvRow(
                                key=key, odds=odds, popularity=popularity,
                                prob=p, px_o=pxo, tier=_tier(pxo),
                            ))
                if not ev_rows:
                    continue
                ev_rows.sort(key=lambda r: r.px_o, reverse=True)

                finish = g["finish_pos"].to_numpy()
                a_idx = np.where(finish == 1.0)[0]
                b_idx = np.where(finish == 2.0)[0]
                c_idx = np.where(finish == 3.0)[0]
                if len(a_idx) != 1 or len(b_idx) != 1 or len(c_idx) != 1:
                    continue
                actual_key = (
                    int(horse_numbers[a_idx[0]]),
                    int(horse_numbers[b_idx[0]]),
                    int(horse_numbers[c_idx[0]]),
                )
                real_payout = payout_cache.get(rid, 0)
                n_used += 1

                apt_top = aptitude_cache.get(rid, [])
                picks_a = plan_balanced(ev_rows)
                picks_b = plan_max_ev(ev_rows)
                picks_c = plan_wide(ev_rows)
                picks_g = plan_aptitude_ev(ev_rows, apt_top) if apt_top else []
                picks_h1 = plan_hit_pure(ev_rows, target=3)
                picks_h2 = plan_hit_safe(ev_rows, target=3)
                picks_f = plan_final(picks_a, picks_b, picks_c, picks_g, picks_h1, picks_h2)

                for code, picks in (
                    ("A", picks_a), ("B", picks_b), ("C", picks_c),
                    ("G", picks_g), ("H1", picks_h1), ("H2", picks_h2),
                    ("F", picks_f),
                ):
                    n_pts = len(picks)
                    if n_pts == 0:
                        continue
                    stake[code] += n_pts * 100
                    points[code] += n_pts
                    if any(r.key == actual_key for r in picks):
                        payout[code] += real_payout
                        hits[code] += 1
            return {
                "label": label,
                "n_used": n_used,
                "plans": {
                    code: {
                        "stake": stake[code],
                        "payout": payout[code],
                        "hits": hits[code],
                        "points": points[code],
                        "roi": (payout[code] / stake[code]) if stake[code] else 0.0,
                        "avg_pts": (points[code] / n_used) if n_used else 0.0,
                    }
                    for code in plan_codes
                },
            }

        plan_rows: list[dict] = []
        plan_rows.append(plan_roi_for("lgbm_prob", "LightGBM (pure, β=0.0)"))
        plan_rows.append(plan_roi_for("market_prob", "Market only (β=1.0)"))
        for col, beta in blend_cols.items():
            if beta in (0.0, 1.0):
                continue
            plan_rows.append(plan_roi_for(col, f"Loglinear blend β={beta:.2f}"))

        plan_specs_disp = (
            ("A", "5 点バランス"),
            ("B", "最高 EV 集中"),
            ("C", "広め保険"),
            ("G", "適性ゲート"),
            ("H1", "確率上位 3 点"),
            ("H2", "確率 +EV≥1"),
            ("F", "A〜H2 union"),
        )
        used = plan_rows[0]["n_used"] if plan_rows else 0
        tbl5 = Table(
            title=f"Plan A/B/C/G/H1/H2/F real-odds ROI (n={used} races)",
            show_lines=False,
        )
        tbl5.add_column("Model", style="bold")
        for code, _desc in plan_specs_disp:
            tbl5.add_column(f"Plan {code}", justify="right")
        for m in plan_rows:
            cells = [m["label"]]
            for code, _desc in plan_specs_disp:
                d = m["plans"][code]
                if d["stake"] == 0:
                    cells.append("—")
                    continue
                roi = d["roi"]
                if roi >= 1.0:
                    rs = f"[bold green]{roi*100:.0f}%[/]"
                elif roi >= 0.775:
                    rs = f"[green]{roi*100:.0f}%[/]"
                else:
                    rs = f"[red]{roi*100:.0f}%[/]"
                cells.append(f"{rs} h{d['hits']}/{d['avg_pts']:.1f}p")
            tbl5.add_row(*cells)
        console.print(tbl5)

        for code, desc in plan_specs_disp:
            valid_rows = [r for r in plan_rows if r["plans"][code]["stake"] > 0]
            if not valid_rows:
                continue
            best = max(valid_rows, key=lambda r: r["plans"][code]["roi"])
            console.print(
                f"[bold]Plan {code}[/bold] best: {best['label']} "
                f"ROI={best['plans'][code]['roi']*100:.1f}% "
                f"(hits {best['plans'][code]['hits']}, "
                f"avg {best['plans'][code]['avg_pts']:.1f} pt/race)"
            )
        console.print(
            "[dim]Plan A/B/C は P×O ≥ 1.02 で EV filter する production logic。"
            "Plan H1 は EV 不問で確率上位 3 点。控除率 22.5% より理論期待 ROI ~77.5%。"
            "1.0 を超える β が長期黒字。[/dim]"
        )
    elif TRIFECTA_CACHE_DIR.exists() and not odds_cache:
        console.print(
            "[yellow]trifecta odds キャッシュは空。"
            f"`python scripts/fetch_trifecta_odds_holdout.py` で {TRIFECTA_CACHE_DIR.name} を埋めると "
            "Plan A/B/C/H1 の real-odds ROI が出る[/yellow]"
        )
    else:
        console.print(
            "[dim]Plan A/B/C/H1 real-odds ROI を出すには "
            "`python scripts/fetch_trifecta_odds_holdout.py` で trifecta odds を scrape する必要あり。[/dim]"
        )


if __name__ == "__main__":
    app()
