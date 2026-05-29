"""netkeiba の開催一覧を polling し、**締切** window〜window+tolerance 分前のレースを自動解析する。

締切は発走の CLOSE_LEAD_SEC (=120) 秒前で固定 (`parse.close_at_for_start`)。検出帯を
締切基準にすることで、賭けの締切までのリード時間が常に安定する (発走基準より +2 分 lead)。
片側 (+のみ): 締切まで window 分以上のリードを必ず確保し、締切間際の解析を防ぐ。

使い方:
    python -m src.auto_watch                # 1 巡
    python -m src.auto_watch --window 5 --tolerance 2   # 締切 5〜7 分前で検出
                                                         # (= 発走 7〜9 分前 相当)

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
from .parse import _split_race_id
from .scrape_alt import fetch_race_list_keibabook
from .scrape_oddspark import fetch_race_list_oddspark

ROOT = Path(__file__).resolve().parents[1]
CACHE_FILE = ROOT / "data/cache/auto_watch_analyzed.txt"
HISTORY_FILE = ROOT / "data/cache/auto_watch_history.jsonl"
BET_QUEUE_DIR = ROOT / "data/cache/oddspark_bet_queue"   # = oddspark_bet.QUEUE_DIR

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


# oddspark 投票を打たない場 (analyze/snapshot は通常通り保存、enqueue だけ skip)。
# 場名は src/parse.py:VENUE_CODE の値と一致させる。
BET_SKIP_VENUES: set[str] = {"浦和"}


def _enqueue_oddspark_bet(race_id: str, netkeiba_rid: str) -> bool:
    """snapshot に束(legs)があれば oddspark 常駐 betting セッションの queue に投入する。

    `--bet-oddspark` 時のみ呼ぶ。`oddspark_bet --session` daemon が <rid>.req を拾って
    カート投入する (購入確定は人)。NAR (投票 joCode がある場) のみ・束が非空のみ・
    未投入のみ enqueue。**賭金は動かない** (カート投入手前まで)。
    BET_SKIP_VENUES の場 (現状: 浦和) は enqueue しない (snapshot は残る)。
    """
    # netkeiba rid は 12桁数字前提 (これでないと daemon 側 race_val 生成が壊れる)
    if not (netkeiba_rid.isdigit() and len(netkeiba_rid) == 12):
        return False
    # JRA / 未対応場は oddspark で投票できない → enqueue しない
    from .oddspark_bet import _vote_jo_code
    if _vote_jo_code(netkeiba_rid) is None:
        return False
    # ユーザ指定スキップ場 (浦和 等) → 投票しない
    from .parse import VENUE_CODE
    venue = VENUE_CODE.get(netkeiba_rid[4:6], "")
    if venue in BET_SKIP_VENUES:
        console.print(f"[yellow]oddspark enqueue skip: {venue} は BET_SKIP_VENUES 指定 ({netkeiba_rid})[/yellow]")
        return False
    snap = ROOT / "data/predictions" / f"{race_id}.json"
    if not snap.exists():
        return False
    try:
        d = json.loads(snap.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    legs = [l for l in ((d.get("recommended_bundle") or {}).get("legs") or [])
            if int(l.get("stake", 0)) > 0]
    if not legs:
        return False   # 見送り (束が空) は投入しない
    BET_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    req = BET_QUEUE_DIR / f"{netkeiba_rid}.req"
    if req.exists() or (BET_QUEUE_DIR / f"{netkeiba_rid}.done").exists():
        return False   # 既に投入/処理済
    tmp = req.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "netkeiba_rid": netkeiba_rid, "race_id": race_id, "legs": len(legs),
        "total_stake": sum(int(l.get("stake", 0)) for l in legs),
        "enqueued_at": int(time.time()),
    }, ensure_ascii=False), encoding="utf-8")
    tmp.rename(req)   # atomic
    return True


# 失敗 race の再試行 cooldown (秒)。
# auto_watch は 60 秒毎にループするので、毎 tick 同じ race を再分析しないよう
# 一定時間 skip する。netkeiba 規制 / network エラー / fetch 失敗で永遠に
# 再試行 → CPU 浪費を防ぐ。block 解除 / 一時的エラーなら 5 分後にリトライ可能。
FAILED_RETRY_COOLDOWN_SEC = 300


def _recently_failed(race_id: str, now_ts: int, cooldown_sec: int = FAILED_RETRY_COOLDOWN_SEC) -> bool:
    """history を遡って race_id が直近 cooldown_sec 秒以内に rc != 0 で
    失敗していたかを返す。True なら skip 推奨。"""
    if not HISTORY_FILE.exists():
        return False
    cutoff = now_ts - cooldown_sec
    try:
        # 末尾から読む方が効率的だが、簡易に全行読み (typically <1000 lines)
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("race_id") != race_id:
                continue
            if rec.get("rc", 0) == 0:
                continue  # success は cooldown 対象外 (analyzed cache が別途扱う)
            finished_at = rec.get("finished_at") or 0
            if finished_at >= cutoff:
                return True
    except OSError:
        return False
    return False


def _list_due_races(window_min: int, tolerance_min: int, now_ts: int) -> list[dict]:
    """その日 (JST) の開催一覧を **公式ソース** から取得し、締切 N±M 分のレースを抽出。

    - NAR: **oddspark** の当日 race list (netkeiba_rid + 発走時刻、netkeiba 不要)。
      analyze は `_dispatch_nar_fallback` で keiba.go.jp 公式 (全6券種) → oddspark 順。
    - JRA: 発走時刻ソースが現状無いため live discovery skip (CLAUDE.md 既知の宿題)。
      JRA を打つ場合は手動で `python -m src.scrape_jra <rid> --snapshot` または
      `make run URL=...` を使う。

    netkeiba live (race_list / shutuba / odds) は IP 規制を避けるため **常に使わない**。
    過去レースの解析や学習・holdout は data/raw/ の netkeiba キャッシュをそのまま使う。
    """
    today = datetime.fromtimestamp(now_ts).strftime("%Y%m%d")
    races: list[dict] = []
    # NAR 公式 (oddspark の当日 race list) で discovery。netkeiba race_list は呼ばない。
    try:
        ops = fetch_race_list_oddspark(today)
        console.print(f"[cyan]oddspark NAR discovery: {len(ops)} races[/cyan]")
        for a in ops:
            races.append({
                "race_id": a["netkeiba_race_id"],
                "url": a["url"],
                "start_at": a["start_at"],
                "venue": a["venue"],
                "race_no": a["race_no"],
                "source": "oddspark",
            })
    except Exception as ex:  # noqa: BLE001
        console.print(f"[red]oddspark NAR discovery 失敗: {ex}[/red]")
    # JRA discovery: 競馬ブック (発走時刻ソース) × JRA 公式 discover (netkeiba_rid ソース) を
    # (venue_name, race_no) で join。発走時刻と netkeiba_rid の両方が揃った race のみ採用。
    try:
        from .parse import VENUE_CODE
        from .scrape_jra import discover_jra_races
        kb = fetch_race_list_keibabook(today)
        if kb:
            jra = discover_jra_races()
            # (venue_name, race_no) → netkeiba_rid
            jra_by_key = {(VENUE_CODE.get(r["venue"], ""), r["race_no"]): r
                          for r in jra if r["date"] == today}
            joined = 0
            for k in kb:
                key = (k["venue"], k["race_no"])
                j = jra_by_key.get(key)
                if not j:
                    continue
                rid = j["netkeiba_rid"]
                races.append({
                    "race_id": rid,
                    "url": f"https://race.netkeiba.com/race/shutuba.html?race_id={rid}",
                    "start_at": k["start_at"],
                    "venue": k["venue"],
                    "race_no": k["race_no"],
                    "source": "keibabook",   # = JRA (keibago/oddspark でない印)
                })
                joined += 1
            console.print(
                f"[cyan]JRA discovery: keibabook {len(kb)} × JRA 公式 {len(jra)} → "
                f"join {joined} races[/cyan]"
            )
        else:
            console.print(f"[yellow]keibabook JRA discovery 0 件 (当日 JRA 無し or 取得失敗)[/yellow]")
    except Exception as ex:  # noqa: BLE001
        console.print(f"[red]JRA discovery 失敗: {ex}[/red]")
    if not races:
        console.print(
            "[yellow]race discovery 0 件 (NAR/JRA とも当日無し or 公式ソース不通)[/yellow]"
        )

    # 検出帯は「**締切まで** window〜window+tolerance 分」の片側 (+のみ)。締切は発走の
    # CLOSE_LEAD_SEC 秒前で固定 (parse.close_at_for_start)。締切基準にすることで、レース
    # スケジュールが変わっても「賭けの締切前の lead time」が一定になる。
    # 片側 (+のみ) なのは window 分以上のリードを必ず確保し、締切間際の解析を防ぐため。
    from .parse import close_at_for_start
    low_sec = window_min * 60
    high_sec = (window_min + tolerance_min) * 60

    out: list[dict] = []
    for r in races:
        start_at = r.get("start_at") or 0
        if start_at <= 0:
            continue
        close_at = close_at_for_start(start_at)
        delta = close_at - now_ts   # 締切までの秒数
        if not (low_sec <= delta <= high_sec):
            continue
        rid = r["race_id"]
        out.append({
            "race_id": _normalize_race_id(rid),
            "netkeiba_race_id": rid,
            "url": r["url"],
            "start_at": start_at,
            "close_at": close_at,
            "delta_sec": delta,   # 締切まで残り秒
            "venue": r["venue"],
            "race_no": r["race_no"],
            "source": r.get("source", "netkeiba"),
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


def _dispatch_keibago(netkeiba_rid: str, start_at: int = 0,
                      *, market_blend: float | None = None,
                      aptitude_top: int | None = None) -> int:
    """NAR: keiba.go.jp の全6券種オッズで解析し snapshot を保存。"""
    cmd = [sys.executable, "-m", "src.scrape_keibago", netkeiba_rid,
           "--snapshot", f"--start-at={start_at}"]
    if market_blend is not None:
        cmd.append(f"--market-blend={market_blend}")
    if aptitude_top is not None:
        cmd.append(f"--aptitude-top={aptitude_top}")
    console.print(f"[bold cyan]→ keiba.go.jp analyze:[/bold cyan] {netkeiba_rid}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _dispatch_oddspark(netkeiba_rid: str, start_at: int = 0,
                       *, market_blend: float | None = None,
                       aptitude_top: int | None = None) -> int:
    """NAR: oddspark オッズで解析し snapshot を保存 (keibago 不可時)。"""
    cmd = [sys.executable, "-m", "src.scrape_oddspark", netkeiba_rid,
           "--snapshot", f"--start-at={start_at}"]
    if market_blend is not None:
        cmd.append(f"--market-blend={market_blend}")
    if aptitude_top is not None:
        cmd.append(f"--aptitude-top={aptitude_top}")
    console.print(f"[bold cyan]→ oddspark analyze:[/bold cyan] {netkeiba_rid}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _dispatch_jra(netkeiba_rid: str, start_at: int = 0,
                  *, market_blend: float | None = None,
                  aptitude_top: int | None = None) -> int:
    """JRA: 公式 (accessO.html token walk) で全7券種オッズを取得して snapshot 保存。"""
    cmd = [sys.executable, "-m", "src.scrape_jra", netkeiba_rid,
           "--snapshot", f"--start-at={start_at}"]
    if market_blend is not None:
        cmd.append(f"--market-blend={market_blend}")
    if aptitude_top is not None:
        cmd.append(f"--aptitude-top={aptitude_top}")
    console.print(f"[bold cyan]→ JRA 公式 analyze:[/bold cyan] {netkeiba_rid}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _dispatch_nar_fallback(netkeiba_rid: str, start_at: int = 0,
                           *, market_blend: float | None = None,
                           aptitude_top: int | None = None) -> int:
    """NAR フォールバック: keiba.go.jp (全6券種・組合せ明示) を優先、失敗時 oddspark。

    keiba.go.jp は馬連/ワイド/馬単/3連複/3連単 を組合せ明示で取れるので oddspark
    (単複/3連単のみ + グリッド誤オッズ回避で他を無効) より優れる。当日 NAR で
    keiba.go.jp が解決できない (場名/開催) 場合のみ oddspark に落ちる。
    """
    rc = _dispatch_keibago(netkeiba_rid, start_at,
                           market_blend=market_blend, aptitude_top=aptitude_top)
    if rc != 0:
        console.print("[yellow]keiba.go.jp 不可 → oddspark にフォールバック[/yellow]")
        rc = _dispatch_oddspark(netkeiba_rid, start_at,
                                market_blend=market_blend, aptitude_top=aptitude_top)
    return rc


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
    if start_min <= end_min:
        return start_min <= now_min <= end_min
    # 日跨ぎ範囲 (例 "22:00-01:00"): start 以降 または end 以前なら active。
    return now_min >= start_min or now_min <= end_min


@app.command()
def main(
    window_min: int = typer.Option(5, "--window", help="**締切までの**目標リード時間 (分)。締切=発走2分前固定なので、発走基準より +2 分の lead になる"),
    tolerance_min: int = typer.Option(2, "--tolerance", help="window からの + 側許容 (分)。締切 window〜window+tolerance 分前で検出 (= 発走 window+2〜window+tolerance+2 分前)"),
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
    bet_oddspark: bool = typer.Option(
        False, "--bet-oddspark",
        help="束(legs)が出た発走前 NAR レースを oddspark betting queue に投入する。別途 "
             "`python -m src.oddspark_bet --session` を起動しログインしておくこと (購入確定は人)。",
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
        f"[dim]{now_str}[/dim] 締切 {window_min}〜{window_min + tolerance_min} 分前のレースを検索中... (= 発走 {window_min + 2}〜{window_min + tolerance_min + 2} 分前)"
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
    # 公式ソース (keibago) は単複/馬連/ワイド/馬単/3連複/3連単 を組合せ明示で常時 fetch する
    # ため --with-exacta/--with-trio は no-op。--ev-max/--min-prob は keibago の plan filter に
    # まだ通っていない (TODO: 必要なら scrape_keibago に --min-prob を足す)。
    # 効くのは --market-blend / --aptitude-top (下の _dispatch_nar_fallback で渡す)。
    _ = (ev_max, min_prob, with_exacta, with_trio)   # 受け取るが現状未配線 (no-op)

    for race in due:
        rid = race["race_id"]
        mins = race["delta_sec"] / 60.0   # 締切までの分数 (発走 - 2 分 - 現在)
        tag = f"{race['venue']} {race['race_no']}R 締切まで {mins:.1f}分 (発走まで {mins + 2:.1f}分)"
        if rid in analyzed:
            console.print(f"[dim]skip (already analyzed): {tag} {rid}[/dim]")
            continue
        if _recently_failed(rid, int(time.time())):
            console.print(
                f"[dim]skip (recently failed, cooldown {FAILED_RETRY_COOLDOWN_SEC}s): {tag} {rid}[/dim]"
            )
            continue
        console.print(f"[bold green]match:[/bold green] {tag} ({rid})")
        if dry_run:
            console.print(f"  [dim]dry-run: {race['url']}[/dim]")
            continue
        started_at = int(time.time())
        # 公式ソース dispatch:
        #   NAR (source=oddspark)  → keiba.go.jp (全6券種) 優先・oddspark フォールバック
        #   JRA (source=keibabook) → JRA 公式 accessO.html (全7券種)
        # netkeiba live は一切使わない。
        if race.get("source") == "keibabook":
            rc = _dispatch_jra(
                race["netkeiba_race_id"], race.get("start_at", 0),
                market_blend=market_blend, aptitude_top=aptitude_top,
            )
        else:
            rc = _dispatch_nar_fallback(
                race["netkeiba_race_id"], race.get("start_at", 0),
                market_blend=market_blend, aptitude_top=aptitude_top,
            )
        finished_at = int(time.time())
        if rc == 0:
            _mark_analyzed(rid)
            analyzed.add(rid)  # 同 tick 内の重複 rid (oddspark+netkeiba 両経路等) を二重 dispatch しない
            if bet_oddspark:
                try:
                    if _enqueue_oddspark_bet(rid, race.get("netkeiba_race_id", rid)):
                        console.print(
                            f"  [magenta]→ oddspark betting queue に投入:[/magenta] {rid} "
                            "(--session daemon がカート投入。確定は人)")
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]oddspark enqueue 失敗: {e}[/red]")
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
