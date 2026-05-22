"""過去レース大量収集 pipeline (resumable, 並列)。

機能:
  - 指定日付範囲 (--since YYYYMMDD --until YYYYMMDD) で JRA / NAR の race_list を引く
  - 各 race_id について shutuba.html / shutuba_past.html / result.html を取得
  - HTML は data/raw/<race_id>-{shutuba,past,result}.html.gz に gzip 保存
  - すでに存在するファイルは skip (resumable)
  - 並列数 (--workers) は Playwright instance 数。各 worker が独立した
    browser instance を持つ。デフォルト 5。
  - error log は data/cache/bulk_fetch_errors.jsonl に追記

使い方:
  # スモーク (直近 3 日)
  python -m src.bulk_fetch --since 20260519 --until 20260521 --workers 3

  # 本実行 (2026 年全体)
  python -m src.bulk_fetch --since 20260101 --until 20260521 --workers 5
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Queue
from threading import Lock

import typer
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .parse import parse_race_list
from .scrape import (
    NAR_HOST,
    UA,
    race_list_url,
    result_url,
    shutuba_past_url,
    shutuba_url,
)

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
ERROR_LOG = ROOT / "data" / "cache" / "bulk_fetch_errors.jsonl"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


# --- データクラス ---


@dataclass
class FetchTarget:
    """1 race × 1 種別の取得対象。"""
    race_id: str
    kind: str   # "shutuba" | "past" | "result"
    url: str
    out_path: Path

    @property
    def already_done(self) -> bool:
        return self.out_path.exists() and self.out_path.stat().st_size > 0


@dataclass
class WorkerStats:
    fetched: int = 0
    skipped: int = 0
    errors: int = 0


# --- 取得 ---


def _gzip_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)


def _log_error(rec: dict) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _make_targets(race_id: str) -> list[FetchTarget]:
    return [
        FetchTarget(race_id=race_id, kind="shutuba", url=shutuba_url(race_id),
                    out_path=RAW_DIR / f"{race_id}-shutuba.html.gz"),
        FetchTarget(race_id=race_id, kind="past", url=shutuba_past_url(race_id),
                    out_path=RAW_DIR / f"{race_id}-past.html.gz"),
        FetchTarget(race_id=race_id, kind="result", url=result_url(race_id),
                    out_path=RAW_DIR / f"{race_id}-result.html.gz"),
    ]


def _worker_loop(
    queue: Queue,
    stats: WorkerStats,
    progress: Progress,
    task_id,
    settle_ms: int,
    timeout_ms: int,
    polite_sleep_ms: int,
) -> None:
    """1 worker thread。自分の Playwright browser を持って queue から target を取り処理。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=UA, locale="ja-JP",
            viewport={"width": 1280, "height": 1800},
        )
        page = ctx.new_page()
        try:
            while True:
                target = queue.get()
                if target is None:
                    queue.task_done()
                    break
                try:
                    if target.already_done:
                        stats.skipped += 1
                    else:
                        page.goto(target.url, wait_until="domcontentloaded", timeout=timeout_ms)
                        page.wait_for_timeout(settle_ms)
                        html = page.content()
                        _gzip_write(target.out_path, html)
                        stats.fetched += 1
                        if polite_sleep_ms > 0:
                            page.wait_for_timeout(polite_sleep_ms)
                except Exception as ex:  # noqa: BLE001
                    stats.errors += 1
                    _log_error({
                        "race_id": target.race_id,
                        "kind": target.kind,
                        "url": target.url,
                        "error": f"{type(ex).__name__}: {str(ex)[:300]}",
                        "ts": int(time.time()),
                    })
                finally:
                    progress.update(task_id, advance=1)
                    queue.task_done()
        finally:
            try:
                browser.close()
            except Exception:
                pass


# --- race_id 列挙 ---


def _date_range(since: str, until: str):
    d0 = datetime.strptime(since, "%Y%m%d").date()
    d1 = datetime.strptime(until, "%Y%m%d").date()
    cur = d0
    while cur <= d1:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=1)


def collect_race_ids(
    since: str,
    until: str,
    *,
    jra: bool = True,
    nar: bool = True,
    settle_ms: int = 3000,
) -> list[str]:
    """各日付の race_list を引いて全 race_id を返す。"""
    out: list[str] = []
    seen: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=UA, locale="ja-JP",
            viewport={"width": 1280, "height": 1800},
        )
        page = ctx.new_page()
        try:
            dates = list(_date_range(since, until))
            for d in dates:
                for is_nar, enabled in [(False, jra), (True, nar)]:
                    if not enabled:
                        continue
                    url = race_list_url(d, nar=is_nar)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(settle_ms)
                        html = page.content()
                        races = parse_race_list(html, d)
                        for r in races:
                            rid = r["race_id"]
                            if rid not in seen:
                                seen.add(rid)
                                out.append(rid)
                        console.print(
                            f"[dim]{d} {'NAR' if is_nar else 'JRA'}: "
                            f"+{len(races)} (total {len(seen)})[/dim]"
                        )
                    except Exception as ex:  # noqa: BLE001
                        console.print(
                            f"[yellow]{d} {'NAR' if is_nar else 'JRA'} race_list failed: {ex}[/yellow]"
                        )
        finally:
            browser.close()
    return out


# --- CLI ---


@app.command()
def main(
    since: str = typer.Option(..., "--since", help="YYYYMMDD (含む)"),
    until: str = typer.Option(..., "--until", help="YYYYMMDD (含む)"),
    jra: bool = typer.Option(True, "--jra/--no-jra"),
    nar: bool = typer.Option(True, "--nar/--no-nar"),
    workers: int = typer.Option(5, "--workers", help="並列 Playwright browser 数"),
    settle_ms: int = typer.Option(2000, "--settle-ms"),
    timeout_ms: int = typer.Option(45_000, "--timeout-ms"),
    polite_ms: int = typer.Option(500, "--polite-ms", help="fetch 間の sleep (ms)"),
    enum_only: bool = typer.Option(False, "--enum-only", help="race_id 列挙だけして終了"),
    rids_file: Path | None = typer.Option(None, "--rids-file", help="既存 race_id リストファイルを読む (列挙スキップ)"),
):
    """bulk fetch 本体。"""
    console.rule(f"[bold]bulk_fetch[/bold] since={since} until={until} workers={workers}")

    # 1) race_id 列挙
    if rids_file and rids_file.exists():
        rids = [line.strip() for line in rids_file.read_text().splitlines() if line.strip()]
        console.print(f"[dim]loaded {len(rids)} race_ids from {rids_file}[/dim]")
    else:
        console.print("Step 1: enumerating race_ids ...")
        rids = collect_race_ids(since, until, jra=jra, nar=nar)
        console.print(f"[green]→ {len(rids)} race_ids collected[/green]")
        # 保存
        rids_out = ROOT / "data" / "cache" / f"rids_{since}_{until}.txt"
        rids_out.parent.mkdir(parents=True, exist_ok=True)
        rids_out.write_text("\n".join(rids) + "\n")
        console.print(f"[dim]saved race_id list: {rids_out}[/dim]")

    if enum_only:
        return

    if not rids:
        console.print("[yellow]対象 race なし[/yellow]")
        return

    # 2) fetch target 展開
    targets: list[FetchTarget] = []
    for rid in rids:
        targets.extend(_make_targets(rid))
    total = len(targets)
    already = sum(1 for t in targets if t.already_done)
    console.print(
        f"Step 2: {total} fetch targets ({already} already cached, "
        f"{total - already} to do)"
    )

    if total == already:
        console.print("[green]全部キャッシュ済[/green]")
        return

    # 3) queue & workers
    queue: Queue = Queue()
    for t in targets:
        queue.put(t)
    for _ in range(workers):
        queue.put(None)  # sentinel

    stats = WorkerStats()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("fetching", total=total)
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = [
                exe.submit(_worker_loop, queue, stats, progress, task_id, settle_ms, timeout_ms, polite_ms)
                for _ in range(workers)
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as ex:  # noqa: BLE001
                    console.print(f"[red]worker died: {ex}[/red]")

    console.rule("[bold]done[/bold]")
    console.print(
        f"fetched={stats.fetched} skipped(cached)={stats.skipped} errors={stats.errors}"
    )
    if stats.errors > 0:
        console.print(f"[yellow]error log: {ERROR_LOG}[/yellow]")


if __name__ == "__main__":
    app()
