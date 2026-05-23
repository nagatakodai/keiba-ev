"""netkeiba の開催一覧を polling し、締切 (= 発走) 5±N 分以内のレースを自動解析する。

使い方:
    python -m src.auto_watch                # 1 巡
    python -m src.auto_watch --window 5 --tolerance 2

通常は Makefile の `watch-auto` ターゲットから無限ループで叩く。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from .fetch_result import process_pending, schedule as schedule_result_fetch
from .parse import _split_race_id, parse_race_list
from .scrape import NetkeibaBlocked, fetch_html, race_list_url
from .scrape_alt import fetch_race_list_keibalab

ROOT = Path(__file__).resolve().parents[1]
CACHE_FILE = ROOT / "data/cache/auto_watch_analyzed.txt"
HISTORY_FILE = ROOT / "data/cache/auto_watch_history.jsonl"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


def _load_analyzed() -> set[str]:
    if not CACHE_FILE.exists():
        return set()
    return {line.strip() for line in CACHE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()}


def _mark_analyzed(race_id: str) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("a", encoding="utf-8") as f:
        f.write(race_id + "\n")


def _append_history(record: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _list_due_races(window_min: int, tolerance_min: int, now_ts: int) -> list[dict]:
    """その日 (JST) の開催一覧を netkeiba (JRA + NAR) から取得し、締切 N±M 分のレースを抽出。

    netkeiba 両ドメインが block されたら keibalab.jp に fallback する
    (race_id + 発走時刻だけ取れる; live odds は不可なので analyze 自体は失敗するが、
    どの race が走るかは把握できる)。
    """
    today = datetime.fromtimestamp(now_ts).strftime("%Y%m%d")
    races: list[dict] = []
    blocked_count = 0
    for is_nar in (False, True):
        try:
            html = fetch_html(race_list_url(today, nar=is_nar), timeout_ms=30_000)
            races.extend(parse_race_list(html, today))
        except NetkeibaBlocked as ex:
            blocked_count += 1
            console.print(f"[red]netkeiba blocked (nar={is_nar}): {ex}[/red]")
        except Exception as ex:
            console.print(f"[yellow]race_list fetch failed (nar={is_nar}): {ex}[/yellow]")
    if blocked_count >= 2:
        # 両ドメイン block → keibalab.jp に fallback。race_id + 発走時刻が取れれば
        # watch-auto は当該 race の analyze を試みる (analyze 内部で netkeiba odds
        # fetch が失敗すれば skip される)。
        console.print(
            "[bold yellow]netkeiba 両ドメイン block → keibalab.jp に fallback します。"
            "発走時刻まで分かれば watch は続行できますが live odds 取得は不可なので "
            "analyze 段階で失敗する可能性があります。[/bold yellow]"
        )
        try:
            alt = fetch_race_list_keibalab(today)
            console.print(f"[cyan]keibalab fallback: {len(alt)} races detected[/cyan]")
            for a in alt:
                races.append({
                    "race_id": a.race_id,
                    "url": a.url,
                    "start_at": a.start_at,
                    "venue": a.venue or "?",  # keibalab simple parser では未取得
                    "race_no": a.race_no,
                })
        except Exception as ex:
            console.print(f"[red]keibalab fallback も失敗: {ex}[/red]")
            console.print(
                "[bold red]race discovery 不能。数時間-1日待つか、別 IP/VPN から再試行してください。[/bold red]"
            )

    low_sec = (window_min - tolerance_min) * 60
    high_sec = (window_min + tolerance_min) * 60

    out: list[dict] = []
    for r in races:
        start_at = r.get("start_at") or 0
        if start_at <= 0:
            continue
        delta = start_at - now_ts
        if not (low_sec <= delta <= high_sec):
            continue
        rid = r["race_id"]
        out.append({
            "race_id": _normalize_race_id(rid),
            "netkeiba_race_id": rid,
            "url": r["url"],
            "start_at": start_at,
            "close_at": start_at,
            "delta_sec": delta,
            "venue": r["venue"],
            "race_no": r["race_no"],
        })
    out.sort(key=lambda x: x["start_at"])
    return out


def _normalize_race_id(netkeiba_rid: str) -> str:
    """netkeiba race_id を `cup_id-schedule_index-race_number` 文字列に正規化。

    キャリブレ join 用 (parse_shutuba 後の analyze.py が生成する形式と一致させる)。
    JRA/NAR で race_id 形式が違うため `_split_race_id` に委譲する。
    """
    if not netkeiba_rid or len(netkeiba_rid) != 12:
        return netkeiba_rid
    _, schedule_index, race_number, cup_id = _split_race_id(netkeiba_rid)
    return f"{cup_id}-{schedule_index}-{race_number}"


def _dispatch_analyze(url: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, "-m", "src.analyze", url, "--llm-model", "opus", *extra_args]
    console.print(f"[bold cyan]→ analyze:[/bold cyan] {url}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _drain_pending(*, label: str = "") -> None:
    try:
        s = process_pending(now_ts=int(time.time()))
    except Exception as e:
        console.print(f"[red]process_pending 失敗: {e}[/red]")
        return
    if not (s["checked"] or s["success"] or s["failed"]):
        return
    tag = f"[{label}] " if label else ""
    console.print(
        f"[dim]{tag}result fetch:[/dim] checked={s['checked']} "
        f"success={len(s['success'])} failed={len(s['failed'])} "
        f"still_pending={s['still_pending']} not_due={s['not_due']}"
    )
    for r in s["success"]:
        console.print(f"  [green]✓ result saved:[/green] {r}")
    for r in s["failed"]:
        console.print(f"  [red]✗ result giveup:[/red] {r}")


def _in_active_hours(now: datetime, active_hours: str) -> bool:
    try:
        start_s, end_s = active_hours.split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except (ValueError, AttributeError):
        return True
    now_min = now.hour * 60 + now.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    return start_min <= now_min <= end_min


@app.command()
def main(
    window_min: int = typer.Option(5, "--window", help="発走までの目標時間 (分)"),
    tolerance_min: int = typer.Option(2, "--tolerance", help="目標時間の前後許容 (分)"),
    ev_max: float = typer.Option(None, "--ev-max"),
    min_prob: float = typer.Option(None, "--min-prob"),
    market_blend: float = typer.Option(None, "--market-blend"),
    aptitude_top: int = typer.Option(None, "--aptitude-top"),
    with_exacta: bool = typer.Option(False, "--with-exacta"),
    with_trio: bool = typer.Option(False, "--with-trio"),
    active_hours: str = typer.Option(
        "09:00-23:45", "--active-hours",
        help="race detection を行う JST 時間帯。JRA 土日 ~9:50-17:00、NAR ナイター ~21:00、ばんえい 等の遅レースを含めて広めに。",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """1 巡だけ実行。"""
    now_dt = datetime.now()
    now_ts = int(now_dt.timestamp())
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    _drain_pending(label="pre-analyze")

    if not _in_active_hours(now_dt, active_hours):
        console.print(
            f"[dim]{now_str}[/dim] off-hours ({active_hours} 外): "
            "race detection skip、result fetch のみ"
        )
        return

    console.print(
        f"[dim]{now_str}[/dim] 発走 {window_min}±{tolerance_min} 分のレースを検索中..."
    )
    try:
        due = _list_due_races(window_min, tolerance_min, now_ts)
    except Exception as e:
        console.print(f"[red]race_list 取得失敗: {e}[/red]")
        return

    if not due:
        console.print("[dim]該当レースなし[/dim]")
        return

    analyzed = _load_analyzed()
    extra: list[str] = []
    if ev_max is not None:
        extra += ["--ev-max", str(ev_max)]
    if min_prob is not None:
        extra += ["--min-prob", str(min_prob)]
    if market_blend is not None:
        extra += ["--market-blend", str(market_blend)]
    if aptitude_top is not None:
        extra += ["--aptitude-top", str(aptitude_top)]
    if with_exacta:
        extra.append("--with-exacta")
    if with_trio:
        extra.append("--with-trio")

    for race in due:
        rid = race["race_id"]
        mins = race["delta_sec"] / 60.0
        tag = f"{race['venue']} {race['race_no']}R 発走まで {mins:.1f}分"
        if rid in analyzed:
            console.print(f"[dim]skip (already analyzed): {tag} {rid}[/dim]")
            continue
        console.print(f"[bold green]match:[/bold green] {tag} ({rid})")
        if dry_run:
            console.print(f"  [dim]dry-run: {race['url']}[/dim]")
            continue
        started_at = int(time.time())
        rc = _dispatch_analyze(race["url"], extra)
        finished_at = int(time.time())
        if rc == 0:
            _mark_analyzed(rid)
            try:
                p = schedule_result_fetch(rid, race["url"], race["start_at"])
                if p.status == "pending":
                    console.print(
                        f"  [cyan]→ result fetch scheduled:[/cyan] "
                        f"{rid} at {datetime.fromtimestamp(p.due_at).strftime('%H:%M:%S')}"
                    )
            except Exception as e:
                console.print(f"[red]schedule_result_fetch 失敗: {e}[/red]")
            _drain_pending(label="post-analyze")
        else:
            console.print(f"[red]analyze 失敗 rc={rc} race={rid}[/red]")
        _append_history({
            "started_at": started_at,
            "finished_at": finished_at,
            "race_id": rid,
            "netkeiba_race_id": race.get("netkeiba_race_id", rid),
            "url": race["url"],
            "venue": race["venue"],
            "race_no": race["race_no"],
            "start_at": race["start_at"],
            "close_at": race["close_at"],
            "rc": rc,
        })


if __name__ == "__main__":
    app()
