"""学習データセット構築 — data/raw/<rid>-*.html.gz から DataFrame を作る。

各レース 1 行 / 1 馬で展開。labels:
  - finish_pos: 1, 2, 3, ... 18 (4 着以下も含む)
  - target_top1: 1 着なら 1 (LightGBM binary)
  - target_top3: 3 着以内なら 1
  - target_rank: 着順 reciprocal (lambdarank label に使う)

入力:
  data/raw/<rid>-shutuba.html.gz   ← 出馬表 (馬番, 騎手, 斤量, 馬体重, 単勝オッズ)
  data/raw/<rid>-past.html.gz      ← 馬柱 (過去 5 走 → Layer 1 特徴量)
  data/raw/<rid>-result.html.gz    ← 結果 (finish_order)

リーク防止:
  - 当該レースの「現在のオッズ」「人気」は学習特徴量として使わない (umaro_ai 流)
  - 過去走の集計は当該レース日付以前のもののみ (馬柱は構造的にそうなっている)
  - スピード指数のクラス指数は当該過去走のクラスで計算
"""
from __future__ import annotations

import gzip
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd
import typer
from rich.console import Console

from .features import build_features
from .models import RaceData
from .parse import parse_past_runs, parse_result, parse_shutuba, race_date_from_html
from .scrape import is_nar_race_id

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DATASETS_DIR = ROOT / "data" / "datasets"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


def _read_gz(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _iter_race_ids() -> Iterator[str]:
    """data/raw/ に shutuba + past + result の 3 つが揃っている race_id を yield。"""
    seen: set[str] = set()
    for p in sorted(RAW_DIR.glob("*-shutuba.html.gz")):
        # p.name = "<rid>-shutuba.html.gz" → race_id を `-shutuba` の前で切る
        rid = p.name.split("-shutuba")[0]
        if rid in seen:
            continue
        if (RAW_DIR / f"{rid}-past.html.gz").exists() and (RAW_DIR / f"{rid}-result.html.gz").exists():
            seen.add(rid)
            yield rid


def load_race(race_id: str) -> tuple[RaceData, dict | None] | None:
    """1 レースぶんの HTML を読んで (RaceData (past_runs 注入済), result_dict) を返す。"""
    sh = _read_gz(RAW_DIR / f"{race_id}-shutuba.html.gz")
    if not sh:
        return None
    rd = parse_shutuba(sh, race_id=race_id)
    past = _read_gz(RAW_DIR / f"{race_id}-past.html.gz")
    if past:
        runs = parse_past_runs(past)
        for h in rd.race.horses:
            h.past_runs = runs.get(h.number, [])
    res_html = _read_gz(RAW_DIR / f"{race_id}-result.html.gz")
    res = parse_result(res_html) if res_html else None
    return rd, res


def _race_date(rid: str, rd: RaceData) -> str:
    """開催日 "YYYYMMDD" (時系列 split 用)。取れなければ "" (build がmiss 数を報告)。

    旧 train.py は int(race_id) ソートで時系列 split していたが、race_id の桁順は
    年→場コード→日付 なので年内は「会場 split」になっていた (2026-06-10 レビュー)。
    NAR rid は日付を内包 (YYYY+PP+MMDD+RR)。JRA rid は開催回+日で日付が無いので
    start_at (タイトルの「YYYY年M月D日」由来) → raw HTML の日付 regex の順で取る。
    """
    if is_nar_race_id(rid) and len(rid) == 12 and rid[6:10].isdigit():
        return rid[:4] + rid[6:10]
    st = int(getattr(rd.race, "start_at", 0) or 0)
    if st > 0:
        return datetime.fromtimestamp(st).strftime("%Y%m%d")
    sh = _read_gz(RAW_DIR / f"{rid}-shutuba.html.gz")
    return race_date_from_html(sh or "")


def build_dataframe(race_ids: list[str]) -> pd.DataFrame:
    """各レース × 各馬 1 行で DataFrame を作る。

    Columns:
      race_id, race_date (YYYYMMDD, 時系列 split 用),
      horse_number, finish_pos (NaN if absent or 4着以下),
      target_top1, target_top3, target_rank,
      [feature columns from FeatureVec]
    """
    rows = []
    for rid in race_ids:
        loaded = load_race(rid)
        if loaded is None:
            continue
        rd, res = loaded
        race_date = _race_date(rid, rd)
        # ラベル: 結果から取れたら使う。なければ全 NaN (= レースは特徴量だけ使える)
        finish_lookup: dict[int, int] = {}
        if res and len(res.get("finish_order") or []) >= 3:
            for i, num in enumerate(res["finish_order"]):
                finish_lookup[int(num)] = i + 1  # 1..3 着
        feats = build_features(rd)
        for h in rd.race.horses:
            if h.absent:
                continue
            fv = feats.get(h.number)
            if fv is None:
                continue
            finish_pos = finish_lookup.get(h.number)
            row = {
                "race_id": rid,
                "race_date": race_date,
                "venue": rd.race.venue_name,
                "race_no": rd.race.race_number,
                "distance": rd.race.distance,
                "surface": rd.race.surface,
                # going = 馬場状態 (良/稍重/重/不良)。weather_text ("晴 / 良" 等の combined)
                # を入れていた旧実装は race_class_diagnostic.py の Going bin が
                # 天気で分割される歪みを生んでいた。track_condition だけを取る。
                "going": (rd.race.weather.track_condition if rd.race.weather else ""),
                "horse_number": h.number,
                "n_horses": len([x for x in rd.race.horses if not x.absent]),
                # ラベル (3 着以内のみ確定、それ以外 None = 4 着以下 or 未確定)
                "finish_pos": finish_pos,
                "target_top1": 1 if finish_pos == 1 else 0,
                "target_top3": 1 if finish_pos and finish_pos <= 3 else 0,
                # lambdarank label: 4 - rank (top=3, 2nd=2, 3rd=1, others=0)
                "target_rank": (4 - finish_pos) if finish_pos and finish_pos <= 3 else 0,
                # 特徴量
                **{k: v for k, v in asdict(fv).items() if k != "number"},
            }
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


@app.command()
def build(
    output: Path = typer.Option(
        DATASETS_DIR / "all.parquet", "--output", "-o",
        help="出力 parquet ファイル",
    ),
    csv: bool = typer.Option(False, "--csv", help="parquet ではなく CSV で書く"),
    limit: int | None = typer.Option(None, "--limit", help="最初の N レースだけ"),
):
    """data/raw/ にある全 race の特徴量 + ラベルを集約。"""
    rids = list(_iter_race_ids())
    if limit:
        rids = rids[:limit]
    if not rids:
        console.print("[yellow]data/raw/ に完全な race セットがありません[/yellow]")
        raise typer.Exit(1)

    console.print(f"building dataset from {len(rids)} races ...")
    df = build_dataframe(rids)
    if df.empty:
        console.print("[yellow]empty dataframe[/yellow]")
        raise typer.Exit(1)

    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    if csv:
        out = output.with_suffix(".csv")
        df.to_csv(out, index=False)
    else:
        df.to_parquet(output, index=False)
        out = output
    console.print(
        f"[green]saved {out} — rows={len(df):,} races={df['race_id'].nunique():,} "
        f"cols={len(df.columns)}[/green]"
    )
    # サマリ
    completed = df["finish_pos"].notna().sum()
    console.print(
        f"  完走確定 (1-3 着付与済) rows: {completed:,} / "
        f"特徴量列 数: {len([c for c in df.columns if c not in ['race_id','race_date','venue','race_no','distance','surface','going','horse_number','n_horses','finish_pos','target_top1','target_top3','target_rank']]):,}"
    )
    # race_date 欠落チェック (時系列 split の前提。欠落レースは sort 先頭 = train 側に落ちる)
    miss_dates = df.loc[df["race_date"] == "", "race_id"].nunique()
    if miss_dates:
        console.print(f"[yellow]race_date 欠落: {miss_dates:,} races — split 順序が崩れるので要調査[/yellow]")
    else:
        console.print(f"  race_date: 全 {df['race_id'].nunique():,} races で取得済")


if __name__ == "__main__":
    app()
