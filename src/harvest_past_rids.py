"""現有 data/raw/*-past.html.gz から過去 race_id を抽出する。

NAR/JRA の `race_list.html?kaisai_date=…` は過去日付を受け付けない (status 400) ため、
代わりに既に取得済の 2026 馬柱から各馬の直近 5 走の race_id を取り出して
「次世代の fetch 対象」を構築する。

世代を回せば再帰的に過去へ遡れる:
  Gen 0: 今日の race_list から取得 (2026 のみ)
  Gen 1: Gen 0 の 馬柱 → 2025 race_id 多数 + 2024 少々 + 2026 同年既知
  Gen 2: Gen 1 の 馬柱 → 2024 以前の race_id 大半
  ...
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR = ROOT / "data" / "cache"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)

RACE_ID_RE = re.compile(r"db\.netkeiba\.com/race/(\d{12})")


def extract_past_race_ids(past_dir: Path = RAW_DIR) -> set[str]:
    """全 *-past.html.gz から past race_id を抽出。"""
    out: set[str] = set()
    files = list(past_dir.glob("*-past.html.gz"))
    if not files:
        return out
    for i, p in enumerate(files):
        if i % 500 == 0 and i > 0:
            console.print(f"  [dim]processed {i}/{len(files)}, ids={len(out):,}[/dim]")
        try:
            html = gzip.open(p, "rt", encoding="utf-8").read()
            for m in RACE_ID_RE.finditer(html):
                out.add(m.group(1))
        except (OSError, EOFError):
            continue
    return out


def existing_race_ids(raw_dir: Path = RAW_DIR) -> set[str]:
    """すでに shutuba (or past) が cache されている race_id 集合。"""
    out = set()
    for p in raw_dir.glob("*-shutuba.html.gz"):
        out.add(p.name.split("-shutuba")[0])
    return out


@app.command()
def main(
    output: Path = typer.Option(
        CACHE_DIR / "rids_harvested.txt", "--output", "-o",
        help="出力 race_id リスト",
    ),
    skip_existing: bool = typer.Option(True, "--skip-existing/--include-existing",
        help="既に raw にある race_id は除外"),
    year_filter: str | None = typer.Option(None, "--year",
        help="特定年だけ (例: 2025)"),
):
    """過去 race_id 一覧を作る。bulk_fetch --rids-file に渡せる形式。"""
    console.print("Scanning data/raw/ for 馬柱 HTML ...")
    all_ids = extract_past_race_ids()
    console.print(f"  found {len(all_ids):,} unique past race_ids in 馬柱")

    target = set(all_ids)
    if year_filter:
        target = {rid for rid in target if rid.startswith(year_filter)}
        console.print(f"  filtered to year={year_filter}: {len(target):,}")

    if skip_existing:
        existing = existing_race_ids()
        target -= existing
        console.print(f"  excluding {len(existing):,} already cached → {len(target):,} todo")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sorted(target)) + "\n")
    console.print(f"[green]saved {len(target):,} race_ids → {output}[/green]")

    # 年別内訳
    from collections import Counter
    year_count = Counter(rid[:4] for rid in target)
    console.print("[dim]breakdown by year:[/dim]")
    for y in sorted(year_count):
        console.print(f"  {y}: {year_count[y]:,}")


if __name__ == "__main__":
    app()
