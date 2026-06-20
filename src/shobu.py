"""今日の勝負レース スキャン。

`discover_today_races` で当日の全レースを **netkeiba 非依存** で列挙し、各レースを 2 つの基準で
採点して「勝負レース (= 通常より賭ける価値が高いと思われるレース)」を抽出する:

 (A) **強弱がはっきり** — 市場の単勝 implied 勝率分布の集中度。`sep_score = 100·(1 − 正規化エントロピー)`。
     一様な (どの馬も同じくらい) フィールドは 0、1〜数頭が突出していれば高い。
     データ源: 最新オッズの軽量 fetch (単勝のみ 1 リクエスト/レース) か、既存 snapshot の market_win_index 復元。

 (B) **市場より Claude 指数が高い馬が複数** — 既存 snapshot (watch-auto / 手動 score) の index_compare で
     `claude_index − market_index ≥ margin` の馬数。複数 (既定 2 頭) で「Claude が市場と乖離している妙味レース」。

基準の ON/OFF・しきい値・OR/AND・JRA/NAR・発走前のみ・最新オッズ取得・Claude 指数の新規生成(上位N件) は
全て option (CLI / Web UI から指定)。結果は JSON で `--out` に書き出す (API がそれを配信)。

ユーザ指示 (2026-06-20): 基準 B は **既存スナップショット中心** (無料・即時)。新規 claude -p 生成は既定 OFF で、
`--claude-eval N` のときだけ上位 N 件の未解析レースに対して score ステージを spawn する。
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
PY = str(ROOT / ".venv" / "bin" / "python")
if not Path(PY).exists():
    PY = sys.executable
JST = ZoneInfo("Asia/Tokyo")

# ヘッダの netkeiba 場コード (01-10) は JRA。それ以外は NAR (= discovery の source でも判定)。
_JRA_VENUE_CODES = {f"{i:02d}" for i in range(1, 11)}


def today_jst() -> str:
    """当日 (JST) を YYYYMMDD で返す。"""
    return datetime.now(JST).strftime("%Y%m%d")


def _race_type(rid: str, source: str) -> str:
    """JRA / NAR の判定。discovery の source ("keibabook"=JRA / "oddspark"=NAR) を優先し、
    無ければ netkeiba rid の場コードで判定する。"""
    s = (source or "").lower()
    if s == "keibabook":
        return "jra"
    if s in ("oddspark", "keibago"):
        return "nar"
    return "jra" if (len(rid) >= 6 and rid[4:6] in _JRA_VENUE_CODES) else "nar"


def _internal_id(rid: str) -> str:
    """netkeiba rid → 内部 race_id (cup-si-rn)。snapshot / results の join key。"""
    from src.parse import _split_race_id
    if not rid or len(rid) != 12:
        return rid
    _venue, si, rn, cup = _split_race_id(rid)
    return f"{cup}-{si}-{rn}"


def _load_snapshot(internal_id: str) -> dict[str, Any] | None:
    """data/predictions/<internal>.json を読む。無ければ None。"""
    p = PRED_DIR / f"{internal_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- 強弱 (A) ----

def _implied_from_win_odds(odds_by_num: dict[int, float]) -> dict[int, float]:
    """単勝オッズ {馬番: odds} → 正規化 implied 勝率 (Σ=1)。控除は正規化で概ね相殺。"""
    raw = {n: 1.0 / o for n, o in odds_by_num.items() if o and o > 0}
    s = sum(raw.values())
    if s <= 0:
        return {}
    return {n: v / s for n, v in raw.items()}


def _implied_from_market_index(mwi: dict[str, float]) -> dict[int, float]:
    """snapshot の market_win_index (= 100·(1/odds)^(1/1.5)) から implied 勝率を復元 (Σ=1)。

    market_win_index と同じ尺度を使うので「最新オッズ取得」経路と整合する。
    """
    raw: dict[int, float] = {}
    for k, v in (mwi or {}).items():
        try:
            num = int(k)
            idx = float(v)
        except (TypeError, ValueError):
            continue
        if idx <= 0:
            continue
        # idx = 100·p^(1/1.5) → p = (idx/100)^1.5
        raw[num] = (idx / 100.0) ** 1.5
    s = sum(raw.values())
    if s <= 0:
        return {}
    return {n: v / s for n, v in raw.items()}


def _separation(implied: dict[int, float],
                names: dict[int, str] | None = None) -> dict[str, Any] | None:
    """implied 勝率分布 → 集中度メトリクス。

    sep_score = 100·(1 − H/ln n)。high = 強弱がはっきり (favorite が突出)。
    """
    probs = [(n, p) for n, p in implied.items() if p > 0]
    n = len(probs)
    if n < 2:
        return None
    H = -sum(p * math.log(p) for _num, p in probs)
    H_max = math.log(n)
    entropy_norm = (H / H_max) if H_max > 0 else 1.0
    sep_score = round(100.0 * (1.0 - entropy_norm), 1)
    ranked = sorted(probs, key=lambda kp: kp[1], reverse=True)
    top1 = ranked[0][1]
    top2 = ranked[1][1] if n > 1 else 0.0
    nm = names or {}
    favorites = [
        {"number": num, "name": nm.get(num, ""), "prob": round(p, 4)}
        for num, p in ranked[:3]
    ]
    return {
        "score": sep_score,
        "n": n,
        "top1": round(top1, 4),
        "top2": round(top2, 4),
        "gap": round(top1 - top2, 4),
        "entropy_norm": round(entropy_norm, 4),
        "favorites": favorites,
    }


# --------------------------------------------------------------- Claude (B) ----

def _claude_edge(snap: dict[str, Any], margin: float) -> dict[str, Any] | None:
    """snapshot から「Claude 指数 − 市場指数 ≥ margin」の馬を抽出。

    index_compare があればそれを使い、無ければ llm_win_index / market_win_index から自前で diff を計算。
    Claude 指数が一つも無い (score 未実施) snapshot は None (= 基準 B 評価不能)。
    """
    rows: list[dict[str, Any]] = []
    ic = snap.get("index_compare")
    if isinstance(ic, list) and ic:
        for r in ic:
            ci = r.get("claude_index")
            mi = r.get("market_index")
            if ci is None:
                continue
            diff = r.get("diff")
            if diff is None and mi is not None:
                diff = ci - mi
            rows.append({
                "number": r.get("number"),
                "name": r.get("name", ""),
                "claude_index": ci,
                "market_index": mi,
                "diff": diff,
                "support": r.get("support"),
                "alerts": r.get("alerts") or [],
            })
    else:
        lwi = snap.get("llm_win_index") or {}
        mwi = snap.get("market_win_index") or {}
        if not lwi:
            return None
        for k, ci in lwi.items():
            try:
                num = int(k)
                ci = float(ci)
            except (TypeError, ValueError):
                continue
            mi = mwi.get(k)
            mi = float(mi) if mi is not None else None
            diff = (ci - mi) if mi is not None else None
            rows.append({"number": num, "name": "", "claude_index": ci,
                         "market_index": mi, "diff": diff,
                         "support": None, "alerts": []})
    if not rows:
        return None
    # diff が出せた馬だけで edge を数える (margin 以上 = Claude が市場より強気)。
    edge_horses = [r for r in rows if r.get("diff") is not None and r["diff"] >= margin]
    edge_horses.sort(key=lambda r: r["diff"], reverse=True)
    diffs = [r["diff"] for r in rows if r.get("diff") is not None]
    max_diff = max(diffs) if diffs else None
    # claude_score: edge 馬の diff 合計 (capped 100)。馬数と乖離の大きさの両方を反映。
    claude_score = round(min(100.0, sum(max(0.0, r["diff"]) for r in edge_horses)), 1)
    return {
        "available": True,
        "edge_count": len(edge_horses),
        "max_diff": round(max_diff, 1) if max_diff is not None else None,
        "score": claude_score,
        "edge_horses": edge_horses[:6],
        "scored_at": snap.get("llm_scored_at"),
    }


# ---------------------------------------------------------------- fresh odds --

def _fetch_fresh_win(rid: str, rtype: str) -> dict[str, Any] | None:
    """最新の単勝のみを軽量 fetch → {"odds": {num:odds}, "names": {num:name}}。失敗は None。

    NAR は keiba.go.jp、JRA は JRA 公式 (netkeiba は使わない = IP 規制回避)。
    """
    try:
        if rtype == "nar":
            from src.scrape_keibago import fetch_keibago_win_list
            rows = fetch_keibago_win_list(rid)
        else:
            from src.scrape_jra import fetch_jra_win_list
            rows = fetch_jra_win_list(rid)
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    odds = {num: float(od) for num, _nm, od in rows if od and od > 0}
    names = {num: (nm or "") for num, nm, _od in rows}
    if not odds:
        return None
    return {"odds": odds, "names": names}


# -------------------------------------------------------------- claude eval ----

def _score_stage_cmd(rid: str, rtype: str, start_at: int) -> list[str]:
    """score ステージ (Claude 指数生成 + 暫定 snapshot) の subprocess コマンド。

    api/main.py の refresh-odds と同経路: NAR=keibago / JRA=jra に `--phase=score --snapshot`。
    """
    mod = "src.scrape_keibago" if rtype == "nar" else "src.scrape_jra"
    return [PY, "-m", mod, rid, "--snapshot", "--phase=score", f"--start-at={start_at}"]


def _select_claude_targets(results: list[dict[str, Any]], *, claude_all: bool,
                           claude_eval: int, upcoming_only: bool,
                           now: int) -> list[dict[str, Any]]:
    """Claude 指数を生成すべきレースを選ぶ。

    対象は「Claude 指数がまだ無い (= snapshot に未スコア) + (発走前のみなら) 締切前」のレース。
    claude_all=True なら **全件** (ボタンで一気に取得)、False かつ claude_eval>0 なら強弱スコア上位
    claude_eval 件だけ。既にスコア済 (snapshot に Claude 指数あり) のレースは二重生成しない。
    """
    if not (claude_all or claude_eval > 0):
        return []
    cand = [
        r for r in results
        if r["claude"] is None
        and (not upcoming_only or (r["close_at"] and r["close_at"] > now))
    ]
    # 強弱スコア降順 (上位 N モードで優先順位を付ける。全件モードでも処理順がこの順になる)。
    cand.sort(key=lambda r: (r["separation"]["score"] if r["separation"] else -1.0),
              reverse=True)
    return cand if claude_all else cand[:claude_eval]


def _run_claude_eval(targets: list[dict[str, Any]], *, timeout: int,
                     parallel: int, log: Callable[[str], None]) -> int:
    """対象レースに score ステージ (claude -p) を spawn して Claude 指数を生成。生成成功数を返す。

    並列は ThreadPoolExecutor で回すが、実際の claude -p 同時数は src 側の file-slot semaphore
    (KEIBA_LLM_MAX_CONCURRENT, 既定5) で頭打ちになる (fail-open)。各 race の subprocess 出力は
    capture して捨て、進捗だけ 1 行ずつ log に流す (scan のログを汚さない)。
    """
    if not targets:
        return 0
    total = len(targets)
    log(f"[claude-eval] {total} レースの Claude 指数を一括生成中 "
        f"(並列 {parallel} / 各 timeout {timeout}s)…")

    def _one(t: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
        rid = t["netkeiba_race_id"]
        cmd = _score_stage_cmd(rid, t["race_type"], int(t.get("start_at") or 0))
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return (t, False, f"timeout ({timeout}s)")
        except Exception as e:  # noqa: BLE001
            return (t, False, str(e))
        return (t, r.returncode == 0, "OK" if r.returncode == 0 else f"rc={r.returncode}")

    done = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        for fut in as_completed([ex.submit(_one, t) for t in targets]):
            t, ok, note = fut.result()
            completed += 1
            if ok:
                done += 1
            log(f"[claude-eval] ({completed}/{total}) {t['venue']}{t['race_no']}R {note}")
    log(f"[claude-eval] 完了: {done}/{total} 生成成功")
    return done


# ----------------------------------------------------------------- scan -------

def scan(
    *,
    date: str | None = None,
    race_type: str = "all",
    use_separation: bool = True,
    use_claude_edge: bool = True,
    combine: str = "or",
    sep_threshold: float = 35.0,
    edge_margin: float = 8.0,
    edge_min_count: int = 2,
    upcoming_only: bool = True,
    fetch_odds: bool = True,
    claude_all: bool = False,
    claude_eval: int = 0,
    claude_eval_timeout: int = 900,
    claude_eval_parallel: int = 6,
    max_races: int | None = None,
    fetch_parallel: int = 6,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """当日全レースを採点して勝負レースを抽出した dict を返す。"""
    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg, flush=True)

    date = date or today_jst()
    now = int(time.time())
    from src.auto_watch import discover_today_races
    from src.parse import close_at_for_start

    _log(f"[discover] {date} の開催一覧を取得中 (netkeiba 非依存)…")
    try:
        discovered = discover_today_races(date)
    except Exception as e:  # noqa: BLE001
        _log(f"[discover] 失敗: {e}")
        discovered = []
    _log(f"[discover] {len(discovered)} レース")

    # race_type フィルタ + 発走前フィルタ + メタ整形。
    races: list[dict[str, Any]] = []
    for d in discovered:
        rid = d.get("race_id") or ""
        if len(rid) != 12:
            continue
        rtype = _race_type(rid, d.get("source", ""))
        if race_type in ("jra", "nar") and rtype != race_type:
            continue
        start_at = int(d.get("start_at") or 0)
        close_at = close_at_for_start(start_at) if start_at else 0
        if upcoming_only and close_at and close_at <= now:
            continue  # 締切済 (発走前のみ)
        races.append({
            "netkeiba_race_id": rid,
            "race_id": _internal_id(rid),
            "venue": d.get("venue", ""),
            "race_no": int(d.get("race_no") or 0),
            "race_type": rtype,
            "start_at": start_at,
            "close_at": close_at,
            "source": d.get("source", ""),
            "url": d.get("url", ""),
        })
    races.sort(key=lambda r: (r["start_at"] or 0, r["race_no"]))
    if max_races is not None:
        races = races[:max_races]
    _log(f"[filter] 評価対象 {len(races)} レース "
         f"(race_type={race_type} / 発走前のみ={upcoming_only})")

    # 既存 snapshot をロードして、最新オッズ fetch が要るレースを決める。
    need_fresh: list[dict[str, Any]] = []
    for r in races:
        snap = _load_snapshot(r["race_id"])
        r["_snap"] = snap
        has_market = bool(snap and snap.get("market_win_index"))
        if fetch_odds and not has_market:
            need_fresh.append(r)

    # 最新オッズ (単勝のみ) を並列 fetch (snapshot に市場データが無いレースのみ)。
    if need_fresh:
        _log(f"[odds] {len(need_fresh)} レースの最新オッズ (単勝) を取得中…")
        ok = 0
        with ThreadPoolExecutor(max_workers=max(1, fetch_parallel)) as ex:
            futs = {ex.submit(_fetch_fresh_win, r["netkeiba_race_id"], r["race_type"]): r
                    for r in need_fresh}
            for fut in as_completed(futs):
                r = futs[fut]
                try:
                    res = fut.result()
                except Exception:  # noqa: BLE001
                    res = None
                if res:
                    r["_fresh"] = res
                    ok += 1
        _log(f"[odds] 取得成功 {ok}/{len(need_fresh)}")

    # 各レースを採点。
    def _evaluate(r: dict[str, Any]) -> dict[str, Any]:
        snap = r.get("_snap")
        fresh = r.get("_fresh")
        # --- 強弱 (A) ---
        sep = None
        sep_source = None
        if fresh:
            sep = _separation(_implied_from_win_odds(fresh["odds"]), fresh.get("names"))
            sep_source = "fresh"
        elif snap and snap.get("market_win_index"):
            implied = _implied_from_market_index(snap["market_win_index"])
            names = {}
            for h in (snap.get("horse_aptitude") or []):
                if h.get("number") is not None:
                    names[int(h["number"])] = h.get("name", "")
            sep = _separation(implied, names)
            sep_source = "snapshot"
        # --- Claude (B) ---
        claude = _claude_edge(snap, edge_margin) if snap else None
        # --- データ源 ---
        data_source = "fresh" if fresh else ("snapshot" if snap else "none")
        n_runners = (snap.get("n_runners") if snap else None) or (sep.get("n") if sep else None)
        # --- 判定 ---
        sep_avail = sep is not None
        claude_avail = claude is not None
        sep_pass = use_separation and sep_avail and sep["score"] >= sep_threshold
        claude_pass = (use_claude_edge and claude_avail
                       and claude["edge_count"] >= edge_min_count)
        active_passes = []
        if use_separation:
            active_passes.append(sep_pass)
        if use_claude_edge:
            active_passes.append(claude_pass)
        if not active_passes:
            recommended = False
        elif combine == "and":
            recommended = all(active_passes)
        else:
            recommended = any(active_passes)
        matched = []
        if sep_pass:
            matched.append("sep")
        if claude_pass:
            matched.append("claude")
        # --- score (ランキング用) ---
        sep_s = sep["score"] if (use_separation and sep_avail) else 0.0
        claude_s = claude["score"] if (use_claude_edge and claude_avail) else 0.0
        comps = [s for s, active in
                 ((sep_s, use_separation), (claude_s, use_claude_edge)) if active]
        if comps:
            shobu_score = round(max(comps) + 0.25 * min(comps), 1) if len(comps) > 1 else round(comps[0], 1)
            shobu_score = min(100.0, shobu_score)
        else:
            shobu_score = 0.0
        # --- reasons (人が読む) ---
        reasons: list[str] = []
        if sep_avail:
            fav = sep["favorites"][0] if sep["favorites"] else None
            favtxt = (f" / 1番手 {fav['number']}番 {int(fav['prob']*100)}%"
                      if fav else "")
            reasons.append(f"強弱スコア {sep['score']:.0f}{favtxt}")
        if claude_avail and claude["edge_count"] > 0:
            md = claude["max_diff"]
            reasons.append(f"Claude>市場 {claude['edge_count']}頭"
                           + (f" (最大+{md:.0f})" if md is not None else ""))
        return {
            "netkeiba_race_id": r["netkeiba_race_id"],
            "race_id": r["race_id"],
            "venue": r["venue"],
            "race_no": r["race_no"],
            "race_type": r["race_type"],
            "start_at": r["start_at"],
            "close_at": r["close_at"],
            "n_runners": n_runners,
            "data_source": data_source,
            "sep_source": sep_source,
            "has_snapshot": snap is not None,
            "snapshot_stage": (snap.get("stage") if snap else None),
            "separation": sep,
            "claude": claude,
            "recommended": recommended,
            "matched": matched,
            "shobu_score": shobu_score,
            "reasons": reasons,
        }

    results = [_evaluate(r) for r in races]

    # --- Claude 指数の生成 (ボタンで一括取得 / 上位N件) ---
    # claude_all=True: Claude 指数が無い発走前レースを **全件** 生成 (ボタン押下で一気に取得)。
    # claude_all=False & claude_eval>0: 強弱上位 claude_eval 件のみ。既にスコア済は二重生成しない。
    if use_claude_edge and (claude_all or claude_eval > 0):
        targets = _select_claude_targets(
            results, claude_all=claude_all, claude_eval=claude_eval,
            upcoming_only=upcoming_only, now=now)
        if targets:
            _run_claude_eval(targets, timeout=claude_eval_timeout,
                             parallel=claude_eval_parallel, log=_log)
            # 生成後に対象を再評価 (snapshot 再読込)。
            by_id = {r["race_id"]: r for r in races}
            pos = {r["race_id"]: i for i, r in enumerate(results)}
            for t in targets:
                base = by_id.get(t["race_id"])
                if base is None:
                    continue
                base["_snap"] = _load_snapshot(t["race_id"])
                results[pos[t["race_id"]]] = _evaluate(base)
        else:
            _log("[claude-eval] 生成対象なし (全レース既に Claude 指数あり/締切済)")

    # 並べ替え: 推奨を上に、その中で shobu_score 降順。
    results.sort(key=lambda r: (r["recommended"], r["shobu_score"]), reverse=True)

    sep_scores = [r["separation"]["score"] for r in results if r["separation"]]
    sep_median = round(sorted(sep_scores)[len(sep_scores) // 2], 1) if sep_scores else None
    summary = {
        "total_discovered": len(discovered),
        "evaluated": len(results),
        "recommended": sum(1 for r in results if r["recommended"]),
        "with_snapshot": sum(1 for r in results if r["has_snapshot"]),
        "with_claude": sum(1 for r in results if r["claude"]),
        "with_fresh_odds": sum(1 for r in results if r["data_source"] == "fresh"),
        "sep_median": sep_median,
    }
    options = {
        "date": date, "race_type": race_type,
        "use_separation": use_separation, "use_claude_edge": use_claude_edge,
        "combine": combine, "sep_threshold": sep_threshold,
        "edge_margin": edge_margin, "edge_min_count": edge_min_count,
        "upcoming_only": upcoming_only, "fetch_odds": fetch_odds,
        "claude_all": claude_all, "claude_eval": claude_eval,
    }
    _log(f"[done] 推奨 {summary['recommended']} / 評価 {summary['evaluated']} "
         f"(snapshot {summary['with_snapshot']} / Claude {summary['with_claude']})")
    return {
        "date": date,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "options": options,
        "summary": summary,
        "races": results,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="今日の勝負レース スキャン")
    ap.add_argument("--out", help="結果 JSON の出力先 (省略時は stdout に最後の1行で出す)")
    ap.add_argument("--date", default=None, help="YYYYMMDD (省略時は当日 JST)")
    ap.add_argument("--race-type", choices=["all", "jra", "nar"], default="all")
    ap.add_argument("--no-separation", action="store_true", help="基準A (強弱) を無効化")
    ap.add_argument("--no-claude", action="store_true", help="基準B (Claude>市場) を無効化")
    ap.add_argument("--combine", choices=["or", "and"], default="or")
    ap.add_argument("--sep-threshold", type=float, default=35.0)
    ap.add_argument("--edge-margin", type=float, default=8.0)
    ap.add_argument("--edge-min-count", type=int, default=2)
    ap.add_argument("--include-finished", action="store_true", help="締切済も含める")
    ap.add_argument("--no-fetch-odds", action="store_true", help="最新オッズ取得をしない (snapshot のみ)")
    ap.add_argument("--claude-all", action="store_true",
                    help="Claude 指数が無い発走前レースを全件 claude -p で一括生成")
    ap.add_argument("--claude-eval", type=int, default=0, help="上位N件に Claude 指数を新規生成 (--claude-all なしの時)")
    ap.add_argument("--claude-eval-timeout", type=int, default=900)
    ap.add_argument("--claude-eval-parallel", type=int, default=6)
    ap.add_argument("--max-races", type=int, default=None)
    args = ap.parse_args(argv)

    result = scan(
        date=args.date,
        race_type=args.race_type,
        use_separation=not args.no_separation,
        use_claude_edge=not args.no_claude,
        combine=args.combine,
        sep_threshold=args.sep_threshold,
        edge_margin=args.edge_margin,
        edge_min_count=args.edge_min_count,
        upcoming_only=not args.include_finished,
        fetch_odds=not args.no_fetch_odds,
        claude_all=args.claude_all,
        claude_eval=args.claude_eval,
        claude_eval_timeout=args.claude_eval_timeout,
        claude_eval_parallel=args.claude_eval_parallel,
        max_races=args.max_races,
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)
        print(f"[out] {out} に書き出しました ({len(result['races'])} レース)", flush=True)
    else:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
