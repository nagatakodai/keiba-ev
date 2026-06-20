"""FastAPI エンドポイント。

起動:
    uvicorn api.main:app --reload --port 9788

機能:
- GET  /api/predictions             予測スナップショット一覧
- GET  /api/predictions/{race_id}   詳細
- GET  /api/calibrate               calibrate.py 相当の JSON
- GET  /api/timeline/{race_id}      オッズ変動時系列 (win/place + depth) + 確定結果
- POST /api/analyze                 analyze ジョブ起動
- GET  /api/jobs                    ジョブ一覧
- GET  /api/jobs/{id}               ジョブ詳細
- GET  /api/jobs/{id}/stream        SSE でログ配信
- POST /api/jobs/{id}/cancel        中断
- POST /api/watch-auto/start        watch-auto 開始
- POST /api/watch-auto/stop         watch-auto 停止
- GET  /api/watch-auto/status       状態
- GET  /api/watch-auto/history      自動解析履歴
- GET  /api/watch-auto/stream       ログ SSE
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# .env を読み込む (src/analyze.py と同じ挙動)。これがないと `make api` (uvicorn) の env に
# ODDSPARK_ID/PASSWORD/PIN が乗らず、Web UI から起動した投票 daemon の自動ログインが失敗する。
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .runner import (
    PY,
    JobRegistry,
    WatchAutoManager,
    build_analyze_cmd,
    build_shobu_cmd,
    shutdown_all_jobs,
)
from .store import (
    PRED_DIR,
    RESULT_DIR,
    SHOBU_DIR,
    _safe_race_id,
    compute_calibration,
    get_prediction,
    get_shobu_result,
    list_auto_watch_history,
    list_predictions,
    netkeiba_rid_from_internal,
    shobu_today_jst,
)


# --- API key auth ---
# 共有 API キーを X-API-Key ヘッダまたは ?api_key= で受ける。
# API_SHARED_KEY が未設定なら認証スキップ (ローカル開発用)。
# /api/health は監視用に常に open。
API_KEY = os.environ.get("API_SHARED_KEY", "").strip()
PUBLIC_PATHS = {"/api/health", "/", "/docs", "/openapi.json", "/redoc"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not API_KEY:
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        provided = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
        if not hmac.compare_digest(provided, API_KEY):
            return JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """startup: 永続化された watch-auto 状態を読んで should_run=true なら再起動。
    shutdown: 生きている subprocess を全て倒す (オーファン防止)。
    """
    try:
        await WATCH.resume_if_needed()
    except Exception as e:  # noqa: BLE001 - startup を絶対に止めない
        print(f"[lifespan.startup] watch resume failed: {e}")
    yield
    await shutdown_all_jobs()


app = FastAPI(title="keiba-ev API", version="0.1.0", lifespan=lifespan)
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
)

JOBS = JobRegistry()
# JOBS を渡すと投票 daemon (oddspark/ipat) の Job が /api/jobs に載り、Web UI から
# daemon ログ (ブラウザ起動 / X server エラー / ログイン待ち) を閲覧できる。
WATCH = WatchAutoManager(registry=JOBS)


# --- predictions ---

@app.get("/api/predictions")
def api_predictions(limit: int = 100) -> dict[str, Any]:
    return {"items": list_predictions(limit=limit)}


@app.get("/api/predictions/{race_id}")
def api_prediction(race_id: str) -> dict[str, Any]:
    d = get_prediction(race_id)
    if d is None:
        raise HTTPException(404, f"prediction not found: {race_id}")
    return d


# --- calibrate ---

@app.get("/api/calibrate")
def api_calibrate(point_cost: int = 100) -> dict[str, Any]:
    return compute_calibration(point_cost=point_cost)


# --- odds timeline ---

@app.get("/api/timeline/{race_id}")
def api_timeline(race_id: str) -> dict[str, Any]:
    """オッズ変動の時系列 (`data/cache/odds_timeline/<race_id>.jsonl`) + 確定結果。

    各行の odds は UI チャート用に win/place のみ (3連単グリッドは数千組で巨大)。
    券種別の組数は depth に載る。結果があれば finish_order / final_odds も返す。
    """
    from .store import get_timeline  # 遅延 import (/api/pending と同パターン)

    d = get_timeline(race_id)
    if d is None:
        raise HTTPException(404, f"timeline not found: {race_id}")
    return d


# --- record (Web UI PendingRecorder からの手動着順入力) ---

class RecordRequest(BaseModel):
    race_id: str
    finish_order: list[int] = Field(..., min_length=3, max_length=3)
    trifecta_payout: int = 0
    note: str | None = None


@app.post("/api/record")
def api_record(req: RecordRequest) -> dict[str, Any]:
    """data/results/<race_id>.json に手動で着順を保存。
    src/record.py CLI と同じ振る舞い (既存 file 上書き禁止、prediction との突合)。
    """
    import datetime as dt

    safe = _safe_race_id(req.race_id)
    if safe is None:
        raise HTTPException(400, "invalid race_id")
    if any(n < 1 or n > 18 for n in req.finish_order):
        raise HTTPException(400, "finish_order must contain 馬番 1..18")
    if len(set(req.finish_order)) != 3:
        raise HTTPException(400, "finish_order must be 3 unique 馬番")
    if req.trifecta_payout < 0:
        raise HTTPException(400, "trifecta_payout must be non-negative")
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{safe}.json"
    if out_path.exists():
        # CLI 同様、誤上書き防止。意図的な訂正は UI からは不可 (CLI で --overwrite)。
        raise HTTPException(409, f"result already recorded: {safe}")
    payload = {
        "race_id": safe,
        "finish_order": req.finish_order,
        "trifecta_payout": int(req.trifecta_payout),
        "note": req.note or "",
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "manual",
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pred_path = PRED_DIR / f"{safe}.json"
    return {
        "saved": True,
        "race_id": safe,
        "finish_order": req.finish_order,
        "trifecta_payout": int(req.trifecta_payout),
        "matched": pred_path.exists(),
    }


# --- analyze jobs ---

class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="netkeiba 出馬表 / オッズ URL (race_id 含む)")
    refresh: bool = False
    no_llm: bool = False
    llm_model: str = "opus"
    ev_max: float | None = None
    min_prob: float | None = None
    market_blend: float | None = None
    # Plan G の適性 top N (頭数)。None なら CLI default (6)。
    aptitude_top: int | None = None
    # 馬単 (b5) / 3 連複 (b6) を追加 fetch するか。jiku iteration で重い。
    with_exacta: bool = False
    with_trio: bool = False
    # phase=score = Claude 指数のみ生成し暫定 snapshot を保存 (束選定・実弾なし) /
    # bet (既定) = 指数+市場で P→束→確定 snapshot。レース予測分析タブは score を送る。
    phase: Literal["score", "bet"] = "bet"
    # score タブの検索チューニング (phase=score 時に per-job env で analyze へ渡す)。
    # 検索並列化 (KEIBA_SCORE_PARALLEL)。queries は並列時のみ有効。
    score_parallel: bool = False
    # 1馬あたり検索クエリ数の上限/回数 (KEIBA_SCORE_QUERIES_PER_HORSE)。None=既定6。
    score_queries_per_horse: int | None = Field(default=None, ge=2, le=12)
    # score 段の締切=タイムアウト秒 (KEIBA_SCORE_TIMEOUT)。None=既定900。
    score_timeout: int | None = Field(default=None, ge=60, le=1800)


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest) -> dict[str, Any]:
    cmd = build_analyze_cmd(
        req.url,
        refresh=req.refresh,
        no_llm=req.no_llm,
        llm_model=req.llm_model,
        ev_max=req.ev_max,
        min_prob=req.min_prob,
        market_blend=req.market_blend,
        aptitude_top=req.aptitude_top,
        with_exacta=req.with_exacta,
        with_trio=req.with_trio,
        phase=req.phase,
    )
    label = f"{('score' if req.phase == 'score' else 'refresh' if req.refresh else 'analyze')}: {req.url}"
    # score タブの検索チューニングは per-job env で渡す (os.environ を汚さない)。
    # KEIBA_SCORE_PARALLEL は ON のとき "1"、OFF のとき "" (継承した "1" を確実に打ち消す)。
    score_env: dict[str, str] = {}
    if req.phase == "score":
        score_env["KEIBA_SCORE_PARALLEL"] = "1" if req.score_parallel else ""
        if req.score_queries_per_horse is not None:
            score_env["KEIBA_SCORE_QUERIES_PER_HORSE"] = str(req.score_queries_per_horse)
        if req.score_timeout is not None:
            score_env["KEIBA_SCORE_TIMEOUT"] = str(req.score_timeout)
    job = JOBS.new(label=label, cmd=cmd, env_extra=score_env or None)
    await job.start()
    return job.to_dict()


@app.post("/api/predictions/{race_id}/refresh-odds")
async def api_refresh_odds(race_id: str) -> dict[str, Any]:
    """履歴のレースを今すぐ最新オッズで再取得 → score 段を再評価し stage="score" で上書き。

    経路は snapshot の odds_source (欠落=netkeiba 経路)、netkeiba rid は内部 race_id から復元
    (旧 snapshot も可)。score 段スクレイパは即時に fresh odds を取りに行くので --refresh は付けない
    (= 締切まで待たず今すぐ取得)。Job を返すので frontend は /api/jobs/{id}/stream で進捗を見られる。"""
    safe = _safe_race_id(race_id)
    if safe is None:
        raise HTTPException(400, f"invalid race_id: {race_id}")
    snap = get_prediction(safe)
    if not snap:
        raise HTTPException(404, f"prediction not found: {race_id}")
    rid = netkeiba_rid_from_internal(snap.get("race_id") or safe)
    if not rid:
        raise HTTPException(422, f"netkeiba race_id を race_id から復元できません: {race_id}")
    odds_source = (snap.get("odds_source") or "").strip().lower()
    start_at = int(snap.get("start_at") or 0)
    if odds_source == "keibago":
        cmd = [PY, "-m", "src.scrape_keibago", rid, "--snapshot", f"--start-at={start_at}", "--phase=score"]
    elif odds_source == "jra":
        cmd = [PY, "-m", "src.scrape_jra", rid, "--snapshot", f"--start-at={start_at}", "--phase=score"]
    elif odds_source == "oddspark":
        cmd = [PY, "-m", "src.scrape_oddspark", rid, "--snapshot", f"--start-at={start_at}", "--phase=score"]
    elif odds_source in ("", "netkeiba"):
        # netkeiba 経路: rid から出馬表 URL を組む (JRA=race. / NAR=nar.)。即時 score (--refresh なし)。
        is_jra = rid[4:6] in {f"{i:02d}" for i in range(1, 11)}
        host = "race.netkeiba.com" if is_jra else "nar.netkeiba.com"
        url = f"https://{host}/race/shutuba.html?race_id={rid}"
        cmd = build_analyze_cmd(url, phase="score")
    else:
        raise HTTPException(422, f"unknown odds_source: {odds_source}")
    job = JOBS.new(label=f"refresh-odds: {race_id}", cmd=cmd)
    await job.start()
    return job.to_dict()


# --- shobu (今日の勝負レース) ---

class ShobuScanRequest(BaseModel):
    # 評価日 (YYYYMMDD)。None なら当日 JST。
    date: str | None = None
    # 対象 (all=JRA+NAR / jra / nar)。Literal で値検証。
    race_type: Literal["all", "jra", "nar"] = "all"
    # 基準A (強弱がはっきり = 市場 implied 勝率の集中度) を使うか。
    use_separation: bool = True
    # 基準B (市場より Claude 指数が高い馬が複数) を使うか。
    use_claude_edge: bool = True
    # 基準の合成 (or=いずれか / and=両方)。
    combine: Literal["or", "and"] = "or"
    # 基準A しきい値 (sep_score 0-100)。これ以上で「強弱はっきり」。
    sep_threshold: float = Field(default=35.0, ge=0, le=100)
    # 基準B: Claude 指数 − 市場指数 がこの値以上の馬を「市場超え」とみなす。
    edge_margin: float = Field(default=8.0, ge=0, le=100)
    # 基準B: 「市場超え」の馬がこの頭数以上で勝負 (複数=2)。
    edge_min_count: int = Field(default=2, ge=1, le=18)
    # 発走前のみ (締切前) を対象にするか。False で締切済も含む。
    upcoming_only: bool = True
    # snapshot に市場データが無いレースの最新オッズ (単勝) を取得するか。
    fetch_odds: bool = True
    # Claude 指数なしの強弱上位 N 件に score ステージを spawn して指数を新規生成 (既定 0=しない)。
    # コスト/時間がかかるので上限を設ける。
    claude_eval: int = Field(default=0, ge=0, le=20)
    # 評価レース数の上限 (デバッグ/負荷制御)。None=全件。
    max_races: int | None = Field(default=None, ge=1, le=300)


@app.post("/api/shobu/scan")
async def api_shobu_scan(req: ShobuScanRequest) -> dict[str, Any]:
    """今日の勝負レース スキャンを Job として起動。結果は data/cache/shobu/<date>.json に書かれ、
    GET /api/shobu/result?date=... で取得できる。Job はバックグラウンドで進捗を stream する。"""
    date = (req.date or shobu_today_jst()).strip()
    import re as _re
    if not _re.fullmatch(r"\d{8}", date):
        raise HTTPException(400, f"invalid date (YYYYMMDD expected): {req.date}")
    SHOBU_DIR.mkdir(parents=True, exist_ok=True)
    out_path = str(SHOBU_DIR / f"{date}.json")
    cmd = build_shobu_cmd(
        out_path,
        date=date,
        race_type=req.race_type,
        use_separation=req.use_separation,
        use_claude_edge=req.use_claude_edge,
        combine=req.combine,
        sep_threshold=req.sep_threshold,
        edge_margin=req.edge_margin,
        edge_min_count=req.edge_min_count,
        upcoming_only=req.upcoming_only,
        fetch_odds=req.fetch_odds,
        claude_eval=req.claude_eval,
        max_races=req.max_races,
    )
    job = JOBS.new(label=f"shobu-scan: {date}", cmd=cmd)
    await job.start()
    d = job.to_dict()
    d["date"] = date
    return d


@app.get("/api/shobu/result")
def api_shobu_result(date: str | None = None) -> dict[str, Any]:
    """勝負レース スキャンの最新結果 (data/cache/shobu/<date>.json)。未スキャンは 404。"""
    d = get_shobu_result(date)
    if d is None:
        raise HTTPException(404, "shobu result not found (まだスキャンしていません)")
    return d


@app.get("/api/jobs")
def api_jobs() -> dict[str, Any]:
    return {"items": JOBS.list()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"job not found: {job_id}")
    d = job.to_dict()
    d["lines"] = list(job.lines)
    return d


@app.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"job not found: {job_id}")
    await job.cancel()
    return job.to_dict()


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str, since: int = 0) -> EventSourceResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"job not found: {job_id}")

    async def gen():
        async for line in job.stream(since=since):
            yield {"event": "log", "data": json.dumps(line, ensure_ascii=False)}
        yield {"event": "end", "data": json.dumps(job.to_dict(), ensure_ascii=False)}

    return EventSourceResponse(gen())


# --- watch-auto ---

class WatchAutoStartRequest(BaseModel):
    # 2段パイプライン: SCORE 帯 (締切 score_window〜+score_tolerance 分前) で Claude 考察→各馬
    # 指数をキャッシュ → BET 帯 (締切 window〜+tolerance 分前) で最新オッズ+指数→束→投票。
    # window/tolerance は分。0 で締切ちょうどまで受け付け、小数可 (例 0.5)。ge=0 で負値拒否。
    window: float = Field(default=1, ge=0)        # BET 帯 (既定 締切1分前)
    tolerance: float = Field(default=1.5, ge=0)
    score_window: float = Field(default=5, ge=0)  # SCORE 帯 (既定 締切5分前で考察)
    score_tolerance: float = Field(default=2, ge=0)
    # Claude 指数と model fundamental の合成重み (0=モデルのみ, 1=指数のみ)。
    # None で各 analyze の既定 (ev.LLM_BLEND_DEFAULT=0.5)。
    llm_blend: float | None = Field(default=None, ge=0.0, le=1.0)
    # 締切の何秒前に投票を発火するか (score 完了で予約、この秒数で発火)。既定 60=締切1分前。
    bet_lead_sec: int = Field(default=150, ge=0, le=600)
    interval_sec: int = 60
    ev_max: float | None = None
    min_prob: float | None = None
    market_blend: float | None = None
    aptitude_top: int | None = None
    with_exacta: bool = False
    with_trio: bool = False
    # claude -p (回収優先の束選定 + 的中優先評価) を一切使わず確率モデルのみで snapshot 保存。
    no_llm: bool = False
    # race detection を行う JST 時間帯 (HH:MM-HH:MM)。
    # 範囲外は result fetch のみ動かして race detection はスキップ。
    # JRA 土日 ~9:50-17:00 + NAR ナイター ~14:00-21:00 + ばんえい 等の遅レースを含めて広め。
    active_hours: str = "09:00-23:45"
    # オッズパーク自動投票 (カート投入)。ON で auto_watch に --bet-oddspark を付け、
    # 投票 daemon (headful ブラウザ・人がログイン) を起動する。
    # **headful なので `make api` は DISPLAY のある端末で起動しておくこと** (WSLg 等)。
    bet_oddspark: bool = False
    # 自動ログイン。ON で daemon に --auto-login を付け、env 認証 (ODDSPARK_ID/PASSWORD/PIN) で
    # 自動ログインする。**uvicorn (`make api`) の env にこれらを設定しておくこと** (未設定だと daemon が
    # 起動直後に失敗)。OFF (既定) は人が headful ブラウザで手でログイン (最も安全)。
    bet_auto_login: bool = False
    # 自動購入 (実弾)。ON で #gotobuy → 確認 → 確定 まで自動。daily_cap で日次上限ガード。
    # AUTO_PURCHASE_VERIFIED=False の間は src 側で fail-safe (実弾を撃たない)。
    bet_auto_purchase: bool = False
    # 日次上限 (円)。0 で無効化、ge=0 で負値拒否、le で安全上限 (誤入力暴走防止)。
    bet_daily_cap: int = Field(default=50000, ge=0, le=10_000_000)
    # **このセッション中のみ** 3連単束の全 leg stake を N 倍 (小数倍可・100円単位切り捨て)。
    # per-race 上限 + daily_cap は維持される。gt=0 で 0 倍を拒否 (誤入力で予期しない floor 動作を
    # 避ける)、le=100 で実用上限 (100 倍超は事故の方が高確率)。
    bet_stake_multiplier: float = Field(default=1.0, gt=0.0, le=100.0)
    # per-race 上限の専用倍率 (×N)。上限 = 基準¥10,000 × N。None (既定) なら従来どおり
    # 掛金倍率に連動 (基準 × bet_stake_multiplier)。掛金倍率を上げずに上限だけ広げる /
    # 逆に上限だけ絞る用途。daemon の --max-stake-multiplier に渡る。gt=0/le=100 で誤入力ガード。
    bet_max_stake_multiplier: float | None = Field(default=None, gt=0.0, le=100.0)
    # 支払方法: opcoin (OPコイン残, 既定) | buylimit (投票資金残, 会員入金)
    # Literal で API 境界で値検証 (任意文字列を入れて子プロセスのラベルに垂れ流すのを防ぐ)。
    bet_payment_method: Literal["opcoin", "buylimit"] = "opcoin"
    # JRA 即PAT 自動投票 (カート投入)。ON で auto_watch に --bet-ipat を付け、JRA 投票 daemon
    # (headful ブラウザ・人がログイン) を起動。土日 JRA 開催日に JRA ブラウザも一緒に立てる用途。
    # **headful なので `make api` は DISPLAY のある端末で起動**。認証は env
    # (IPAT_INETID/IPAT_SUBSCRIBER/IPAT_PARS/IPAT_PIN)。bet_oddspark と独立に ON 可。
    bet_ipat: bool = False
    # 投票束の切替 (2026-06-10 レビュー後に復活): ev=EV束 (recommended_bundle, 既定・推奨) /
    # trifecta=3連単束 (recommended_bundle_t)。env KEIBA_BET_BUNDLE で auto_watch (enqueue 判定) と
    # 投票 daemon (oddspark/ipat) に伝播。EV束は全脚がシェード込み P×O≥1.02 を通過した時のみ
    # legs が立つため**大半のレースは見送り**になる (それが正しい挙動)。
    bet_bundle: Literal["ev", "trifecta"] = "ev"
    # EV束の1レース予算 (円)。env KEIBA_EV_BANKROLL で伝播。½Kelly なので実投入は通常この
    # 10-30%。実測で +EV が未実証のため計測モード ¥5,000 を初期推奨とする。
    ev_bankroll: int = Field(default=5_000, ge=100, le=10_000_000)
    # 3連単の1レース購入予算 (円)。束の合計購入額をこの予算内に収める (Claude選定・モデル共通)。
    # 全 dispatch subprocess に env KEIBA_TRIFECTA_BANKROLL で伝播 (analyze/keibago/jra/oddspark が尊重)。
    # 投票時の倍率 (bet_stake_multiplier) とは別: これは束を組む時点の予算、倍率は購入時のスケール。
    trifecta_bankroll: int = Field(default=10_000, ge=100, le=10_000_000)
    # 3連単束モード: recovery=回収(穴狙い, 市場1番人気はClaude指数>90でない限り1着に置かない) /
    # hit=旧 全力的中 (既定, 2026-06-18〜 実測 ROI で hit>recovery)。
    # env KEIBA_TRIFECTA_MODE で全 dispatch subprocess に伝播 (_trifecta_mode が尊重)。
    # Literal で API 境界で値検証 (任意文字列が env に垂れ流されるのを防ぐ)。
    trifecta_mode: Literal["recovery", "hit"] = "hit"
    # score ステージ (Claude 指数) の検索並列化。env KEIBA_SCORE_PARALLEL で全 dispatch に伝播。
    # True で K プロセス並列 research + 単一 scoring (検索大幅増)、False (既定) は単一セッション。
    score_parallel: bool = False
    # score の1馬あたり検索クエリ数 (env KEIBA_SCORE_QUERIES_PER_HORSE)。既定 6 (旧単一は2)。
    score_queries_per_horse: int = Field(default=6, ge=2, le=12)


@app.post("/api/watch-auto/start")
async def api_watch_start(req: WatchAutoStartRequest) -> dict[str, Any]:
    job = await WATCH.start(
        window=req.window,
        tolerance=req.tolerance,
        score_window=req.score_window,
        score_tolerance=req.score_tolerance,
        llm_blend=req.llm_blend,
        bet_lead_sec=req.bet_lead_sec,
        interval_sec=req.interval_sec,
        ev_max=req.ev_max,
        min_prob=req.min_prob,
        market_blend=req.market_blend,
        aptitude_top=req.aptitude_top,
        with_exacta=req.with_exacta,
        with_trio=req.with_trio,
        no_llm=req.no_llm,
        active_hours=req.active_hours,
        bet_oddspark=req.bet_oddspark,
        bet_auto_login=req.bet_auto_login,
        bet_auto_purchase=req.bet_auto_purchase,
        bet_daily_cap=req.bet_daily_cap,
        bet_stake_multiplier=req.bet_stake_multiplier,
        bet_max_stake_multiplier=req.bet_max_stake_multiplier,
        bet_payment_method=req.bet_payment_method,
        bet_ipat=req.bet_ipat,
        trifecta_bankroll=req.trifecta_bankroll,
        trifecta_mode=req.trifecta_mode,
        bet_bundle=req.bet_bundle,
        ev_bankroll=req.ev_bankroll,
        score_parallel=req.score_parallel,
        score_queries_per_horse=req.score_queries_per_horse,
    )
    return {"running": WATCH.running, "bet_running": WATCH.bet_running,
            "ipat_bet_running": WATCH.ipat_bet_running,
            "scheduler_running": WATCH.scheduler_running,
            "config": WATCH.config, "job": job.to_dict()}


@app.post("/api/watch-auto/stop")
async def api_watch_stop() -> dict[str, Any]:
    await WATCH.stop()
    return {"running": WATCH.running, "bet_running": WATCH.bet_running,
            "ipat_bet_running": WATCH.ipat_bet_running,
            "scheduler_running": WATCH.scheduler_running, "config": WATCH.config}


@app.get("/api/watch-auto/status")
def api_watch_status() -> dict[str, Any]:
    return {
        "running": WATCH.running,
        "bet_running": WATCH.bet_running,
        "ipat_bet_running": WATCH.ipat_bet_running,
            "scheduler_running": WATCH.scheduler_running,
        "config": WATCH.config,
        "job": WATCH.job.to_dict() if WATCH.job else None,
        "bet_job": WATCH.bet_job.to_dict() if WATCH.bet_job else None,
        "ipat_bet_job": WATCH.ipat_bet_job.to_dict() if WATCH.ipat_bet_job else None,
    }


@app.get("/api/watch-auto/history")
def api_watch_history(limit: int = 200) -> dict[str, Any]:
    return {"items": list_auto_watch_history(limit=limit)}


@app.delete("/api/pending/{race_id}")
def api_pending_delete(race_id: str) -> dict[str, Any]:
    """pending queue から特定 race_id のエントリを削除する。

    フロントの「failed / 救済不能なエントリを除外したい」要望に対応。
    auto-prune (24h) より早くキューから消したい場合に使う。
    返り値: removed (削除された件数, 0/1)、total (削除後の総件数)。
    """
    from src.fetch_result import _load_pending, _pending_lock, _save_pending
    # auto_watch loop の process_pending と同じ file lock 下で read/mutate/save。
    # lock 無しだと auto_watch が _load → 削除分が auto_watch の旧 entries
    # save で復活する lost update が起こる。
    with _pending_lock():
        entries = _load_pending()
        before = len(entries)
        entries = [e for e in entries if e.race_id != race_id]
        removed = before - len(entries)
        if removed > 0:
            _save_pending(entries)
        total = len(entries)
    return {"removed": removed, "race_id": race_id, "total": total}


@app.get("/api/pending")
def api_pending() -> dict[str, Any]:
    """結果取得 pending queue の現在状態を返す。

    UI が「これから取得予定 / 取得中 / 諦めた」レースを表示するための endpoint。
    `data/cache/pending_results.json` を読む。
    """
    from src.fetch_result import _load_pending  # 遅延 import (循環回避)
    import time as _time
    entries = _load_pending()
    now = int(_time.time())
    items = []
    for e in entries:
        items.append({
            "race_id": e.race_id,
            "url": e.url,
            "status": e.status,
            "attempts": e.attempts,
            "max_attempts": e.max_attempts,
            "retry_interval_sec": e.retry_interval_sec,
            "due_at": e.due_at,
            "next_attempt_at": e.next_attempt_at,
            "scheduled_at": e.scheduled_at,
            "seconds_until_next": max(0, e.next_attempt_at - now) if e.status == "pending" else 0,
            "last_error": e.last_error,
        })
    items.sort(key=lambda x: x["due_at"], reverse=True)
    pending = sum(1 for i in items if i["status"] == "pending")
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "success": sum(1 for i in items if i["status"] == "success"),
            "failed": sum(1 for i in items if i["status"] == "failed"),
        },
    }


@app.get("/api/watch-auto/stream")
async def api_watch_stream(since: int = 0) -> EventSourceResponse:
    if not WATCH.job:
        raise HTTPException(404, "watch-auto job not started")
    job = WATCH.job

    async def gen():
        async for line in job.stream(since=since):
            yield {"event": "log", "data": json.dumps(line, ensure_ascii=False)}
        yield {"event": "end", "data": json.dumps(job.to_dict(), ensure_ascii=False)}

    return EventSourceResponse(gen())


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True}
