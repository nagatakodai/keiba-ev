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
IPAT_BET_QUEUE_DIR = ROOT / "data/cache/ipat_bet_queue"  # = ipat_bet.QUEUE_DIR (JRA 投票)

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)

# 投票束は **Plan T (recommended_bundle_t, 3連単的中モード) 固定** (ユーザ指示 2026-06-06)。
# 旧 EV束 (recommended_bundle) の投票/claude 選定 (回収優先AI) は廃止。recommended_bundle は
# snapshot にモデルのみの参考値として残るが、enqueue/投票には一切使わない。
def _bet_bundle_source() -> str:
    """投票束 source token。常に "plan_t" (Plan T 特化)。"""
    return "plan_t"


def _bet_bundle_field() -> str:
    return "recommended_bundle_t"


def _plan_t_missing_claude_index(d: dict) -> bool:
    """Claude 指数が無い (= model fallback) かを返す (True なら投票しない)。

    Plan T「3連単的中モード」は Claude 各馬指数フォーメーションが本質なので、指数キャッシュが
    無く model ランキングへ縮退した束 (rank_source != "claude") は投票しない (ユーザ指示 2026-06-03)。
    """
    bundle = d.get(_bet_bundle_field()) or {}
    return bundle.get("rank_source") != "claude"


# 2段パイプライン: score / bet で dedup 名前空間を分ける。同一 race を score で済ませても
# bet 帯で skip されないようにする (= 共有すると bet が全 skip され賭けが走らない致命バグ)。
# bet は既存ファイルを使い後方互換、score は別ファイル。
CACHE_FILE_SCORE = ROOT / "data/cache/auto_watch_analyzed_score.txt"


def _analyzed_file(phase: str):
    return CACHE_FILE_SCORE if phase == "score" else CACHE_FILE


def _load_analyzed(phase: str = "bet") -> set[str]:
    f = _analyzed_file(phase)
    if not f.exists():
        return set()
    return {line.strip() for line in f.read_text(encoding="utf-8").splitlines() if line.strip()}


def _mark_analyzed(race_id: str, phase: str = "bet") -> None:
    f = _analyzed_file(phase)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(race_id + "\n")


# 2段パイプライン: score 完了で「このレースを締切 BET_LEAD_SEC 秒前に投票せよ」を予約し、
# bet は band スキャンせず**予約時刻が来たら発火**する (= 締切1分前を探さず締切1分前に撃つ)。
BET_SCHEDULE_DIR = ROOT / "data/cache/auto_watch_bet_schedule"
# 締切の何秒前に投票を発火するか (既定 60 = 締切1分前)。poll 間隔ぶん遅れ得るので、daemon の
# カート投入+確定が締切に間に合うよう余裕を見て調整可 (--bet-lead-sec)。
BET_LEAD_SEC_DEFAULT = 60


def _write_bet_schedule(race: dict) -> None:
    """score 完了レースを bet 予約に書く (close_at をキーに後で時刻発火)。"""
    import json
    BET_SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    rid = race["race_id"]
    entry = {
        "race_id": rid,
        "netkeiba_race_id": race.get("netkeiba_race_id", rid),
        "source": race.get("source"),
        "url": race.get("url"),
        "venue": race.get("venue"),
        "race_no": race.get("race_no"),
        "start_at": race.get("start_at", 0),
        "close_at": race.get("close_at", 0),
    }
    tmp = BET_SCHEDULE_DIR / f".{rid}.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    tmp.rename(BET_SCHEDULE_DIR / f"{rid}.json")


def _read_bet_schedule() -> list[dict]:
    import json
    if not BET_SCHEDULE_DIR.exists():
        return []
    out = []
    for p in sorted(BET_SCHEDULE_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def _remove_bet_schedule(race_id: str) -> None:
    try:
        (BET_SCHEDULE_DIR / f"{race_id}.json").unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


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
    if _plan_t_missing_claude_index(d):
        console.print(f"[yellow]Plan T enqueue skip: Claude 指数なし (rank_source≠claude) — 投票しない ({netkeiba_rid})[/yellow]")
        return False
    legs = [l for l in ((d.get(_bet_bundle_field()) or {}).get("legs") or [])
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
        "bundle_source": _bet_bundle_source(),
        "enqueued_at": int(time.time()),
    }, ensure_ascii=False), encoding="utf-8")
    tmp.rename(req)   # atomic
    return True


def _enqueue_ipat_bet(race_id: str, netkeiba_rid: str) -> bool:
    """snapshot に束(legs)があれば JRA 即PAT 常駐 betting セッションの queue に投入する。

    `--bet-ipat` 時のみ呼ぶ。`ipat_bet --session` daemon が <rid>.req を拾ってカート投入する
    (購入確定は人、--auto-purchase で全自動)。**JRA レース (venue 01-10) のみ**・束が非空のみ・
    未投入のみ enqueue。oddspark (NAR) の対になる JRA 専用経路。**賭金は動かない**。
    """
    if not (netkeiba_rid.isdigit() and len(netkeiba_rid) == 12):
        return False
    from .ipat_bet import _is_jra_rid
    if not _is_jra_rid(netkeiba_rid):
        return False   # NAR / 未対応 → IPAT では投票しない
    snap = ROOT / "data/predictions" / f"{race_id}.json"
    if not snap.exists():
        return False
    try:
        d = json.loads(snap.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if _plan_t_missing_claude_index(d):
        console.print(f"[yellow]Plan T enqueue skip: Claude 指数なし (rank_source≠claude) — 投票しない ({netkeiba_rid})[/yellow]")
        return False
    legs = [l for l in ((d.get(_bet_bundle_field()) or {}).get("legs") or [])
            if int(l.get("stake", 0)) > 0]
    if not legs:
        return False   # 見送り (束が空) は投入しない
    IPAT_BET_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    req = IPAT_BET_QUEUE_DIR / f"{netkeiba_rid}.req"
    if req.exists() or (IPAT_BET_QUEUE_DIR / f"{netkeiba_rid}.done").exists():
        return False   # 既に投入/処理済
    tmp = req.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "netkeiba_rid": netkeiba_rid, "race_id": race_id, "legs": len(legs),
        "total_stake": sum(int(l.get("stake", 0)) for l in legs),
        "bundle_source": _bet_bundle_source(),
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


def _list_due_races(window_min: float, tolerance_min: float, now_ts: int) -> list[dict]:
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


def _phase_args(phase: str, llm_blend: float | None) -> list[str]:
    """dispatch subprocess に渡す共通 phase 引数 (--phase / --llm-blend)。"""
    args = [f"--phase={phase}"]
    if llm_blend is not None:
        args.append(f"--llm-blend={llm_blend}")
    return args


def _dispatch_keibago(netkeiba_rid: str, start_at: int = 0,
                      *, market_blend: float | None = None,
                      aptitude_top: int | None = None, no_llm: bool = False,
                      phase: str = "bet", llm_blend: float | None = None) -> int:
    """NAR: keiba.go.jp の全6券種オッズで解析し snapshot を保存 (phase=score で指数キャッシュ)。"""
    cmd = [sys.executable, "-m", "src.scrape_keibago", netkeiba_rid,
           "--snapshot", f"--start-at={start_at}", *_phase_args(phase, llm_blend)]
    if market_blend is not None:
        cmd.append(f"--market-blend={market_blend}")
    if aptitude_top is not None:
        cmd.append(f"--aptitude-top={aptitude_top}")
    if no_llm:
        cmd.append("--no-llm")
    console.print(f"[bold cyan]→ keiba.go.jp analyze ({phase}):[/bold cyan] {netkeiba_rid}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _dispatch_oddspark(netkeiba_rid: str, start_at: int = 0,
                       *, market_blend: float | None = None,
                       aptitude_top: int | None = None, no_llm: bool = False,
                       phase: str = "bet", llm_blend: float | None = None) -> int:
    """NAR: oddspark オッズで解析し snapshot を保存 (keibago 不可時)。

    keibago/jra と同じ 2段パイプライン対応: phase=score で Claude 指数キャッシュ、
    phase=bet で指数合成 + Plan T 束生成 (指数無しなら機械フォーメーションへ縮退)。
    """
    cmd = [sys.executable, "-m", "src.scrape_oddspark", netkeiba_rid,
           "--snapshot", f"--start-at={start_at}", *_phase_args(phase, llm_blend)]
    if market_blend is not None:
        cmd.append(f"--market-blend={market_blend}")
    if aptitude_top is not None:
        cmd.append(f"--aptitude-top={aptitude_top}")
    if no_llm:
        cmd.append("--no-llm")
    console.print(f"[bold cyan]→ oddspark analyze ({phase}):[/bold cyan] {netkeiba_rid}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _dispatch_jra(netkeiba_rid: str, start_at: int = 0,
                  *, market_blend: float | None = None,
                  aptitude_top: int | None = None, no_llm: bool = False,
                  phase: str = "bet", llm_blend: float | None = None) -> int:
    """JRA: 公式 (accessO.html token walk) で全7券種オッズを取得して snapshot 保存。"""
    cmd = [sys.executable, "-m", "src.scrape_jra", netkeiba_rid,
           "--snapshot", f"--start-at={start_at}", *_phase_args(phase, llm_blend)]
    if market_blend is not None:
        cmd.append(f"--market-blend={market_blend}")
    if aptitude_top is not None:
        cmd.append(f"--aptitude-top={aptitude_top}")
    if no_llm:
        cmd.append("--no-llm")
    console.print(f"[bold cyan]→ JRA 公式 analyze ({phase}):[/bold cyan] {netkeiba_rid}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def _dispatch_nar_fallback(netkeiba_rid: str, start_at: int = 0,
                           *, market_blend: float | None = None,
                           aptitude_top: int | None = None, no_llm: bool = False,
                           phase: str = "bet", llm_blend: float | None = None) -> int:
    """NAR フォールバック: keiba.go.jp (全6券種・組合せ明示) を優先、失敗時 oddspark。

    keiba.go.jp は馬連/ワイド/馬単/3連複/3連単 を組合せ明示で取れるので oddspark
    (単複/3連単のみ + グリッド誤オッズ回避で他を無効) より優れる。当日 NAR で
    keiba.go.jp が解決できない (場名/開催) 場合のみ oddspark に落ちる。
    """
    rc = _dispatch_keibago(netkeiba_rid, start_at,
                           market_blend=market_blend, aptitude_top=aptitude_top,
                           no_llm=no_llm, phase=phase, llm_blend=llm_blend)
    if rc != 0:
        console.print("[yellow]keiba.go.jp 不可 → oddspark にフォールバック[/yellow]")
        rc = _dispatch_oddspark(netkeiba_rid, start_at,
                                market_blend=market_blend, aptitude_top=aptitude_top,
                                no_llm=no_llm, phase=phase, llm_blend=llm_blend)
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
    window_min: float = typer.Option(1, "--window", help="**bet 帯** 締切までの目標リード時間 (分)。締切=発走2分前固定。発走前 bet 用に既定 1 分 (=締切1分前にオッズ取得+購入)。小数可"),
    tolerance_min: float = typer.Option(1.5, "--tolerance", help="bet 帯 window からの + 側許容 (分)。締切 window〜window+tolerance 分前で検出。小数可"),
    score_window: float = typer.Option(5, "--score-window", help="**score 帯** 締切までのリード (分)。Claude 考察で各馬指数を出しキャッシュする早回し。既定 5 分前"),
    score_tolerance: float = typer.Option(2, "--score-tolerance", help="score 帯の + 側許容 (分)。締切 score_window〜+tolerance 分前で考察。小数可"),
    llm_blend: float = typer.Option(None, "--llm-blend", help="Claude 指数と model fundamental の合成重み (未指定なら各 analyze の既定 0.5)"),
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
    bet_ipat: bool = typer.Option(
        False, "--bet-ipat",
        help="束(legs)が出た発走前 JRA レースを 即PAT betting queue に投入する。別途 "
             "`python -m src.ipat_bet --session` を起動しログインしておくこと (購入確定は人)。",
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm",
        help="claude -p による各馬指数 (考察) を行わず確率モデルのみで snapshot を保存する。"
             "予約・締切発火の枠組みは同じ (bet はモデルのみで撃つ)。",
    ),
    bet_lead_sec: int = typer.Option(
        BET_LEAD_SEC_DEFAULT, "--bet-lead-sec",
        help="締切の何秒前に投票を発火するか (既定 60=締切1分前)。score 完了で予約し、この秒数に "
             "達した tick で発火する。poll 間隔ぶん遅れ得るので daemon の確定が間に合うよう調整可。",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """1 巡だけ実行。2段: score 帯で考察→指数キャッシュ+**締切前 bet を予約** → 予約時刻 (締切
    bet_lead_sec 秒前) が来た bet を**自動発火** (最新オッズ→束→enqueue)。"""
    if (bet_oddspark or bet_ipat):
        console.print(f"[dim]bet enqueue 束 = {_bet_bundle_field()} (Plan T 3連単的中モード固定)[/dim]")
    now_dt = datetime.now()
    now_ts = int(now_dt.timestamp())
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    _drain_pending(label="pre-analyze")

    # 予約済 bet の発火は active-hours 外でも行う (締切は時刻で来るため)。result fetch も継続。
    _fire_due_bets(
        now_ts, bet_lead_sec=bet_lead_sec,
        market_blend=market_blend, aptitude_top=aptitude_top, no_llm=no_llm,
        llm_blend=llm_blend, bet_oddspark=bet_oddspark, bet_ipat=bet_ipat, dry_run=dry_run,
    )

    if not _in_active_hours(now_dt, active_hours):
        console.print(
            f"[dim]{now_str}[/dim] off-hours ({active_hours} 外): "
            "race detection skip、bet 発火/result fetch のみ"
        )
        return

    _ = (ev_max, min_prob, with_exacta, with_trio, window_min, tolerance_min)   # 未配線 (no-op)

    # score 帯 (締切 score_window〜+tol 分前): Claude 考察で各馬指数をキャッシュし、
    # 同時に「締切 bet_lead_sec 秒前に投票」を予約する。bet はその予約時刻に発火 (上の _fire_due_bets)。
    _run_phase(
        "score", score_window, score_tolerance, now_ts,
        market_blend=market_blend, aptitude_top=aptitude_top, no_llm=no_llm,
        bet_oddspark=bet_oddspark, bet_ipat=bet_ipat, dry_run=dry_run, llm_blend=llm_blend,
    )


def _run_phase(
    phase: str, window_min: float, tolerance_min: float, now_ts: int,
    *, market_blend, aptitude_top, no_llm: bool,
    bet_oddspark: bool, bet_ipat: bool, dry_run: bool, llm_blend,
) -> None:
    """1 巡分の race 検出→dispatch を 1 phase 実行する。phase=score は指数キャッシュのみ、
    phase=bet は束生成+enqueue+result fetch スケジュール。dedup は phase 名前空間で独立。
    """
    label = "考察(score)" if phase == "score" else "投票(bet)"
    console.print(
        f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim] [{label}] 締切 "
        f"{window_min:g}〜{window_min + tolerance_min:g} 分前を検索中... "
        f"(= 発走 {window_min + 2:g}〜{window_min + tolerance_min + 2:g} 分前)"
    )
    try:
        due = _list_due_races(window_min, tolerance_min, now_ts)
    except Exception as e:
        console.print(f"[red]race_list 取得失敗: {e}[/red]")
        return
    if not due:
        console.print(f"[dim]該当レースなし ({label})[/dim]")
        return

    analyzed = _load_analyzed(phase)
    for race in due:
        rid = race["race_id"]
        mins = race["delta_sec"] / 60.0
        tag = f"{race['venue']} {race['race_no']}R 締切まで {mins:.1f}分 (発走まで {mins + 2:.1f}分)"
        if rid in analyzed:
            console.print(f"[dim]skip ({phase} 済): {tag} {rid}[/dim]")
            continue
        if _recently_failed(rid, int(time.time())):
            console.print(
                f"[dim]skip (recently failed, cooldown {FAILED_RETRY_COOLDOWN_SEC}s): {tag} {rid}[/dim]"
            )
            continue
        console.print(f"[bold green]match ({phase}):[/bold green] {tag} ({rid})")
        if dry_run:
            console.print(f"  [dim]dry-run: {race['url']}[/dim]")
            continue
        started_at = int(time.time())
        if race.get("source") == "keibabook":
            rc = _dispatch_jra(
                race["netkeiba_race_id"], race.get("start_at", 0),
                market_blend=market_blend, aptitude_top=aptitude_top, no_llm=no_llm,
                phase=phase, llm_blend=llm_blend,
            )
        else:
            rc = _dispatch_nar_fallback(
                race["netkeiba_race_id"], race.get("start_at", 0),
                market_blend=market_blend, aptitude_top=aptitude_top, no_llm=no_llm,
                phase=phase, llm_blend=llm_blend,
            )
        finished_at = int(time.time())
        if rc == 0:
            _mark_analyzed(rid, phase)
            analyzed.add(rid)
            # score 完了 → このレースを「締切 BET_LEAD_SEC 秒前に投票」予約する。
            # bet はこの予約時刻が来たら発火する (band スキャンしない)。
            if phase == "score":
                try:
                    _write_bet_schedule(race)
                    cl = race.get("close_at", 0)
                    when = (datetime.fromtimestamp(cl - BET_LEAD_SEC_DEFAULT).strftime("%H:%M:%S")
                            if cl else "?")
                    console.print(f"  [cyan]→ bet 予約: {rid} を 締切前に発火 (≈ {when})[/cyan]")
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]bet 予約失敗: {e}[/red]")
            # enqueue + result fetch は bet 帯のみ (score 帯は指数キャッシュだけ)。
            if phase == "bet":
                if bet_oddspark:
                    try:
                        if _enqueue_oddspark_bet(rid, race.get("netkeiba_race_id", rid)):
                            console.print(
                                f"  [magenta]→ oddspark betting queue に投入:[/magenta] {rid} "
                                "(--session daemon がカート投入。確定は人)")
                    except Exception as e:  # noqa: BLE001
                        console.print(f"[red]oddspark enqueue 失敗: {e}[/red]")
                if bet_ipat:
                    try:
                        if _enqueue_ipat_bet(rid, race.get("netkeiba_race_id", rid)):
                            console.print(
                                f"  [magenta]→ 即PAT betting queue に投入:[/magenta] {rid} "
                                "(--session daemon がカート投入。確定は人)")
                    except Exception as e:  # noqa: BLE001
                        console.print(f"[red]ipat enqueue 失敗: {e}[/red]")
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
            console.print(f"[red]analyze 失敗 rc={rc} race={rid} ({phase})[/red]")
        _append_history({
            "started_at": started_at,
            "finished_at": finished_at,
            "phase": phase,
            "race_id": rid,
            "netkeiba_race_id": race.get("netkeiba_race_id", rid),
            "url": race["url"],
            "venue": race["venue"],
            "race_no": race["race_no"],
            "start_at": race["start_at"],
            "close_at": race["close_at"],
            "rc": rc,
        })


def _fire_due_bets(
    now_ts: int, *, bet_lead_sec: int, market_blend, aptitude_top, no_llm: bool,
    llm_blend, bet_oddspark: bool, bet_ipat: bool, dry_run: bool,
) -> None:
    """bet 予約を読み、**締切 bet_lead_sec 秒前に達したレースを発火** (最新オッズで束→enqueue)。

    band スキャンせず予約時刻ベース。締切を過ぎた予約は破棄 (撃っても不成立)。発火済 (rc==0)
    は予約を消し analyzed_bet で二重防止。未発走 (まだ時刻でない) はそのまま残す。
    """
    sched = _read_bet_schedule()
    if not sched:
        return
    analyzed = _load_analyzed("bet")
    for race in sched:
        rid = race["race_id"]
        close_at = int(race.get("close_at") or 0)
        if rid in analyzed:
            _remove_bet_schedule(rid)
            continue
        if not close_at:
            continue
        # 締切を過ぎた予約は破棄 (発火しても締切後で不成立)
        if now_ts >= close_at:
            console.print(f"[dim]bet 予約破棄 (締切経過): {rid}[/dim]")
            _remove_bet_schedule(rid)
            continue
        # まだ発火時刻 (締切 bet_lead_sec 秒前) に達していない
        if close_at - now_ts > bet_lead_sec:
            continue
        secs = close_at - now_ts
        tag = f"{race.get('venue')} {race.get('race_no')}R 締切まで {secs}s"
        console.print(f"[bold green]bet 発火:[/bold green] {tag} ({rid})")
        if dry_run:
            console.print(f"  [dim]dry-run: {race.get('url')}[/dim]")
            continue
        nkrid = race.get("netkeiba_race_id", rid)
        if race.get("source") == "keibabook":
            rc = _dispatch_jra(nkrid, race.get("start_at", 0),
                               market_blend=market_blend, aptitude_top=aptitude_top,
                               no_llm=no_llm, phase="bet", llm_blend=llm_blend)
        else:
            rc = _dispatch_nar_fallback(nkrid, race.get("start_at", 0),
                                        market_blend=market_blend, aptitude_top=aptitude_top,
                                        no_llm=no_llm, phase="bet", llm_blend=llm_blend)
        if rc != 0:
            console.print(f"[red]bet 発火 analyze 失敗 rc={rc} race={rid} (次tickで再試行)[/red]")
            continue   # 予約は残す (締切前なら次tickで再試行)
        _mark_analyzed(rid, "bet")
        _remove_bet_schedule(rid)
        if bet_oddspark:
            try:
                if _enqueue_oddspark_bet(rid, nkrid):
                    console.print(f"  [magenta]→ oddspark betting queue に投入:[/magenta] {rid}")
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]oddspark enqueue 失敗: {e}[/red]")
        if bet_ipat:
            try:
                if _enqueue_ipat_bet(rid, nkrid):
                    console.print(f"  [magenta]→ 即PAT betting queue に投入:[/magenta] {rid}")
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]ipat enqueue 失敗: {e}[/red]")
        try:
            p = schedule_result_fetch(rid, race.get("url", ""), race.get("start_at", 0))
            if p.status == "pending":
                console.print(f"  [cyan]→ result fetch scheduled:[/cyan] {rid}")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]schedule_result_fetch 失敗: {e}[/red]")
        _drain_pending(label="post-bet")


if __name__ == "__main__":
    app()
