"""netkeiba の結果ページからレース結果を自動取得して data/results/<race_id>.json に保存。

タイミング:
  - 中央競馬の結果反映は通常 発走 + 5〜10 分。
  - 初回 fetch は **発走 +8 分**。失敗時は 2 分間隔で 8 回 (発走 +8 〜 +22 分)。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from .parse import parse_result
from .scrape import extract_race_id, fetch_html, result_url

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "data" / "results"
PENDING_FILE = ROOT / "data" / "cache" / "pending_results.json"

DEFAULT_DELAY_SEC = 8 * 60
DEFAULT_RETRY_INTERVAL_SEC = 120
DEFAULT_MAX_ATTEMPTS = 8
MAX_PROCESS_PER_TICK = 3
TERMINAL_RETENTION_SEC = 24 * 3600
# netkeiba block 中の result fetch は attempt を消費せず、この間隔で再試行し続ける
# (block 解除後に取得 → block 中に走った race の結果/calibration を取りこぼさない)。
BLOCK_RETRY_INTERVAL_SEC = 15 * 60
# block-pending は attempt を消費しないので、長期/恒久 block では pending が単調増大し
# due_idx を食い潰す。scheduled_at からこの age を超えた pending は諦めて failed 化する
# (開催終了で結果ページが永遠に block 風レスポンスを返すケース等の上限)。
MAX_PENDING_AGE_SEC = 3 * 24 * 3600


def _is_block_failure(reason: str) -> bool:
    """fetch 失敗理由が netkeiba の IP block (空 body / CloudFront 400) かを判定。"""
    return bool(reason) and (
        "NetkeibaBlocked" in reason or "empty body" in reason or "CloudFront" in reason
    )

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)


@dataclass
class Pending:
    race_id: str                 # YYYYMMDD-MMDD-R 正規化 ID
    url: str                     # shutuba.html URL を保存 (fetch 時に result.html へ変換)
    due_at: int
    next_attempt_at: int
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_interval_sec: int = DEFAULT_RETRY_INTERVAL_SEC
    status: str = "pending"
    last_error: str = ""
    scheduled_at: int = field(default_factory=lambda: int(time.time()))


def result_url_from_racecard(url: str) -> str:
    """shutuba / odds URL を結果ページ URL に変換。"""
    rid = extract_race_id(url)
    if not rid:
        return url
    return result_url(rid)


def fetch_result(url: str, *, timeout_ms: int = 30_000) -> dict | None:
    result, _ = fetch_result_with_reason(url, timeout_ms=timeout_ms)
    return result


def fetch_result_with_reason(url: str, *, timeout_ms: int = 30_000) -> tuple[dict | None, str]:
    try:
        html = fetch_html(url, timeout_ms=timeout_ms)
    except Exception as ex:
        msg = f"fetch_html: {type(ex).__name__}: {str(ex)[:200]}"
        _log(msg, url)
        return None, msg
    result = parse_result(html)
    if result is not None:
        return result, ""
    _log("no finish_order parsed", url)
    return None, "no finish_order in result page (race not yet settled?)"


def _log(msg: str, url: str) -> None:
    import sys
    print(f"[fetch_result] {msg} url={url}", file=sys.stderr, flush=True)


def save_result(race_id: str, payload: dict, *, note: str = "") -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{race_id}.json"
    data = {
        "race_id": race_id,
        "finish_order": payload["finish_order"],
        "trifecta_payout": payload.get("payout", 0),
        "note": note,
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": payload.get("source", "auto"),
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --- 永続 pending キュー ---
#
# 複数 process (auto_watch loop / api server / 手動 CLI) から同じ file を
# read/mutate/save するため、file lock + atomic write で:
#   - lost update (auto_watch が _load → 別 process が DELETE/save → auto_watch が
#     旧 entries で _save → DELETE 消失) を防ぐ
#   - partial write (中断 / disk full で 0 byte / 半端 JSON が残り、_load_pending が
#     JSONDecodeError 経由で全 pending 消失する) を防ぐ
#
# 使い方: read/mutate/save の sequence は `with _pending_lock():` で wrap する。
# 単発 _load_pending (read-only) は lock 無しでも safety net (broken JSON → []) で
# 動くが、UI 表示時の一時的な不整合を防ぐため lock を取るのが望ましい。


def _load_pending() -> list[Pending]:
    if not PENDING_FILE.exists():
        return []
    try:
        raw = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[Pending] = []
    for r in raw.get("races", []):
        try:
            out.append(Pending(**{k: v for k, v in r.items() if k in Pending.__dataclass_fields__}))
        except TypeError:
            continue
    return out


def _save_pending(entries: list[Pending]) -> None:
    """atomic rename で書き込む。同一 dir の tmp に write → os.replace で
    POSIX 上 atomic な rename になる (truncate→write 中の中断で破損しない)。
    """
    import os
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"races": [asdict(e) for e in entries]}, ensure_ascii=False, indent=2)
    tmp = PENDING_FILE.with_suffix(PENDING_FILE.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, PENDING_FILE)


_PENDING_LOCK_FILE = PENDING_FILE.with_suffix(PENDING_FILE.suffix + ".lock")


def _pending_lock():
    """process-safe lock context manager (fcntl.flock)。
    auto_watch loop / api server / 手動 CLI が同じ pending file を
    read/mutate/save する race を防ぐ。Windows 非対応 (本 repo は Linux 限定)。
    """
    import fcntl
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        f = open(_PENDING_LOCK_FILE, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()

    return _cm()


def schedule(
    race_id: str,
    racecard_url: str,
    race_start_at: int,
    *,
    delay_sec: int = DEFAULT_DELAY_SEC,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_interval_sec: int = DEFAULT_RETRY_INTERVAL_SEC,
) -> Pending:
    if (RESULTS_DIR / f"{race_id}.json").exists():
        return Pending(
            race_id=race_id, url="", due_at=0, next_attempt_at=0, status="success",
        )
    # read/mutate/save を file lock で序列化 (auto_watch / api server 同時走行で
    # lost update が起こらないようにする)。
    with _pending_lock():
        entries = _load_pending()
        for e in entries:
            if e.race_id == race_id:
                # 既存 entry が "failed" (max_attempts 到達) なら再 schedule を
                # 「ユーザーによる手動 retry リクエスト」と解釈して pending に戻す。
                # auto_watch は同じ race を re-analyze しないので自動 trigger されないが、
                # 手動 `python -m src.fetch_result schedule` で IP block 解除後の救済が可能になる。
                if e.status == "failed":
                    e.status = "pending"
                    e.attempts = 0
                    e.last_error = ""
                    e.due_at = int(race_start_at) + delay_sec
                    e.next_attempt_at = int(time.time())
                    e.scheduled_at = int(time.time())
                    _save_pending(entries)
                return e
        due_at = int(race_start_at) + delay_sec
        new = Pending(
            race_id=race_id,
            url=racecard_url,
            due_at=due_at,
            next_attempt_at=due_at,
            max_attempts=max_attempts,
            retry_interval_sec=retry_interval_sec,
        )
        entries.append(new)
        _save_pending(entries)
        return new


def process_pending(
    now_ts: int | None = None,
    max_per_tick: int = MAX_PROCESS_PER_TICK,
) -> dict:
    if now_ts is None:
        now_ts = int(time.time())
    summary: dict[str, Any] = {
        "checked": 0, "success": [], "failed": [],
        "still_pending": 0, "not_due": 0, "pruned": 0,
    }

    # Phase 1: lock 取得して prune + due_idx 決定。fetch 自体は lock 外で行う
    # (HTTP 数秒で lock 抱え込むと UI/API が hang する)。
    with _pending_lock():
        entries = _load_pending()
        # 長期 block 等で滞留した pending を failed 化 (単調増大・due_idx 食い潰し防止)。
        stale_cutoff = now_ts - MAX_PENDING_AGE_SEC
        for e in entries:
            if e.status == "pending" and e.scheduled_at < stale_cutoff:
                e.status = "failed"
                e.last_error = e.last_error or f"gave up after {MAX_PENDING_AGE_SEC // 3600}h pending"
        cutoff = now_ts - TERMINAL_RETENTION_SEC
        before_prune = len(entries)
        entries = [
            e for e in entries
            if not (e.status in ("success", "failed") and e.scheduled_at < cutoff)
        ]
        summary["pruned"] = before_prune - len(entries)
        due_idx = sorted(
            (i for i, e in enumerate(entries) if e.status == "pending" and now_ts >= e.next_attempt_at),
            key=lambda i: entries[i].next_attempt_at,
        )
        summary["not_due"] = sum(
            1 for e in entries if e.status == "pending" and now_ts < e.next_attempt_at
        )
        if not due_idx:
            if summary["pruned"] > 0:
                _save_pending(entries)
            return summary
        # 処理対象 race_id をスナップショット。
        target_ids = [entries[i].race_id for i in due_idx[:max_per_tick]]
        target_urls = {entries[i].race_id: entries[i].url for i in due_idx[:max_per_tick]}
        # prune だけ先に反映 (失敗してもいい変更なので)
        if summary["pruned"] > 0:
            _save_pending(entries)

    # Phase 2: lock 外で fetch を実行 (各 race 数秒)。
    fetched: dict[str, tuple[dict | None, str]] = {}
    for rid in target_ids:
        fetch_url = result_url_from_racecard(target_urls[rid])
        fetched[rid] = fetch_result_with_reason(fetch_url)

    # Phase 3: lock 取り直して結果反映 (read again — 別 process の変更を取り込む)。
    with _pending_lock():
        entries = _load_pending()
        by_id = {e.race_id: e for e in entries}
        changed = False
        for rid in target_ids:
            e = by_id.get(rid)
            if e is None or e.status != "pending":
                # 別 process が DELETE / 状態変更した — skip
                continue
            summary["checked"] += 1
            result, reason = fetched[rid]
            e.last_error = reason
            if result and result.get("finish_order"):
                try:
                    save_result(e.race_id, result)
                    e.attempts += 1
                    e.status = "success"
                    e.last_error = ""
                    summary["success"].append(e.race_id)
                except Exception as ex:
                    # 結果は確定済 = 再 fetch すれば取れる。save の一時エラー (disk full /
                    # I/O 等) で terminal failed にすると取りこぼすので、attempt を消費せず
                    # backoff して pending を維持する (block 失敗と同じ扱い)。
                    e.last_error = f"save error: {ex}"
                    e.next_attempt_at = now_ts + max(e.retry_interval_sec, BLOCK_RETRY_INTERVAL_SEC)
                    summary["still_pending"] += 1
            elif _is_block_failure(reason):
                # netkeiba block 中は attempt を消費せず長め間隔で再試行 → 解除後に
                # 取得できる (block 中の race を terminal failed にして取りこぼさない)。
                e.next_attempt_at = now_ts + max(e.retry_interval_sec, BLOCK_RETRY_INTERVAL_SEC)
                summary["still_pending"] += 1
            else:
                # 通常の取得失敗 (結果未確定 / パース失敗等) — attempt 消費して max で failed。
                e.attempts += 1
                if e.attempts >= e.max_attempts:
                    e.status = "failed"
                    summary["failed"].append(e.race_id)
                else:
                    e.next_attempt_at = now_ts + e.retry_interval_sec
                    summary["still_pending"] += 1
            changed = True

        summary["still_pending"] += max(0, len(due_idx) - max_per_tick)
        if changed:
            _save_pending(entries)
    return summary


# --- CLI ---

@app.command("schedule")
def cli_schedule(
    race_id: str = typer.Argument(...),
    url: str = typer.Argument(...),
    start_at: int = typer.Argument(...),
    delay: int = typer.Option(DEFAULT_DELAY_SEC, "--delay"),
    max_attempts: int = typer.Option(DEFAULT_MAX_ATTEMPTS, "--max-attempts"),
    retry_interval: int = typer.Option(DEFAULT_RETRY_INTERVAL_SEC, "--retry-interval"),
):
    p = schedule(
        race_id, url, start_at,
        delay_sec=delay, max_attempts=max_attempts, retry_interval_sec=retry_interval,
    )
    console.print(
        f"[green]scheduled:[/green] {p.race_id} "
        f"due={dt.datetime.fromtimestamp(p.due_at)} "
        f"max_attempts={p.max_attempts} retry={p.retry_interval_sec}s"
    )


@app.command("process")
def cli_process(max_per_tick: int = typer.Option(MAX_PROCESS_PER_TICK, "--max-per-tick")):
    s = process_pending(max_per_tick=max_per_tick)
    console.print(
        f"checked={s['checked']} success={len(s['success'])} "
        f"failed={len(s['failed'])} pending={s['still_pending']} not_due={s['not_due']}"
    )
    for r in s["success"]:
        console.print(f"  [green]✓[/green] {r}")
    for r in s["failed"]:
        console.print(f"  [red]✗[/red] {r}")


@app.command("fetch")
def cli_fetch(
    race_id: str = typer.Argument(...),
    url: str = typer.Argument(...),
):
    url = result_url_from_racecard(url)
    result = fetch_result(url)
    if not result:
        console.print(f"[yellow]結果未取得: {url}[/yellow]")
        raise typer.Exit(1)
    out = save_result(race_id, result)
    console.print(f"[green]saved:[/green] {out} finish_order={result['finish_order']}")


@app.command("list")
def cli_list():
    entries = _load_pending()
    if not entries:
        console.print("[dim]pending なし[/dim]")
        return
    now = int(time.time())
    for e in entries:
        due = dt.datetime.fromtimestamp(e.due_at).strftime("%Y-%m-%d %H:%M:%S")
        nxt = dt.datetime.fromtimestamp(e.next_attempt_at).strftime("%H:%M:%S")
        delta = e.next_attempt_at - now
        console.print(
            f"  {e.status:8s} {e.race_id:24s} attempts={e.attempts}/{e.max_attempts} "
            f"due={due} next={nxt} ({delta:+d}s) {e.last_error}"
        )


if __name__ == "__main__":
    app()
