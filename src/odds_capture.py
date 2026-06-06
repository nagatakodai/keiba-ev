"""締切前 N 分のオッズ時系列キャプチャ daemon (Step 2, 2026-06-06)。

`data/cache/odds_timeline/<race_id>.jsonl` に stage="poll" でオッズを積む。
Step 1 (score/bet hook, 追加 fetch ゼロ) の 2 点に、締切前 ~30 分の多点時系列を足して
ドリフト解析 (TORIGAMI_MARGIN 較正 / late-money momentum) の分解能を上げる。

- **netkeiba は polling しない** (IP 規制で CloudFront 400 block になるため)。
  NAR は keiba.go.jp (静的 UTF-8 GET, 全6券種組合せ明示)、
  JRA は JRA 公式 accessO (Shift_JIS POST chain, 全7券種組合せ明示)。
- discovery は auto_watch と同じ公式ソース (`discover_today_races`:
  oddspark NAR list + 競馬ブック×JRA公式 join)。netkeiba 不要。
- 同一レースの最小キャプチャ間隔 (`--capture-interval`, 既定 180s) と odds_hash dedup
  (odds_timeline 側) で礼儀正しく polling する。失敗レースは cooldown を置いて retry。
- watch-auto とは独立の別プロセス。capture が重くても投票 dispatch の latency に影響しない。

使い方:
  python -m src.odds_capture                 # daemon (Ctrl+C で終了)
  python -m src.odds_capture --once          # 1 周だけ (cron 用)
  make odds-capture
"""
from __future__ import annotations

import time
from datetime import datetime

import typer
from rich.console import Console

from . import odds_timeline as odds_tl
from .auto_watch import _normalize_race_id, discover_today_races
from .parse import close_at_for_start

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)

# 失敗レースの retry cooldown (秒)。find 失敗 (未開催/未公開) を毎周叩かない。
FAIL_COOLDOWN_SEC = 300


def _capture_one(race: dict, *, keibago_loc_cache: dict) -> bool:
    """1 レース分のオッズを公式ソースから取得して timeline に append。"""
    nk_rid = race["race_id"]                  # netkeiba 12桁
    rid = _normalize_race_id(nk_rid)          # predictions/results と同じ join key
    start_at = race.get("start_at") or 0
    close_at = close_at_for_start(start_at) if start_at else 0

    if race.get("source") == "keibabook":
        # JRA: token は checksum 必須・使い捨ての可能性があるため毎回 find から walk。
        from .scrape_jra import fetch_jra_bets, find_jra_race
        loc = find_jra_race(nk_rid)
        if not loc:
            return False
        bets = fetch_jra_bets(loc)
        source = "jra"
    else:
        # NAR: loc は (日付, R, babaCode) で安定なのでメモリ cache (find は当日 1 回)。
        from .scrape_keibago import fetch_keibago_bets, find_keibago_race
        loc = keibago_loc_cache.get(nk_rid)
        if loc is None:
            loc = find_keibago_race(nk_rid)
            if not loc:
                return False
            keibago_loc_cache[nk_rid] = loc
        bets = fetch_keibago_bets(loc)
        source = "keibago"

    payload = odds_tl.build_payload(bets.get("other_bets"), bets.get("trifecta"))
    n_horses = len((bets.get("other_bets") or {}).get("win") or [])
    return odds_tl.append_line(
        rid, payload, "poll",
        close_at=close_at, start_at=start_at, n_horses=n_horses, source=source,
    )


@app.command()
def main(
    window: float = typer.Option(30.0, "--window", help="締切前 N 分以内のレースを polling 対象にする"),
    poll: float = typer.Option(60.0, "--poll", help="メインループの周回間隔 (秒)"),
    capture_interval: float = typer.Option(
        180.0, "--capture-interval",
        help="同一レースの最小キャプチャ間隔 (秒)。score/bet hook の行も間隔に数える"),
    discovery_interval: float = typer.Option(600.0, "--discovery-interval", help="開催一覧の再取得間隔 (秒)"),
    once: bool = typer.Option(False, "--once", help="1 周だけ実行して終了 (cron 用)"),
):
    """締切前 window 分のレースのオッズを公式ソースから定期キャプチャする。"""
    races: list[dict] = []
    last_discovery = 0.0
    fail_until: dict[str, float] = {}      # nk_rid → retry 解禁 unix
    keibago_loc_cache: dict = {}
    captured_total = 0

    console.print(
        f"[bold]odds-capture[/bold]: 締切前 {window:.0f} 分 / {capture_interval:.0f}s おき"
        f" / discovery {discovery_interval:.0f}s おき (netkeiba は使わない)"
    )
    while True:
        now = time.time()
        if not races or now - last_discovery >= discovery_interval:
            today = datetime.fromtimestamp(now).strftime("%Y%m%d")
            races = discover_today_races(today)
            last_discovery = now
            keibago_loc_cache.clear()      # 日跨ぎ/再 discovery で古い loc を捨てる

        due = []
        for r in races:
            start_at = r.get("start_at") or 0
            if start_at <= 0:
                continue
            delta = close_at_for_start(start_at) - now
            if 0 <= delta <= window * 60:
                due.append((delta, r))
        due.sort(key=lambda x: x[0])       # 締切が近い順に処理

        for delta, r in due:
            nk_rid = r["race_id"]
            if now < fail_until.get(nk_rid, 0):
                continue
            rid = _normalize_race_id(nk_rid)
            last = odds_tl.last_captured_at(rid)
            if last is not None and now - last < capture_interval:
                continue
            try:
                ok = _capture_one(r, keibago_loc_cache=keibago_loc_cache)
            except Exception as ex:  # noqa: BLE001
                console.print(f"[yellow]capture 失敗 {r['venue']}{r['race_no']}R ({nk_rid}): {ex}[/yellow]")
                ok = False
            if ok:
                captured_total += 1
                console.print(
                    f"[cyan]capture[/cyan] {r['venue']}{r['race_no']}R 締切まで {delta/60:.1f} 分 "
                    f"→ {rid}.jsonl (累計 {captured_total})"
                )
                fail_until.pop(nk_rid, None)
            else:
                # find 不可/オッズ未公開/同一オッズ skip。同一オッズ skip も cooldown して良い
                # (オッズが動いていない = すぐ再取得しても無駄)。
                fail_until[nk_rid] = now + FAIL_COOLDOWN_SEC

        if once:
            break
        time.sleep(poll)


if __name__ == "__main__":
    app()
