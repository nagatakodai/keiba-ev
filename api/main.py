"""FastAPI エンドポイント。

起動:
    uvicorn api.main:app --reload --port 9788

機能:
- GET  /api/predictions             予測スナップショット一覧
- GET  /api/predictions/{race_id}   詳細
- GET  /api/calibrate               calibrate.py 相当の JSON
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
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .runner import JobRegistry, WatchAutoManager, build_analyze_cmd, shutdown_all_jobs
from .store import (
    PRED_DIR,
    RESULT_DIR,
    _safe_race_id,
    compute_calibration,
    get_prediction,
    list_auto_watch_history,
    list_predictions,
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
WATCH = WatchAutoManager()


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
    )
    label = f"{'refresh' if req.refresh else 'analyze'}: {req.url}"
    job = JOBS.new(label=label, cmd=cmd)
    await job.start()
    return job.to_dict()


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
    window: int = 5
    tolerance: int = 4
    interval_sec: int = 60
    ev_max: float | None = None
    min_prob: float | None = None
    market_blend: float | None = None
    aptitude_top: int | None = None
    with_exacta: bool = False
    with_trio: bool = False
    # race detection を行う JST 時間帯 (HH:MM-HH:MM)。
    # 範囲外は result fetch のみ動かして race detection はスキップ。
    # JRA 土日 ~9:50-17:00 + NAR ナイター ~14:00-21:00 + ばんえい 等の遅レースを含めて広め。
    active_hours: str = "09:00-23:45"


@app.post("/api/watch-auto/start")
async def api_watch_start(req: WatchAutoStartRequest) -> dict[str, Any]:
    job = await WATCH.start(
        window=req.window,
        tolerance=req.tolerance,
        interval_sec=req.interval_sec,
        ev_max=req.ev_max,
        min_prob=req.min_prob,
        market_blend=req.market_blend,
        aptitude_top=req.aptitude_top,
        with_exacta=req.with_exacta,
        with_trio=req.with_trio,
        active_hours=req.active_hours,
    )
    return {"running": WATCH.running, "config": WATCH.config, "job": job.to_dict()}


@app.post("/api/watch-auto/stop")
async def api_watch_stop() -> dict[str, Any]:
    await WATCH.stop()
    return {"running": WATCH.running, "config": WATCH.config}


@app.get("/api/watch-auto/status")
def api_watch_status() -> dict[str, Any]:
    return {
        "running": WATCH.running,
        "config": WATCH.config,
        "job": WATCH.job.to_dict() if WATCH.job else None,
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
