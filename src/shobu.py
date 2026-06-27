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
import os
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
# 勝負レース スキャン結果 (<date>.json) と スコア履歴 (<date>.scores.json) の置き場。
SHOBU_DIR = ROOT / "data" / "cache" / "shobu"
PY = str(ROOT / ".venv" / "bin" / "python")
if not Path(PY).exists():
    PY = sys.executable
JST = ZoneInfo("Asia/Tokyo")
# 市場指数の温度 (analyze.MARKET_INDEX_T のミラー)。market_index = 100·(1/odds)^(1/T)。
# 最新オッズ取得時の基準B再計算で snapshot と同じ尺度の market_index を作るのに使う。
_MARKET_INDEX_T = 1.5

# ヘッダの netkeiba 場コード (01-10) は JRA。それ以外は NAR (= discovery の source でも判定)。
_JRA_VENUE_CODES = {f"{i:02d}" for i in range(1, 11)}
# 帯広ばんえいは netkeiba 場コード 65 (別競技)。確率モデルも ev.segment_of_rd で banei に分離
# されているので、勝負レース screen でも JRA/NAR/banei の 3 区分にする (平地NARと混ぜない)。
_BANEI_VENUE_CODE = "65"


def today_jst() -> str:
    """当日 (JST) を YYYYMMDD で返す。"""
    return datetime.now(JST).strftime("%Y%m%d")


def _race_type(rid: str, source: str) -> str:
    """JRA / NAR / banei の判定 (ev.segment_of_rd と同じ 3 区分)。

    まず netkeiba 場コードで帯広ばんえい (65) を分離 (別競技なので平地 NAR と混ぜない)。
    残りは discovery の source ("keibabook"=JRA / "oddspark"=NAR) 優先、無ければ場コードで判定。
    """
    code = rid[4:6] if len(rid) >= 6 else ""
    if code == _BANEI_VENUE_CODE:
        return "banei"
    s = (source or "").lower()
    if s == "keibabook":
        return "jra"
    if s in ("oddspark", "keibago"):
        return "nar"
    return "jra" if code in _JRA_VENUE_CODES else "nar"


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

def _market_index_from_odds(odds: float) -> float | None:
    """単勝オッズ → 市場指数 (0-100)。analyze._market_win_index と同式 100·(1/odds)^(1/T)。"""
    if not odds or odds <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * ((1.0 / float(odds)) ** (1.0 / _MARKET_INDEX_T)))), 1)


def _claude_edge(snap: dict[str, Any], *, value_floor: float,
                 market_override: dict[int, float] | None = None) -> dict[str, Any] | None:
    """snapshot から **市場とClaudeの順位乖離** を抽出 (単なる Claude>市場 ではなく順位の食い違い)。

    各馬を Claude 指数 / 市場指数それぞれで降順ランク付け (1=最上位) し、
      rank_gap = market_rank − claude_rank   (正 = Claude が市場より上位に評価 = 市場が過小評価)
    を見る。「市場2位なのに Claude1位」= その馬の rank_gap=1。乖離が強いほど rank_gap が大きい。

    - **乖離馬 (edge)** = rank_gap ≥ 1 かつ 指数差 (claude−market) ≥ value_floor
      (順位だけでなく指数差も伴うものに限定 = 数値の裏付けがある乖離)。
    - **top 乖離** = Claude 本命 (claude_rank=1) が市場で何番手か。top_rank_gap = market_rank−1
      (1 = 市場2番人気を Claude が本命に推している = ユーザの言う強い乖離)。
    - **score (0-100)** = top_rank_gap·20 + Σ_edge(rank_gap·5 + max(0,指数差)·0.4)。
      Claude 本命の市場順位ギャップを主軸に、乖離馬の順位差と指数差を加味 ("数値の差もいい感じに")。

    ランク比較には Claude 指数と市場指数の **両方** が要る。両方ある馬が 2 頭未満なら None。

    `market_override` ({馬番: 単勝オッズ}) を渡すと **最新オッズから market_index を再計算**して
    snapshot の (スキャン時点の) market_index を上書きする (2分毎の最新オッズ更新で基準Bも動かす)。
    Claude 指数は snapshot 由来のまま (= Claude を呼ばずに市場順位だけ最新化)。override に居ない馬
    (最新オッズ欠落) は基準Bの対象から外す。
    """
    rows: list[dict[str, Any]] = []
    ic = snap.get("index_compare")

    def _mkt(num: Any, fallback: Any) -> float | None:
        """override があれば最新オッズ由来 market_index、無ければ snapshot の値。"""
        if market_override is not None:
            try:
                od = market_override.get(int(num)) if num is not None else None
            except (TypeError, ValueError):
                od = None
            return _market_index_from_odds(od) if od else None
        return float(fallback) if fallback is not None else None

    if isinstance(ic, list) and ic:
        for r in ic:
            ci = r.get("claude_index")
            mi = _mkt(r.get("number"), r.get("market_index"))
            if ci is None or mi is None:
                continue   # 順位比較には両方必要
            rows.append({"number": r.get("number"), "name": r.get("name", ""),
                         "claude_index": float(ci), "market_index": float(mi),
                         "support": r.get("support"), "alerts": r.get("alerts") or []})
    else:
        lwi = snap.get("llm_win_index") or {}
        mwi = snap.get("market_win_index") or {}
        for k, ci in lwi.items():
            try:
                num = int(k)
            except (TypeError, ValueError):
                continue
            mi = _mkt(num, mwi.get(k))
            if mi is None:
                continue
            try:
                rows.append({"number": num, "name": "", "claude_index": float(ci),
                             "market_index": float(mi), "support": None, "alerts": []})
            except (TypeError, ValueError):
                continue
    if len(rows) < 2:
        return None
    # 降順ランク (1=最上位)。安定ソートで同値はリスト順。
    for i, r in enumerate(sorted(rows, key=lambda x: x["claude_index"], reverse=True), 1):
        r["claude_rank"] = i
    for i, r in enumerate(sorted(rows, key=lambda x: x["market_index"], reverse=True), 1):
        r["market_rank"] = i
    for r in rows:
        r["rank_gap"] = r["market_rank"] - r["claude_rank"]   # + = Claude が上位評価
        r["diff"] = round(r["claude_index"] - r["market_index"], 1)
    top = min(rows, key=lambda r: r["claude_rank"])           # Claude 本命
    top_rank_gap = top["market_rank"] - 1
    edge = [r for r in rows if r["rank_gap"] >= 1 and r["diff"] >= value_floor]
    edge.sort(key=lambda r: (r["rank_gap"], r["diff"]), reverse=True)
    score = top_rank_gap * 20 + sum(r["rank_gap"] * 5 + max(0.0, r["diff"]) * 0.4 for r in edge)
    score = round(min(100.0, max(0.0, score)), 1)
    return {
        "available": True,
        "edge_count": len(edge),
        "score": score,
        "top_pick": {"number": top["number"], "name": top["name"],
                     "market_rank": top["market_rank"]},
        "top_rank_gap": top_rank_gap,
        "max_rank_gap": max((r["rank_gap"] for r in rows), default=0),
        "max_diff": round(max((r["diff"] for r in rows), default=0.0), 1),
        "edge_horses": [
            {"number": r["number"], "name": r["name"],
             "claude_index": r["claude_index"], "market_index": r["market_index"],
             "claude_rank": r["claude_rank"], "market_rank": r["market_rank"],
             "rank_gap": r["rank_gap"], "diff": r["diff"],
             "support": r["support"], "alerts": r["alerts"]}
            for r in edge[:6]
        ],
        "scored_at": snap.get("llm_scored_at"),
    }


# ---------------------------------------------------------------- 採点 --------

def _evaluate_race(
    r: dict[str, Any],
    *,
    use_separation: bool,
    use_claude_edge: bool,
    combine: str,
    sep_threshold: float,
    edge_margin: float,
    edge_threshold: float,
    claude_use_fresh_market: bool = False,
) -> dict[str, Any]:
    """1 レースを 2 基準 (強弱 / 市場乖離) で採点して結果 dict を返す。

    `r` は `_snap` (snapshot) / `_fresh` ({"odds","names"}) を持つレースメタ。scan() と
    refresh_recommended() の双方が使う共通採点。`claude_use_fresh_market=True` のとき、最新
    オッズ (`_fresh`) があれば基準B (市場乖離) の market_index も最新オッズから再計算する。
    """
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
    # --- Claude (B): 市場との順位乖離 (最新オッズがあれば market 側を再計算) ---
    market_override = fresh["odds"] if (claude_use_fresh_market and fresh) else None
    claude = (_claude_edge(snap, value_floor=edge_margin, market_override=market_override)
              if snap else None)
    # --- データ源 ---
    data_source = "fresh" if fresh else ("snapshot" if snap else "none")
    n_runners = (snap.get("n_runners") if snap else None) or (sep.get("n") if sep else None)
    # --- 判定 ---
    sep_avail = sep is not None
    claude_avail = claude is not None
    sep_pass = use_separation and sep_avail and sep["score"] >= sep_threshold
    claude_pass = (use_claude_edge and claude_avail
                   and claude["score"] >= edge_threshold)
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
    if claude_avail and (claude["top_rank_gap"] >= 1 or claude["edge_count"] > 0):
        parts = []
        tp = claude.get("top_pick")
        if claude["top_rank_gap"] >= 1 and tp:
            parts.append(f"Claude本命 {tp['number']}番=市場{tp['market_rank']}番人気")
        if claude["edge_count"] > 0:
            parts.append(f"乖離馬 {claude['edge_count']}頭")
        reasons.append("市場乖離: " + " / ".join(parts) + f" (score {claude['score']:.0f})")
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


def _build_summary(results: list[dict[str, Any]], total_discovered: int) -> dict[str, Any]:
    """results から summary を組む (scan / refresh 共通)。"""
    sep_scores = [r["separation"]["score"] for r in results if r.get("separation")]
    sep_median = round(sorted(sep_scores)[len(sep_scores) // 2], 1) if sep_scores else None
    return {
        "total_discovered": total_discovered,
        "evaluated": len(results),
        "recommended": sum(1 for r in results if r.get("recommended")),
        "with_snapshot": sum(1 for r in results if r.get("has_snapshot")),
        "with_claude": sum(1 for r in results if r.get("claude")),
        "with_fresh_odds": sum(1 for r in results if r.get("data_source") == "fresh"),
        "sep_median": sep_median,
        "by_type": {
            "jra": sum(1 for r in results if r.get("race_type") == "jra"),
            "nar": sum(1 for r in results if r.get("race_type") == "nar"),
            "banei": sum(1 for r in results if r.get("race_type") == "banei"),
        },
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
                     parallel: int, log: Callable[[str], None],
                     score_parallel: bool = False,
                     score_queries_per_horse: int | None = None,
                     llm_max_concurrent: int | None = None,
                     on_progress: Callable[[dict[str, Any], int, int], None] | None = None) -> int:
    """対象レースに score ステージ (claude -p) を spawn して Claude 指数を生成。生成成功数を返す。

    並列は ThreadPoolExecutor で回すが、実際の claude -p 同時数は src 側の file-slot semaphore
    (KEIBA_LLM_MAX_CONCURRENT, 既定5) で頭打ちになる (fail-open)。各 race の subprocess 出力は
    capture して捨て、進捗だけ 1 行ずつ log に流す (scan のログを汚さない)。

    `on_progress(target, completed, total)` を渡すと、1 レース生成が終わるたびに呼ぶ (呼び出し
    スレッドの as_completed ループ内 = シリアル)。scan が「生成完了レースを再採点して暫定一覧を
    live 更新する」のに使う。例外は握り潰して生成を止めない。
    """
    if not targets:
        return 0
    total = len(targets)

    score_env = os.environ.copy()
    # score subprocess の検索並列化 (KEIBA_SCORE_PARALLEL=1 で ARCH-A プロセス並列)。
    # OFF のときは継承した "1" を空文字で打ち消す (main.py の score タブと同流儀)。
    score_env["KEIBA_SCORE_PARALLEL"] = "1" if score_parallel else ""
    if score_queries_per_horse:
        # 1馬あたり検索クエリ数。並列パスは「頭数 × これ」を全シャードで被覆し、
        # 単一セッション (< 並列しきい値の小頭数) も llm.score_horses_stream が同 env を読むので
        # どの頭数でも「頭数 × これ」クエリが流れる (ユーザ指示: 10/頭)。
        score_env["KEIBA_SCORE_QUERIES_PER_HORSE"] = str(score_queries_per_horse)
    if llm_max_concurrent:
        # file-slot semaphore (claude -p 同時数上限)。並列を実際に通すため引き上げる。
        score_env["KEIBA_LLM_MAX_CONCURRENT"] = str(llm_max_concurrent)

    shards_per_race = None
    if score_parallel:
        # --- 並列を飽和させて実行を速める (ユーザ指示: 並列化して速める) ---
        # across-race は `parallel` 本同時に走る。各レースの RESEARCH シャードを小さく刻み
        # (per_shard=3)、シャード数を「claude 同時上限 ÷ across-race」に合わせると、全体の
        # claude -p 同時実行が上限近くまで埋まり、各シャードの担当頭数が減って research が速い。
        # _shard_numbers は全馬を必ず被覆するので「頭数 × クエリ/頭」(= 頭数×N) は不変。
        # keiba.go.jp レート制限は SCRAPE (across-race=parallel) 側の制約で、claude -p (Tavily)
        # は別系統なのでシャードを増やしても scrape は bursting しない。
        try:
            conc = int(llm_max_concurrent or score_env.get("KEIBA_LLM_MAX_CONCURRENT") or 20)
        except (TypeError, ValueError):
            conc = 20
        shards_per_race = max(2, conc // max(1, parallel))
        # 既に operator が明示設定していれば尊重 (= 空のときだけ shobu が飽和値を入れる)。
        if not (score_env.get("KEIBA_SCORE_HORSES_PER_SHARD") or "").strip():
            score_env["KEIBA_SCORE_HORSES_PER_SHARD"] = "3"
        if not (score_env.get("KEIBA_SCORE_MAX_SHARDS") or "").strip():
            score_env["KEIBA_SCORE_MAX_SHARDS"] = str(shards_per_race)
        # 内側 claude の score timeout を外側 subprocess kill (timeout) の手前に収める。
        # 外側が先に発火すると score 結果が丸ごと失われる (TimeoutExpired → rc≠0 で指数なし) ため、
        # ~90s の余裕 (scrape + 起動 + JSON finalize + snapshot 保存) を残す。operator 設定値は
        # その上限でクランプ (外側 kill を越えさせない)。**必ず外側 < timeout** を保証するため、
        # 90s 余裕の希望値を timeout-10 でも上限クランプする (timeout が小さくても反転しない)。
        # 通常運用 (timeout=900) は 810s。CLI も --claude-eval-timeout を 210s 下限にクランプ済。
        headroom = min(max(120, int(timeout) - 90), int(timeout) - 10)
        prior = (score_env.get("KEIBA_SCORE_TIMEOUT") or "").strip()
        try:
            prior_v = int(prior)
        except ValueError:
            prior_v = 0
        score_env["KEIBA_SCORE_TIMEOUT"] = str(min(prior_v, headroom) if prior_v > 0 else headroom)

    qph_txt = f" / {score_queries_per_horse}クエリ/頭" if score_queries_per_horse else ""
    shard_txt = (f" / {shards_per_race}シャード/レース (per_shard 3)"
                 if shards_per_race else "")
    log(f"[claude-eval] {total} レースの Claude 指数を一括生成中 "
        f"(across-race 並列 {parallel}{qph_txt}{shard_txt} / 各 timeout {timeout}s)…")

    def _one(t: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
        rid = t["netkeiba_race_id"]
        cmd = _score_stage_cmd(rid, t["race_type"], int(t.get("start_at") or 0))
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), env=score_env, capture_output=True,
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
            if on_progress is not None:
                try:
                    on_progress(t, completed, total)
                except Exception:  # noqa: BLE001  進捗更新の失敗で生成を止めない
                    pass
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
    edge_margin: float = 3.0,
    edge_threshold: float = 25.0,
    upcoming_only: bool = True,
    fetch_odds: bool = True,
    claude_all: bool = False,
    claude_eval: int = 0,
    claude_eval_timeout: int = 900,
    claude_eval_parallel: int = 6,
    score_parallel: bool = False,
    score_queries_per_horse: int | None = None,
    llm_max_concurrent: int | None = None,
    max_races: int | None = None,
    fetch_parallel: int = 6,
    out: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """当日全レースを採点して勝負レースを抽出した dict を返す。

    `out` を渡すと結果 JSON を **2 段階で書き出す**: ①Claude 指数の一括生成がある時は生成前に
    暫定一覧 (基準A中心・`generating=True`) を先出し ②各レース生成完了ごとに再採点して live 更新
    ③全生成後に最終版 (`generating=False`) を書き出す。これでフロントは生成完了を待たず暫定一覧を
    表示でき、各レースの Claude 指数が付き次第 基準B (市場乖離) が確定する (ユーザ指示 2026-06-22)。
    `out=None` (CLI stdout / テスト) のときは書き出さず最終 dict を返すだけ。
    """
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
        if race_type in ("jra", "nar", "banei") and rtype != race_type:
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

    # 各レースを採点 (scan / refresh 共通の _evaluate_race を使う)。
    eval_kwargs = dict(
        use_separation=use_separation, use_claude_edge=use_claude_edge, combine=combine,
        sep_threshold=sep_threshold, edge_margin=edge_margin, edge_threshold=edge_threshold,
    )
    results = [_evaluate_race(r, **eval_kwargs) for r in races]
    by_id = {r["race_id"]: r for r in races}   # race_id → メタ (再採点に使う)
    options = {
        "date": date, "race_type": race_type,
        "use_separation": use_separation, "use_claude_edge": use_claude_edge,
        "combine": combine, "sep_threshold": sep_threshold,
        "edge_margin": edge_margin, "edge_threshold": edge_threshold,
        "upcoming_only": upcoming_only, "fetch_odds": fetch_odds,
        "claude_all": claude_all, "claude_eval": claude_eval,
    }

    def _emit(*, generating: bool, gen_total: int, gen_done: int) -> dict[str, Any]:
        """results を推奨優先 + score 降順に並べた結果 doc を作り、out があれば atomic 書き出し。"""
        results.sort(key=lambda r: (r["recommended"], r["shobu_score"]), reverse=True)
        doc = {
            "date": date,
            "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
            "options": options,
            "summary": _build_summary(results, len(discovered)),
            "races": results,
            # 生成進捗 — 基準B (市場乖離) は各レースの Claude 指数生成後に確定する。
            # generating=True の間は「暫定 (基準A中心)」、False で「確定 (基準B反映済)」。
            "generating": generating,
            "gen_total": gen_total,
            "gen_done": gen_done,
        }
        if out is not None:
            _atomic_write_json(out, doc)
        return doc

    def _rescore(race_id: str) -> None:
        """race_id のレースを snapshot 再読込して再採点し results 内を差し替える。"""
        base = by_id.get(race_id)
        if base is None:
            return
        base["_snap"] = _load_snapshot(race_id)
        new_res = _evaluate_race(base, **eval_kwargs)
        for i, r in enumerate(results):
            if r["race_id"] == race_id:
                results[i] = new_res
                return
        results.append(new_res)

    # --- Claude 指数の生成 (ボタンで一括取得 / 上位N件) ---
    # claude_all=True: Claude 指数が無い発走前レースを **全件** 生成 (ボタン押下で一気に取得)。
    # claude_all=False & claude_eval>0: 強弱上位 claude_eval 件のみ。既にスコア済は二重生成しない。
    targets: list[dict[str, Any]] = []
    if use_claude_edge and (claude_all or claude_eval > 0):
        targets = _select_claude_targets(
            results, claude_all=claude_all, claude_eval=claude_eval,
            upcoming_only=upcoming_only, now=now)
    gen_total = len(targets)

    if gen_total > 0:
        # ① 暫定一覧 (基準A中心) を生成前に先出し。フロントは生成完了を待たず表示できる。
        _log(f"[provisional] 暫定一覧 (基準A中心) を先出し → Claude 指数 {gen_total} 件を生成中…")
        _emit(generating=True, gen_total=gen_total, gen_done=0)

        # ② 1 レース生成完了ごとに再採点して live 更新 (as_completed は呼び出しスレッド=シリアル)。
        def _on_progress(t: dict[str, Any], completed: int, _total: int) -> None:
            _rescore(t["race_id"])
            _emit(generating=True, gen_total=gen_total, gen_done=completed)

        _run_claude_eval(targets, timeout=claude_eval_timeout,
                         parallel=claude_eval_parallel, log=_log,
                         score_parallel=score_parallel,
                         score_queries_per_horse=score_queries_per_horse,
                         llm_max_concurrent=llm_max_concurrent,
                         on_progress=_on_progress)

        # ③ 権威的な最終再評価 — on_progress が呼ばれなくても (テストの monkeypatch 等) 確定させる。
        for t in targets:
            _rescore(t["race_id"])
    elif use_claude_edge and (claude_all or claude_eval > 0):
        _log("[claude-eval] 生成対象なし (全レース既に Claude 指数あり/締切済)")

    final = _emit(generating=False, gen_total=gen_total, gen_done=gen_total)
    s = final["summary"]
    _log(f"[done] 推奨 {s['recommended']} / 評価 {s['evaluated']} "
         f"(snapshot {s['with_snapshot']} / Claude {s['with_claude']})")
    return final


# ---------------------------------------------------- refresh (2分毎・推奨のみ) --

_SCORE_HISTORY_MAX = 40


def _scores_path(date: str) -> Path:
    return SHOBU_DIR / f"{date}.scores.json"


def _load_scores(date: str) -> dict[str, list[list[float]]]:
    p = _scores_path(date)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _iso_to_unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return None


def refresh_recommended(date: str | None = None, *, parallel: int = 8,
                        log: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """既存スキャン結果の **推奨 (勝負レース) のみ** を最新オッズで再採点する (Claude は呼ばない)。

    勝負レースページを開いている間 2 分毎に呼ばれる軽量更新。各推奨レースの単勝を 1 回 fetch し、
    強弱 (基準A) と 市場乖離 (基準B = market_index を最新オッズで再計算) を recompute → 勝負スコアを
    更新する。スコア履歴 (data/cache/shobu/<date>.scores.json) に追記し、前回比 (score_delta) と
    履歴 (score_history) をレースに付ける。結果 JSON を上書きして返す。結果ファイルが無ければ None。

    discovery も Claude -p も呼ばない (= netkeiba 規制リスク無し・即時)。推奨外レースは据え置き。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    date = date or today_jst()
    rp = SHOBU_DIR / f"{date}.json"
    if not rp.exists():
        return None
    try:
        doc = json.loads(rp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    races = doc.get("races") or []
    opts = doc.get("options") or {}
    eval_kwargs = dict(
        use_separation=bool(opts.get("use_separation", True)),
        use_claude_edge=bool(opts.get("use_claude_edge", True)),
        combine=str(opts.get("combine", "or")),
        sep_threshold=float(opts.get("sep_threshold", 35.0)),
        edge_margin=float(opts.get("edge_margin", 3.0)),
        edge_threshold=float(opts.get("edge_threshold", 25.0)),
    )
    rec_idx = [i for i, r in enumerate(races) if r.get("recommended")]
    now = int(time.time())
    if rec_idx:
        # 推奨レースの単勝を並列 fetch (1 レース 1 リクエスト)。
        fresh_by_i: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
            futs = {ex.submit(_fetch_fresh_win, races[i]["netkeiba_race_id"],
                              races[i]["race_type"]): i for i in rec_idx}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    res = fut.result()
                except Exception:  # noqa: BLE001
                    res = None
                if res:
                    fresh_by_i[i] = res
        _log(f"[refresh] 最新オッズ {len(fresh_by_i)}/{len(rec_idx)} 取得")

        scores = _load_scores(date)
        gen_unix = _iso_to_unix(doc.get("generated_at"))
        for i in rec_idx:
            fresh = fresh_by_i.get(i)
            if fresh is None:
                continue   # 取得失敗は据え置き (履歴も増やさない)
            old = races[i]
            meta = {**old, "_snap": _load_snapshot(old["race_id"]), "_fresh": fresh}
            new = _evaluate_race(meta, claude_use_fresh_market=True, **eval_kwargs)
            rid = old["race_id"]
            prev_score = round(float(old.get("shobu_score") or 0.0), 1)
            hist = scores.get(rid) or []
            if not hist:
                hist = [[gen_unix or now, prev_score]]   # スキャン時点をシード
            new_score = round(float(new.get("shobu_score") or 0.0), 1)
            hist.append([now, new_score])
            hist = hist[-_SCORE_HISTORY_MAX:]
            scores[rid] = hist
            new["score_prev"] = hist[-2][1]
            new["score_delta"] = round(new_score - hist[-2][1], 1)
            new["score_history"] = [{"at": int(a), "score": s} for a, s in hist]
            new["refreshed_at"] = now
            races[i] = new
        _atomic_write_json(_scores_path(date), scores)

    # 並べ替え (推奨を上, shobu_score 降順) + summary 再計算 + refreshed_at。
    races.sort(key=lambda r: (bool(r.get("recommended")), r.get("shobu_score") or 0.0), reverse=True)
    doc["races"] = races
    doc["summary"] = _build_summary(
        races, (doc.get("summary") or {}).get("total_discovered", len(races)))
    doc["refreshed_at"] = datetime.now(JST).isoformat(timespec="seconds")
    # refresh は scan 完了後にしか走らない (フロントは生成中は refresh を止める) ので、
    # 念のため生成フラグを下ろして「確定」状態を維持する (基準B は既に確定済)。
    doc["generating"] = False
    _atomic_write_json(rp, doc)
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="今日の勝負レース スキャン")
    ap.add_argument("--out", help="結果 JSON の出力先 (省略時は stdout に最後の1行で出す)")
    ap.add_argument("--date", default=None, help="YYYYMMDD (省略時は当日 JST)")
    ap.add_argument("--race-type", choices=["all", "jra", "nar", "banei"], default="all")
    ap.add_argument("--no-separation", action="store_true", help="基準A (強弱) を無効化")
    ap.add_argument("--no-claude", action="store_true", help="基準B (Claude>市場) を無効化")
    ap.add_argument("--combine", choices=["or", "and"], default="or")
    ap.add_argument("--sep-threshold", type=float, default=35.0)
    ap.add_argument("--edge-margin", type=float, default=3.0,
                    help="乖離馬の指数差フロア (claude−market ≥ これ)")
    ap.add_argument("--edge-threshold", type=float, default=25.0,
                    help="市場乖離スコアしきい値 (これ以上で基準B合格)")
    ap.add_argument("--include-finished", action="store_true", help="締切済も含める")
    ap.add_argument("--no-fetch-odds", action="store_true", help="最新オッズ取得をしない (snapshot のみ)")
    ap.add_argument("--claude-all", action="store_true",
                    help="Claude 指数が無い発走前レースを全件 claude -p で一括生成")
    ap.add_argument("--claude-eval", type=int, default=0, help="上位N件に Claude 指数を新規生成 (--claude-all なしの時)")
    ap.add_argument("--claude-eval-timeout", type=int, default=900,
                    help="各レースの score subprocess の外側 timeout 秒 (既定 900)。下限 210s に"
                         "クランプ — 内側 claude (research 60s + scoring 60s floor + scrape) が"
                         "外側 kill より先に終わるのを保証する")
    ap.add_argument("--claude-eval-parallel", type=int, default=6)
    ap.add_argument("--score-parallel", dest="score_parallel", action="store_true",
                    default=True, help="score 段の検索を並列化 (KEIBA_SCORE_PARALLEL=1, 既定 ON)")
    ap.add_argument("--no-score-parallel", dest="score_parallel", action="store_false",
                    help="score 段の検索並列化を無効化 (単一セッション)")
    ap.add_argument("--score-queries-per-horse", type=int, default=10,
                    help="1馬あたり検索クエリ数 (KEIBA_SCORE_QUERIES_PER_HORSE)。"
                         "頭数×これ クエリが流れる (既定 10)")
    ap.add_argument("--llm-max-concurrent", type=int, default=20, help="claude -p 同時数上限 (KEIBA_LLM_MAX_CONCURRENT, 既定 20)")
    ap.add_argument("--max-races", type=int, default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="既存スキャン結果の推奨レースのみ最新オッズで再採点 (Claude 呼ばない)")
    args = ap.parse_args(argv)
    # 外側 subprocess timeout が小さすぎると内側 claude の score timeout より先に発火し
    # score 結果が丸ごと失われる (内側は research 60s + scoring 60s + scrape の floor を持つ)。
    # 210s 下限でクランプして反転を防ぐ (_run_claude_eval の headroom も二重防御済)。
    if args.claude_eval_timeout < 210:
        print(f"[warn] --claude-eval-timeout={args.claude_eval_timeout} は小さすぎるため 210s に"
              "クランプ (内側 claude が外側 kill より先に終わるのを保証)", flush=True)
        args.claude_eval_timeout = 210

    if args.refresh:
        result = refresh_recommended(date=args.date)
        if result is None:
            print("[refresh] スキャン結果が見つかりません (先に scan を実行してください)", flush=True)
            return 1
        print(f"[refresh] {result['date']} 推奨 {result['summary']['recommended']} 件を更新 "
              f"({result.get('refreshed_at')})", flush=True)
        return 0

    out = Path(args.out) if args.out else None
    result = scan(
        date=args.date,
        race_type=args.race_type,
        use_separation=not args.no_separation,
        use_claude_edge=not args.no_claude,
        combine=args.combine,
        sep_threshold=args.sep_threshold,
        edge_margin=args.edge_margin,
        edge_threshold=args.edge_threshold,
        upcoming_only=not args.include_finished,
        fetch_odds=not args.no_fetch_odds,
        claude_all=args.claude_all,
        claude_eval=args.claude_eval,
        claude_eval_timeout=args.claude_eval_timeout,
        claude_eval_parallel=args.claude_eval_parallel,
        score_parallel=args.score_parallel,
        score_queries_per_horse=args.score_queries_per_horse,
        llm_max_concurrent=args.llm_max_concurrent,
        max_races=args.max_races,
        out=out,   # scan が暫定→確定の 2 段階で書き出す (生成完了ごとに live 更新)
    )
    if out:
        # 書き出しは scan が担う (暫定/進捗/確定)。ここでは最終状態の確認だけ出す。
        print(f"[out] {out} に書き出しました ({len(result['races'])} レース)", flush=True)
    else:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
