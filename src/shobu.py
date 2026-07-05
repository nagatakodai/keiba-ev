"""今日の勝負レース スキャン。

`discover_today_races` で当日の全レースを **netkeiba 非依存** で列挙し、各レースを
**基準B (市場との順位乖離) 単独** で採点して「勝負レース (= 通常より賭ける価値が高いと
思われるレース)」を抽出する (ユーザ指示 2026-06-28: 旧基準A=強弱/separation は廃止):

 **市場との順位乖離** — 既存 snapshot (watch-auto / 手動 score) の index_compare で、各馬を
   Claude 指数 / 市場指数それぞれで降順ランク付けし、その食い違い (Claude 本命が市場で何番人気か・
   Claude が市場より上位評価する馬の有無) をスコア化。市場が過小評価しているレースを抽出する。

しきい値 (edge_threshold / edge_margin)・JRA/NAR/banei・発走前のみ・Claude 指数の新規生成
(全件 or 上位N件) は option (CLI / Web UI から指定)。結果は JSON で `--out` に書き出す。

ユーザ指示 (2026-06-20): 基準は **既存スナップショット中心** (無料・即時)。新規 claude -p 生成は
ボタン押下で全件 (claude_all) もしくは `--claude-eval N` で上位 N 件の score ステージを spawn する。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
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
    edge_margin: float,
    edge_threshold: float,
    claude_use_fresh_market: bool = False,
) -> dict[str, Any]:
    """1 レースを **基準B (市場との順位乖離) 単独** で採点して結果 dict を返す。

    ユーザ指示 (2026-06-28): 基準A=強弱 (separation) は廃止し、勝負レース判定・勝負スコアとも
    基準B のみで決める。`r` は `_snap` (snapshot) / `_fresh` ({"odds","names"}) を持つレースメタ。
    `claude_use_fresh_market=True` のとき、最新オッズ (`_fresh`) があれば市場 index を再計算する。
    """
    snap = r.get("_snap")
    fresh = r.get("_fresh")
    # 基準B: 市場との順位乖離 (最新オッズがあれば market 側を再計算)。
    market_override = fresh["odds"] if (claude_use_fresh_market and fresh) else None
    claude = (_claude_edge(snap, value_floor=edge_margin, market_override=market_override)
              if snap else None)
    data_source = "fresh" if fresh else ("snapshot" if snap else "none")
    n_runners = snap.get("n_runners") if snap else None
    claude_avail = claude is not None
    recommended = claude_avail and claude["score"] >= edge_threshold
    matched = ["claude"] if recommended else []
    shobu_score = round(min(100.0, claude["score"]), 1) if claude_avail else 0.0
    reasons: list[str] = []
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
        "has_snapshot": snap is not None,
        "snapshot_stage": (snap.get("stage") if snap else None),
        "claude": claude,
        "recommended": recommended,
        "matched": matched,
        "shobu_score": shobu_score,
        "reasons": reasons,
    }


def _build_summary(results: list[dict[str, Any]], total_discovered: int) -> dict[str, Any]:
    """results から summary を組む (scan / refresh 共通)。"""
    return {
        "total_discovered": total_discovered,
        "evaluated": len(results),
        "recommended": sum(1 for r in results if r.get("recommended")),
        "with_snapshot": sum(1 for r in results if r.get("has_snapshot")),
        "with_claude": sum(1 for r in results if r.get("claude")),
        "with_fresh_odds": sum(1 for r in results if r.get("data_source") == "fresh"),
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

def _score_stage_cmd(rid: str, rtype: str, start_at: int, model: str = "opus") -> list[str]:
    """score ステージ (Claude 指数生成 + 暫定 snapshot) の subprocess コマンド。

    api/main.py の refresh-odds と同経路: JRA=jra / **それ以外 (nar + banei) = keibago** に
    `--phase=score --snapshot`。旧実装は `nar なら keibago、else jra` で **banei (帯広) が
    JRA 経路に送られ全レース rc=1 で指数生成不能**だった (実障害: 翌日スキャンで帯広12R が
    2回リトライ後 全見送り, 2026-07-05 修正)。keiba.go.jp は帯広 (babaCode=3) を扱える。
    `model` は claude -p のモデル (opus/sonnet/haiku) — モデルで指数の質・速度・コストが変わるか
    比較するため選べるようにした (ユーザ指示 2026-07-05)。
    """
    mod = "src.scrape_jra" if rtype == "jra" else "src.scrape_keibago"
    return [PY, "-m", mod, rid, "--snapshot", "--phase=score", f"--start-at={start_at}",
            f"--model={model}"]


def _select_claude_targets(results: list[dict[str, Any]], *, claude_all: bool,
                           claude_eval: int, upcoming_only: bool,
                           now: int) -> list[dict[str, Any]]:
    """Claude 指数を生成すべきレースを選ぶ。

    対象は「Claude 指数がまだ無い (= snapshot に未スコア) + (発走前のみなら) 締切前」のレース。
    claude_all=True なら **全件** (ボタンで一気に取得)、False かつ claude_eval>0 なら発走が近い順に
    claude_eval 件だけ。既にスコア済 (snapshot に Claude 指数あり) のレースは二重生成しない。
    """
    if not (claude_all or claude_eval > 0):
        return []
    cand = [
        r for r in results
        if r["claude"] is None
        and (not upcoming_only or (r["close_at"] and r["close_at"] > now))
    ]
    # 発走時刻の昇順 (近い順)。上位 N モードでは発走が近いレースを優先生成する
    # (基準A=強弱を廃止したため、生成前に使える優先度は発走時刻のみ)。
    cand.sort(key=lambda r: (r.get("start_at") or 0))
    return cand if claude_all else cand[:claude_eval]


def _run_claude_eval(targets: list[dict[str, Any]], *, timeout: int,
                     parallel: int, log: Callable[[str], None],
                     score_parallel: bool = False,
                     score_queries_per_horse: int | None = None,
                     llm_max_concurrent: int | None = None,
                     max_retries: int = 2, model: str = "opus",
                     research: str = "agentic",
                     on_progress: Callable[[dict[str, Any], int, int], None] | None = None) -> int:
    """対象レースに score ステージ (claude -p) を spawn して Claude 指数を生成。生成成功数を返す。

    並列は ThreadPoolExecutor で回すが、実際の claude -p 同時数は src 側の file-slot semaphore
    (KEIBA_LLM_MAX_CONCURRENT) で頭打ちになる (fail-open)。各 race の subprocess 出力は
    Popen で 1 行ずつ読み、**検索クエリ行 (🔍) は scan ログへレース名付きで転送** (ユーザ指示
    「クエリもログに出す」)、それ以外の冗長な出力は捨てる。進捗も 1 行ずつ log に流す。
    worker スレッドと as_completed ループの双方から log を呼ぶので Lock で write を直列化する。

    **成功判定は rc ではなく「Claude 指数が実際に付いたか」(2026-06-29 修正)**: 同時実行の
    輻輳で claude -p / Tavily が早期に死ぬと、scrape は成功したまま llm_fallback=True (指数なし)
    で **subprocess は rc=0 で正常終了**する (実機: 盛岡8-11R が rc=0 のまま指数なし、12R のみ生成)。
    rc ゲートでは拾えないので snapshot の claude_index / .llm.json を見て判定し、付かなかった
    レースを **並列度を段階的に下げて (最終は直列) 最大 max_retries 回リトライ**する。直列リトライは
    「最後に1レースだけ走って成功した」実機挙動と同条件に収束させるので確実に埋まる。

    `on_progress(target, succeeded, total)` を渡すと、1 レース subprocess が終わるたびに呼ぶ
    (呼び出しスレッドの as_completed ループ内 = シリアル)。scan が「生成完了レースを再採点して
    暫定一覧を live 更新する」のに使う。例外は握り潰して生成を止めない。
    """
    if not targets:
        return 0
    total = len(targets)

    score_env = os.environ.copy()
    # 子 (scrape_keibago/jra) の stdout を unbuffered に = 検索クエリ行 (🔍) が即 flush され
    # ライブで scan ログへ流れる (pipe 既定の block buffering だと終了まで溜まる)。
    score_env["PYTHONUNBUFFERED"] = "1"
    # score subprocess の検索並列化 (KEIBA_SCORE_PARALLEL=1 で ARCH-A プロセス並列)。
    # OFF のときは継承した "1" を空文字で打ち消す (main.py の score タブと同流儀)。
    score_env["KEIBA_SCORE_PARALLEL"] = "1" if score_parallel else ""
    # リサーチ方式 (ARCH-B, 2026-07-05): "prefetch" = 固定クエリ Tavily 直叩き + 採点 claude 1回。
    # agentic のときは空文字で継承値を打ち消す (score_parallel と同流儀)。
    score_env["KEIBA_SCORE_RESEARCH"] = "prefetch" if research == "prefetch" else ""
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
    # worker スレッド (クエリ転送) と main スレッド (進捗) の log write を直列化。
    _log_lock = threading.Lock()

    def _safe_log(msg: str) -> None:
        with _log_lock:
            log(msg)

    _safe_log(f"[claude-eval] {total} レースの Claude 指数を一括生成中 (model={model}"
              f" / across-race 並列 {parallel}{qph_txt}{shard_txt} / 各 timeout {timeout}s)…")

    def _killpg(proc) -> None:
        """score subprocess を **プロセスグループごと** SIGKILL (research の claude -p 孫まで道連れ)。
        start_new_session=True で起動しているので pgid==pid。失敗時は単独 kill にフォールバック。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _one(t: dict[str, Any], env: dict[str, str]) -> tuple[dict[str, Any], bool, str]:
        rid = t["netkeiba_race_id"]
        label = f"{t['venue']}{t['race_no']}R"
        start_at = int(t.get("start_at") or 0)

        def _exec(cmd: list[str]) -> tuple[bool, str, bool]:
            """score subprocess を 1 回実行 → (ok, note, timed_out)。"""
            # stderr は一時ファイルへ (rc≠0 = keiba.go.jp レート制限でオッズ空 等 の原因を末尾から拾う)。
            err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=err_f,
                    text=True, bufsize=1,
                    start_new_session=True,   # 孫 (claude -p) ごと timeout で kill できるよう別 pgid に
                )
            except Exception as e:  # noqa: BLE001
                err_f.close()
                return (False, str(e), False)
            timed_out = [False]

            def _kill() -> None:
                timed_out[0] = True
                _killpg(proc)

            timer = threading.Timer(timeout, _kill)
            timer.daemon = True
            timer.start()
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    # 内側 score subprocess (analyze._run_score_stage) は検索クエリを
                    # "  🔍 <tool>: <query>" 行で出す。その行だけレース名付きで scan ログへ転送し、
                    # 他の冗長な LLM 出力は読み捨てる (pipe を drain しデッドロックも防ぐ)。
                    if "🔍" in raw:
                        q = raw.split("🔍", 1)[1].strip()
                        if q:
                            _safe_log(f"[query] {label} 🔍 {q}")
            except Exception:  # noqa: BLE001
                pass  # ログ転送の失敗で生成判定 (rc) を壊さない
            finally:
                timer.cancel()
                try:
                    proc.wait(timeout=15)
                except Exception:  # noqa: BLE001
                    _killpg(proc)
                    try:
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
            rc = proc.returncode
            # rc==0 (内側が JSON を書き終え正常終了) を最優先で成功扱い: timeout 丁度の境界で _kill が
            # timed_out を立てても、既に終わっていた成功を誤って失敗にしない。
            if rc == 0:
                ok, note = True, "OK"
            elif timed_out[0]:
                ok, note = False, f"timeout ({timeout}s)"
            else:
                ok, note = False, f"rc={rc}"
                try:   # 非 timeout の失敗のみ stderr 末尾を診断ログへ
                    err_f.seek(0, 2)
                    size = err_f.tell()
                    err_f.seek(max(0, size - 500))
                    tail = err_f.read().strip().replace("\n", " ")
                    if tail:
                        _safe_log(f"[claude-eval] {label} rc={rc} stderr: …{tail[-300:]}")
                except Exception:  # noqa: BLE001
                    pass
            try:
                err_f.close()
            except Exception:  # noqa: BLE001
                pass
            return (ok, note, timed_out[0])

        ok, note, was_timeout = _exec(
            _score_stage_cmd(rid, t["race_type"], start_at, model=model))
        # 地方 (nar/banei) で keibago が rc≠0 で失敗 (timeout 以外) したら oddspark 経路に
        # 1 回だけフォールバック (2026-07-05): 前夜の翌日スキャンでは keiba.go.jp に翌日カードが
        # 未掲載の場 (実測: 盛岡) があり、oddspark 前売りで score 段が成立することがある。
        # oddspark は帯広 (banei) も売るので banei も対象。timeout は時間切れなので追い打ち
        # しない (時間予算を倍にしない)。
        if not ok and not was_timeout and t["race_type"] in ("nar", "banei"):
            _safe_log(f"[claude-eval] {label} keibago 失敗 ({note}) → oddspark にフォールバック")
            ok2, note2, _to2 = _exec(
                [PY, "-m", "src.scrape_oddspark", rid, "--snapshot", "--phase=score",
                 f"--start-at={start_at}", f"--model={model}"])
            if ok2:
                ok, note = True, "OK (oddspark)"
            else:
                note = f"{note} / oddspark: {note2}"
        return (t, ok, note)

    def _internal(t: dict[str, Any]) -> str:
        return t.get("race_id") or _internal_id(t["netkeiba_race_id"])

    def _has_index(t: dict[str, Any]) -> bool:
        """このレースに **Claude 指数が実際に付いたか** を snapshot / .llm.json から判定。

        rc=0 でも llm_fallback (指数なし) で終わるのが実害なので、rc ではなくこれで成否を見る。
        snapshot の index_compare に claude_index があるか、無ければ .llm.json に scores があるか。
        """
        internal = _internal(t)
        snap = _load_snapshot(internal)
        if snap:
            ic = snap.get("index_compare") or []
            if any(h.get("claude_index") is not None for h in ic):
                return True
            if snap.get("llm_fallback") is False:
                return True
        p = PRED_DIR / f"{internal}.llm.json"
        if p.exists():
            try:
                return bool(json.loads(p.read_text(encoding="utf-8")).get("scores"))
            except (OSError, json.JSONDecodeError):
                return False
        return False

    # リトライ時は across-race 並列を下げるだけでなく **各 subprocess の env も軽く**する
    # (2026-07-04 修正)。失敗の主因は 10クエリ/頭 × 並列 (最大 llm_max_concurrent 本の
    # claude -p 同時起動) が Tavily/Anthropic API を叩きすぎてスロットリングされ、1 セッションの
    # リサーチ+採点が内側 timeout 内に終わらず fallback になること (実測: load 1.3/24 なのに
    # 24分で指数0 = CPU でなく検索の同時実行過多)。across-race だけ下げても各 subprocess は
    # 10クエリ/頭 × ARCH-A 並列のまま重いので、リトライでは **単一セッション + 低クエリ +
    # 低同時数 + 内側 timeout クランプ** の既知の成功条件へ落として自己回復させる。
    _retry_timeout = str(min(max(120, int(timeout) - 90), int(timeout) - 10))

    def _pass_env(attempt: int) -> dict[str, str]:
        if attempt == 0:
            return score_env                                   # pass0 は operator 設定を尊重
        env = score_env.copy()
        env["KEIBA_SCORE_PARALLEL"] = ""                       # ARCH-A 並列 off = 単一セッション
        env.pop("KEIBA_SCORE_HORSES_PER_SHARD", None)
        env.pop("KEIBA_SCORE_MAX_SHARDS", None)
        base_q = score_queries_per_horse or 2
        env["KEIBA_SCORE_QUERIES_PER_HORSE"] = str(min(base_q, 4 if attempt == 1 else 3))
        try:
            conc0 = int(llm_max_concurrent or score_env.get("KEIBA_LLM_MAX_CONCURRENT") or 20)
        except (TypeError, ValueError):
            conc0 = 20
        env["KEIBA_LLM_MAX_CONCURRENT"] = str(min(conc0, 4))   # 同時 claude -p を絞り輻輳を解く
        env["KEIBA_SCORE_TIMEOUT"] = _retry_timeout             # 内側 claude kill を外側 timeout の手前に
        return env

    succeeded: set[str] = set()
    pending = list(targets)
    attempt = 0
    while pending:
        # パスごとに across-race 並列度を下げる: 0→parallel, 1→parallel//2, 2+→直列。
        # 輻輳 (同時 claude -p 過多 → Tavily/API 輻輳で早期死) が失敗原因なので、リトライは
        # 同時数を減らす + env も軽くする (_pass_env)。最終パスは直列 = 1レースだけ走る既知の成功条件。
        if attempt == 0:
            pass_parallel = max(1, parallel)
        elif attempt == 1:
            pass_parallel = max(1, parallel // 2)
        else:
            pass_parallel = 1
        pass_env = _pass_env(attempt)
        if attempt > 0:
            _safe_log(f"[claude-eval] リトライ {attempt}/{max_retries}: 指数未生成 "
                      f"{len(pending)} レースを 単一セッション・"
                      f"{pass_env['KEIBA_SCORE_QUERIES_PER_HORSE']}クエリ/頭・across-race 並列 "
                      f"{pass_parallel} で再生成 (輻輳回避)…")
        with ThreadPoolExecutor(max_workers=pass_parallel) as ex:
            for fut in as_completed([ex.submit(_one, t, pass_env) for t in pending]):
                t, _ok_rc, note = fut.result()
                idx_ok = _has_index(t)
                if idx_ok:
                    succeeded.add(_internal(t))
                status = "OK" if idx_ok else f"指数なし ({note})"
                _safe_log(f"[claude-eval] ({len(succeeded)}/{total}) "
                          f"{t['venue']}{t['race_no']}R {status}")
                if on_progress is not None:
                    try:
                        on_progress(t, len(succeeded), total)
                    except Exception:  # noqa: BLE001  進捗更新の失敗で生成を止めない
                        pass
        pending = [t for t in pending if _internal(t) not in succeeded]
        attempt += 1
        if not pending:
            break
        if attempt > max_retries:
            labels = ", ".join(f"{t['venue']}{t['race_no']}R" for t in pending)
            _safe_log(f"[claude-eval] {len(pending)} レースは {max_retries} 回リトライしても "
                      f"指数生成できず見送り: {labels}")
            break
        backoff = 3.0 + attempt * 3.0
        _safe_log(f"[claude-eval] {len(pending)} レース未生成 → {backoff:.0f}s 待って "
                  f"並列を下げて再試行…")
        time.sleep(backoff)
    _safe_log(f"[claude-eval] 完了: {len(succeeded)}/{total} 生成成功"
              + (f" ({attempt} パス)" if attempt > 1 else ""))
    return len(succeeded)


# ----------------------------------------------------------------- scan -------

def scan(
    *,
    date: str | None = None,
    race_type: str = "all",
    edge_margin: float = 3.0,
    edge_threshold: float = 25.0,
    upcoming_only: bool = True,
    claude_all: bool = False,
    claude_eval: int = 0,
    claude_eval_timeout: int = 900,
    claude_eval_parallel: int = 4,
    score_parallel: bool = False,
    score_queries_per_horse: int | None = None,
    llm_max_concurrent: int | None = None,
    model: str = "opus",
    research: str = "agentic",
    max_races: int | None = None,
    out: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """当日全レースを **基準B (市場との順位乖離) 単独** で採点して勝負レースを抽出した dict を返す。

    ユーザ指示 (2026-06-28): 基準A=強弱 (separation) は廃止。判定・勝負スコアとも基準B のみ。
    `out` を渡すと結果 JSON を **2 段階で書き出す**: ①Claude 指数の一括生成がある時は生成前に
    暫定一覧 (`generating=True`) を先出し ②各レース生成完了ごとに再採点して live 更新
    ③全生成後に最終版 (`generating=False`) を書き出す。各レースの Claude 指数が付き次第 基準B が
    確定する。`out=None` (CLI stdout / テスト) のときは書き出さず最終 dict を返すだけ。
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
    if max_races is not None and max_races > 0 and len(races) > max_races:
        # 発走日時が **近い (早い) 順** に N 件だけ採用 (ユーザ指示 2026-06-30)。races は
        # start_at 昇順なので先頭 N = これから最も早く発走する (=今すぐ賭けられる) N 件。
        # 旧挙動 (末尾 N=発走が遅い夜のレース) だと昼スキャン時に発走が近いレースが落ちるため反転。
        dropped = len(races) - max_races
        races = races[:max_races]
        _log(f"[limit] 取得レース数={max_races} → 発走が近い {max_races} 件を採用 "
             f"(発走が遅い {dropped} 件を除外)")

    # **発走後も推奨レースをファイルに残す** (ユーザ指摘 2026-06-28): scan は file を上書きし、
    # upcoming_only=True は発走済を discovery から落とす。再スキャンするとそれまで推奨だった
    # 発走済レースが file から消え、ダッシュボード仮想収支 (compute_shobu_pnl は file の
    # recommended を読む) が発走後に数えられなくなる。→ 前回 file の発走済の **評価対象レース**
    # (recommended に限らず・当日母集団全体) を復元してマージし、file を当日の累積記録にする。
    if upcoming_only and out is not None and out.exists():
        have = {r["race_id"] for r in races}
        try:
            prior_races = json.loads(out.read_text(encoding="utf-8")).get("races") or []
        except (OSError, json.JSONDecodeError):
            prior_races = []
        carried = 0
        for pr in prior_races:
            rid = pr.get("race_id")
            if not rid or rid in have:
                continue
            if race_type in ("jra", "nar", "banei") and pr.get("race_type") != race_type:
                continue   # type 別スキャンに別 type を混ぜない
            races.append({
                "netkeiba_race_id": pr.get("netkeiba_race_id", ""),
                "race_id": rid,
                "venue": pr.get("venue", ""),
                "race_no": int(pr.get("race_no") or 0),
                "race_type": pr.get("race_type", ""),
                "start_at": int(pr.get("start_at") or 0),
                "close_at": int(pr.get("close_at") or 0),
                "source": "", "url": "",
            })
            have.add(rid)
            carried += 1
        if carried:
            races.sort(key=lambda r: (r["start_at"] or 0, r["race_no"]))
            _log(f"[carry] 前回 file の発走済レース {carried} 件を維持 (発走後も dashboard が数えられるように)")

    _log(f"[filter] 評価対象 {len(races)} レース "
         f"(race_type={race_type} / 発走前のみ={upcoming_only})")

    # 既存 snapshot をロード (基準B は snapshot の Claude 指数 + 市場 index で判定するので
    # scan 時の追加オッズ fetch は不要 — 強弱廃止で fresh odds の用途が無くなった)。
    for r in races:
        r["_snap"] = _load_snapshot(r["race_id"])

    # 各レースを採点 (scan / refresh 共通の _evaluate_race = 基準B 単独)。
    eval_kwargs = dict(edge_margin=edge_margin, edge_threshold=edge_threshold)
    results = [_evaluate_race(r, **eval_kwargs) for r in races]
    by_id = {r["race_id"]: r for r in races}   # race_id → メタ (再採点に使う)
    options = {
        "date": date, "race_type": race_type,
        "edge_margin": edge_margin, "edge_threshold": edge_threshold,
        "upcoming_only": upcoming_only,
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
    # claude_all=False & claude_eval>0: 発走が近い順に claude_eval 件のみ。既にスコア済は二重生成しない。
    targets: list[dict[str, Any]] = []
    if claude_all or claude_eval > 0:
        targets = _select_claude_targets(
            results, claude_all=claude_all, claude_eval=claude_eval,
            upcoming_only=upcoming_only, now=now)
    gen_total = len(targets)

    if gen_total > 0:
        # ① 暫定一覧 (指数未生成のレースは未判定) を先出し。フロントは生成完了を待たず表示できる。
        _log(f"[provisional] 暫定一覧を先出し → Claude 指数 {gen_total} 件を生成中…")
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
                         model=model, research=research,
                         on_progress=_on_progress)

        # ③ 権威的な最終再評価 — on_progress が呼ばれなくても (テストの monkeypatch 等) 確定させる。
        for t in targets:
            _rescore(t["race_id"])
    elif claude_all or claude_eval > 0:
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
    市場との順位乖離 (基準B = market_index を最新オッズで再計算) を recompute → 勝負スコアを
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
    # 基準A (強弱/separation) は廃止 (ユーザ指示 2026-06-28)。判定は基準B (市場との順位乖離) 単独。
    ap.add_argument("--edge-margin", type=float, default=3.0,
                    help="乖離馬の指数差フロア (claude−market ≥ これ)")
    ap.add_argument("--edge-threshold", type=float, default=25.0,
                    help="市場乖離スコアしきい値 (これ以上で勝負レース)")
    ap.add_argument("--include-finished", action="store_true", help="締切済も含める")
    ap.add_argument("--claude-all", action="store_true",
                    help="Claude 指数が無い発走前レースを全件 claude -p で一括生成")
    ap.add_argument("--claude-eval", type=int, default=0, help="上位N件に Claude 指数を新規生成 (--claude-all なしの時)")
    ap.add_argument("--claude-eval-timeout", type=int, default=900,
                    help="各レースの score subprocess の外側 timeout 秒 (既定 900)。下限 210s に"
                         "クランプ — 内側 claude (research 60s + scoring 60s floor + scrape) が"
                         "外側 kill より先に終わるのを保証する")
    ap.add_argument("--claude-eval-parallel", type=int, default=4,
                    help="across-race 同時生成数 (既定4)。多いと claude -p/Tavily 輻輳で "
                         "一部レースが指数なしで終わる → _run_claude_eval が並列を下げて自動リトライ")
    ap.add_argument("--score-parallel", dest="score_parallel", action="store_true",
                    default=True, help="score 段の検索を並列化 (KEIBA_SCORE_PARALLEL=1, 既定 ON)")
    ap.add_argument("--no-score-parallel", dest="score_parallel", action="store_false",
                    help="score 段の検索並列化を無効化 (単一セッション)")
    ap.add_argument("--score-queries-per-horse", type=int, default=10,
                    help="1馬あたり検索クエリ数 (KEIBA_SCORE_QUERIES_PER_HORSE)。"
                         "頭数×これ クエリが流れる (既定 10)")
    ap.add_argument("--llm-max-concurrent", type=int, default=20, help="claude -p 同時数上限 (KEIBA_LLM_MAX_CONCURRENT, 既定 20)")
    ap.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus",
                    help="Claude 指数を生成する claude -p のモデル (既定 opus)。sonnet/haiku は"
                         "速いが検索深度/推論の質は要検証")
    ap.add_argument("--research", choices=["agentic", "prefetch"], default="agentic",
                    help="リサーチ方式 (KEIBA_SCORE_RESEARCH)。agentic=Claude が MCP 検索 (従来) / "
                         "prefetch=固定クエリを Tavily 直叩き + 採点 claude 1回 (速い・輻輳なし・"
                         "TAVILY_API_KEY 必須。dossier 不可時は agentic に自動フォールバック)")
    ap.add_argument("--max-races", type=int, default=None,
                    help="取得レース数の上限。発走日時が近い (早い) 順に N 件だけ評価 (既定=全件)")
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
        edge_margin=args.edge_margin,
        edge_threshold=args.edge_threshold,
        upcoming_only=not args.include_finished,
        claude_all=args.claude_all,
        claude_eval=args.claude_eval,
        claude_eval_timeout=args.claude_eval_timeout,
        claude_eval_parallel=args.claude_eval_parallel,
        score_parallel=args.score_parallel,
        score_queries_per_horse=args.score_queries_per_horse,
        llm_max_concurrent=args.llm_max_concurrent,
        model=args.model,
        research=args.research,
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
