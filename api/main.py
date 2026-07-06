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
import time
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
    attach_hit_labels,
    compute_calibration,
    compute_shobu_pnl,
    compute_indexed_pnl,
    compute_shobu_strategies_pnl,
    compute_indexed_strategies_pnl,
    compute_venue_breakdown,
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
    try:
        RESULTS_AUTO.start()  # 予測分析履歴の結果を 5 分毎に自動取得する常駐ループ
    except Exception as e:  # noqa: BLE001
        print(f"[lifespan.startup] results auto start failed: {e}")
    try:
        SHOBU_RESCORER.start()  # 勝負レース(推奨)を締切5-7分前に自動再score (パドック込み)
    except Exception as e:  # noqa: BLE001
        print(f"[lifespan.startup] shobu rescorer start failed: {e}")
    try:
        NIGHTLY_PRESCANNER.start()  # 前日夜間に翌日の勝負レースを全レース解析 (2026-07-05)
    except Exception as e:  # noqa: BLE001
        print(f"[lifespan.startup] nightly prescanner start failed: {e}")
    try:
        DAILY_SCANNER.start()  # 当日朝の自動キャッチアップスキャン (手動ボタン依存の解消, 2026-07-06)
    except Exception as e:  # noqa: BLE001
        print(f"[lifespan.startup] daily scanner start failed: {e}")
    yield
    try:
        await RESULTS_AUTO.stop()  # 結果取得ループを止めてから残 Job を倒す
    except Exception:  # noqa: BLE001
        pass
    try:
        await SHOBU_RESCORER.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        await NIGHTLY_PRESCANNER.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        await DAILY_SCANNER.stop()
    except Exception:  # noqa: BLE001
        pass
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


class ResultAutoFetcher:
    """make api 稼働中に **予測分析履歴の結果 (着順/払戻)** を interval 毎 (既定10分) に自動取得する常駐ループ。

    予測 (data/predictions) のうち**発走済で結果未取得の全レース** (日付不問・ユーザ指示 2026-06-28)
    を pending queue に enqueue し (fetch_result.schedule = 既存結果はスキップ・race_id で dedup・
    terminal failed は resurrect しない)、process_pending で確定結果を取得 → data/results に保存 →
    calibrate / 予測分析履歴 / ダッシュボードに反映される。process_pending は file-lock 済なので
    watch-auto と併走しても二重 fetch しない。watch-auto を回していなくても (手動 analyze / 勝負
    レース由来の予測も含め) 結果が埋まり続ける。interval は env KEIBA_RESULT_FETCH_INTERVAL_SEC で
    上書き可 (既定 600)。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # 既定 10 分毎 (ユーザ指示 2026-06-28)。env KEIBA_RESULT_FETCH_INTERVAL_SEC で上書き可。
        try:
            self.interval_sec = max(60, int(os.environ.get("KEIBA_RESULT_FETCH_INTERVAL_SEC", "600")))
        except (TypeError, ValueError):
            self.interval_sec = 600
        self.last_run_at: float | None = None
        self.next_run_at: float | None = None
        self.last_summary: dict[str, Any] | None = None
        self.runs: int = 0
        self.market_agreement_appends: int = 0   # 市場一致シグナルを history に追記した回数
        self.signal_rules_appends: int = 0        # プレレジルール検証を history に追記した回数

    def status(self) -> dict[str, Any]:
        return {
            "interval_sec": self.interval_sec,
            "loop_running": self._task is not None and not self._task.done(),
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "runs": self.runs,
            "last_summary": self.last_summary,
            "market_agreement_appends": self.market_agreement_appends,
            "signal_rules_appends": self.signal_rules_appends,
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _enqueue_finished() -> int:
        """**発走済・結果未取得の全予測** (日付不問) を pending queue に enqueue (件数を返す)。

        ユーザ指示 (2026-06-28): 本日分のみでなく全レースの結果取得を試行する。発走前 (sa<=0 /
        未発走) と holdout 等 (start_at 無し) は対象外なので件数は数十程度に収まる (実測 ~39)。
        schedule() は既存結果があれば no-op・同 race_id は dedup。**resurrect_failed=False** で
        terminal failed (恒久取得不能=中止/欠落) は復活させない (毎 tick の無限リトライ + netkeiba
        過負荷を防ぐ)。block 失敗は process_pending が attempt を消費せず pending のまま retry する。
        netkeiba URL は内部 race_id から復元 (NAR は block→keiba.go.jp / JRA→公式 へ process_pending が fallback)。
        """
        from src.fetch_result import schedule

        now = int(time.time())
        jra_codes = {f"{i:02d}" for i in range(1, 11)}
        n = 0
        for it in list_predictions(limit=5000):
            if it.get("has_result"):
                continue
            sa = int(it.get("start_at") or 0)
            if sa <= 0 or now < sa + 60:           # 未発走 (発走1分後から結果待ち) / start_at 無し
                continue
            rid = netkeiba_rid_from_internal(it.get("race_id") or "")
            if not rid or len(rid) < 6:
                continue
            host = "race.netkeiba.com" if rid[4:6] in jra_codes else "nar.netkeiba.com"
            url = f"https://{host}/race/shutuba.html?race_id={rid}"
            try:
                schedule(it["race_id"], url, sa, resurrect_failed=False)
                n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    async def _run_once(self) -> None:
        from src.fetch_result import process_pending
        # blocking (file IO + HTTP) なので別スレッドへ。
        enq = await asyncio.to_thread(self._enqueue_finished)
        summary = await asyncio.to_thread(process_pending)
        # 市場一致シグナルを蓄積 (ユーザ指示 2026-06-30): 新しい結果が増えていれば history に追記し
        # CI が 0 から離れる (=確証) まで時系列で追う。dedup 済 (races 不変なら no-op)。例外は呑む。
        try:
            from api.store import append_market_agreement_history
            row = await asyncio.to_thread(append_market_agreement_history)
            if row:
                self.market_agreement_appends += 1
        except Exception:  # noqa: BLE001 - 計測は結果取得ループを止めない
            pass
        # プレレジ済シグナルルールの検証状況も同様に蓄積 (2026-07-05): 登録後レースの ROI CI が
        # 確証★/破綻 に遷移する過程を時系列で残す。dedup 済 (races 不変なら no-op)。
        try:
            from api.store import append_signal_rules_history
            row = await asyncio.to_thread(append_signal_rules_history)
            if row:
                self.signal_rules_appends += 1
        except Exception:  # noqa: BLE001 - 計測は結果取得ループを止めない
            pass
        self.last_run_at = time.time()
        self.runs += 1
        self.last_summary = {
            "enqueued": enq,
            "checked": summary.get("checked", 0),
            "success": len(summary.get("success") or []),
            "failed": len(summary.get("failed") or []),
            "not_due": summary.get("not_due", 0),
        }

    async def _loop(self) -> None:
        # 起動後しばらく待ってから初回実行 (起動直後の負荷集中 / reload 連打での暴発を避ける)。
        first = True
        while True:
            wait = 20 if first else self.interval_sec
            self.next_run_at = time.time() + wait
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            first = False
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - ループは止めない
                self.last_summary = {"error": str(e)}


RESULTS_AUTO = ResultAutoFetcher()


class ShobuPaddockRescorer:
    """make api 常駐: **勝負レース(推奨)を締切5-7分前に自動で再score** し Claude 指数を
    **パドック評価込みで再生成**する (ユーザ指示 2026-06-30)。

    make api を回しておくだけで、推奨レースが締切に近づいた時に score 段の claude -p が
    強化済みパドック検索 (締切~5分前のパドック/当日馬体重/気配) を実行し、その結果を取り込んだ
    指数に更新される。乖離 (基準B) / 市場一致シグナルも snapshot 経由で自動反映。score 段のみで
    **実弾投票はしない**。watch-auto 非依存 (併走しても snapshot 再生成は idempotent)。
    **再score が失敗/timeout しても `analyze._run_score_stage` は scores 空なら .llm.json を上書き
    しない**ので、scan 時点の指数は消えない (= 失敗は無害・最悪「指数据え置き+オッズ更新」)。
    """

    WINDOW_SEC = 7 * 60      # 締切この秒前から再score 対象に入れる (score に時間が要るので余裕)
    MIN_LEAD_SEC = 2 * 60    # 締切この秒前を切ったら対象外 (締切間際は撃たない)
    # 発火イベントの永続ログ (2026-07-06)。in-memory の status だけだと reload で消え、
    # 「再score 有無 × 戦略 ROI」の効果検証 (パドック検索が指数を実際に良くしているか) が
    # できなかった。1 発火 = 1 行 append し、後日 rid で predictions/results と join する。
    EVENTS_FILE = SHOBU_DIR.parent / "paddock_rescore_events.jsonl"

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.interval_sec = 60
        self._fired: set[str] = set()   # 当日 再score 済 race_id (window 内の二重撃ち防止)
        self._fired_date = ""
        self.rescored = 0
        self.attempts = 0
        self.last_run_at: float | None = None
        self.last_fired: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "loop_running": self._task is not None and not self._task.done(),
            "window": "締切5-7分前",
            "rescored": self.rescored,
            "attempts": self.attempts,
            "last_run_at": self.last_run_at,
            "last_fired": self.last_fired,
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    def _due(self) -> list[dict[str, Any]]:
        """今 締切 MIN_LEAD〜WINDOW 秒前 + 未 fire の **推奨 (recommended) NAR/JRA レース** を返す。"""
        today = shobu_today_jst()
        if today != self._fired_date:
            self._fired = set()
            self._fired_date = today
        path = SHOBU_DIR / f"{today}.json"
        if not path.exists():
            return []
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        now = time.time()
        out: list[dict[str, Any]] = []
        for r in doc.get("races") or []:
            if not r.get("recommended"):
                continue
            rid = r.get("netkeiba_race_id")
            internal = r.get("race_id") or rid
            close_at = r.get("close_at") or 0
            rtype = r.get("race_type")
            # banei (帯広ばんえい) も対象 (2026-07-06 実障害の修正): 旧 ("nar", "jra") は
            # ばんえいを再score から漏らし、帯広R3 が前夜の低品質指数 (haiku+フラット) のまま
            # 発走した。cmd 側は「jra 以外 → keibago」なので banei は keiba.go.jp 経路に乗る
            # (nightly の banei→keibago 修正 2026-07-05 と同経路)。
            if (not rid or not close_at or rtype not in ("nar", "jra", "banei")
                    or internal in self._fired):
                continue
            lead = close_at - now
            if self.MIN_LEAD_SEC <= lead <= self.WINDOW_SEC:
                out.append({
                    "netkeiba": rid, "internal": internal, "rtype": rtype,
                    "start_at": int(r.get("start_at") or 0),
                    "venue": r.get("venue", ""), "race_no": r.get("race_no"),
                })
        return out

    @staticmethod
    def _rescore(race: dict[str, Any]) -> bool:
        """1 レースを score 段で再生成 (Claude 指数をパドック込みで)。snapshot を上書き。"""
        import subprocess
        from api.runner import PY
        mod = "src.scrape_jra" if race["rtype"] == "jra" else "src.scrape_keibago"
        cmd = [PY, "-m", mod, race["netkeiba"], "--snapshot", "--phase=score",
               f"--start-at={race['start_at']}"]
        env = dict(os.environ)
        # 締切5分前の素早い再score: パドッククエリが入る予算を確保しつつ window 内に収める。
        env.setdefault("KEIBA_SCORE_QUERIES_PER_HORSE", "4")
        env["KEIBA_SCORE_TIMEOUT"] = "200"   # window 内に収める (失敗時は指数据え置きで無害)
        # ⚠ これが無いと _run_score_stage の「既存指数の再利用」(2026-06-24) に当たり、
        # スキャン時 (=パドック前) の指数をそのまま返して **再検索が一切走らない** =
        # パドック再score が no-op になる実バグだった (2026-07-05 修正)。前夜の先行生成
        # (翌日スキャン) と組むときは特に、当日直前の再検索がここでしか入らない。
        env["KEIBA_SCORE_FORCE_RESCORE"] = "1"
        t0 = time.time()
        rc: int | None = None
        ok = False
        try:
            proc = subprocess.run(cmd, timeout=240, capture_output=True, env=env)
            rc = proc.returncode
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        ShobuPaddockRescorer._log_event(race, rc=rc, duration_sec=round(time.time() - t0, 1), ok=ok)
        return ok

    @classmethod
    def _log_event(cls, race: dict[str, Any], *, rc: int | None,
                   duration_sec: float, ok: bool) -> None:
        """発火 1 回を EVENTS_FILE に 1 行 append。失敗は呑む (再score を止めない)。"""
        try:
            import datetime
            from zoneinfo import ZoneInfo
            line = json.dumps({
                "rid": race.get("internal"),          # 内部 race_id (predictions/results の join キー)
                "netkeiba": race.get("netkeiba"),
                "date": shobu_today_jst(),
                "fired_at": datetime.datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds"),
                "rc": rc,                              # subprocess の returncode (timeout/例外は None)
                "duration_sec": duration_sec,
                "ok": ok,                              # subprocess が例外なく終了したか (rc != 0 でも True)
            }, ensure_ascii=False)
            cls.EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with cls.EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001 - ログ失敗で再score を止めない
            pass

    async def _run_once(self) -> None:
        due = await asyncio.to_thread(self._due)
        for race in due:
            self._fired.add(race["internal"])   # 1 度撃ったら window 内で再撃しない (失敗でも)
            self.attempts += 1
            ok = await asyncio.to_thread(self._rescore, race)
            if ok:
                self.rescored += 1
                self.last_fired = {"race_id": race["internal"], "venue": race["venue"],
                                   "race_no": race["race_no"], "at": time.time()}
        self.last_run_at = time.time()

    async def _loop(self) -> None:
        first = True
        while True:
            try:
                await asyncio.sleep(25 if first else self.interval_sec)
            except asyncio.CancelledError:
                raise
            first = False
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ループは止めない
                pass


SHOBU_RESCORER = ShobuPaddockRescorer()


class ShobuNightlyPrescanner:
    """make api 常駐: **前日夜間に翌日の勝負レースを全レース解析** する (ユーザ指示 2026-07-05)。

    毎晩 `KEIBA_NIGHTLY_SCAN_HOUR` (JST, 既定 21 時・12-23 にクランプ) 以降の最初の tick で、
    **翌日** (JST+1日) の shobu スキャン (race_type=all・claude_all=True) を Job として 1 回
    だけ起動する。出馬表は前夜に公式各所へ出るので Claude 指数 (市場非依存) を先行生成できる:
      - オッズ未発売のレースは指数のみキャッシュ (odds_empty 早期 return, 2026-06-24 機構)
      - 当日の再スキャン/2分毎 refresh が市場を付けて基準B を確定 (指数は再利用 = 当日の
        Claude コストほぼゼロ)
      - 推奨レースは締切5-7分前に ShobuPaddockRescorer が FORCE_RESCORE で再検索し、
        パドック/当日馬体重を指数に反映 (前夜指数が stale なまま賭け時刻を迎えない)
      - keiba.go.jp に翌日カードが未掲載の NAR 場 (実測: 盛岡) は shobu 側の oddspark
        フォールバックが救う。JRA は前日夜に公式オッズページが開けば乗る (無ければ当日)
    無効化は env `KEIBA_NIGHTLY_SCAN=0`。`KEIBA_NIGHTLY_RESEARCH=prefetch` で ARCH-B
    (固定クエリ Tavily) に切替可 (夜間の大量一括はセッション数が効くので好相性)。
    実行済みガードは `SHOBU_DIR/nightly_state.json` に永続 (uvicorn --reload 耐性 =
    再起動のたびに再スキャンして Claude を浪費しない)。
    """

    STATE_FILE = SHOBU_DIR / "nightly_state.json"

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry
        self._task: asyncio.Task | None = None
        self.interval_sec = 300
        self.enabled = (os.environ.get("KEIBA_NIGHTLY_SCAN") or "1").strip() != "0"
        try:
            h = int((os.environ.get("KEIBA_NIGHTLY_SCAN_HOUR") or "21").strip())
        except ValueError:
            h = 21
        self.hour = min(23, max(12, h))
        self.launches = 0
        self.last_job_id: str | None = None
        self.last_launched_date: str | None = None
        self.last_run_at: float | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "hour_jst": self.hour,
            "loop_running": self._task is not None and not self._task.done(),
            "launches": self.launches,
            "last_job_id": self.last_job_id,
            "last_launched_date": self.last_launched_date,
            "last_run_at": self.last_run_at,
        }

    def start(self) -> None:
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _now_jst() -> "datetime.datetime":
        import datetime
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Tokyo"))

    def _tomorrow(self, now: "datetime.datetime | None" = None) -> str:
        import datetime
        n = now or self._now_jst()
        return (n + datetime.timedelta(days=1)).strftime("%Y%m%d")

    def _already_launched(self, date: str) -> bool:
        try:
            st = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
            return st.get("date") == date
        except (OSError, json.JSONDecodeError, ValueError):
            return False

    def _due(self, now: "datetime.datetime | None" = None) -> str | None:
        """発火すべきなら翌日日付 (YYYYMMDD) を返す。時刻前/実行済み/無効は None。"""
        if not self.enabled:
            return None
        n = now or self._now_jst()
        if n.hour < self.hour:
            return None
        date = self._tomorrow(n)
        if self._already_launched(date):
            return None
        return date

    async def _launch(self, date: str) -> None:
        SHOBU_DIR.mkdir(parents=True, exist_ok=True)
        # 既定 "ab" (レース毎 50/50 A/B, 2026-07-06)。明示の agentic/prefetch は尊重。
        research = (os.environ.get("KEIBA_NIGHTLY_RESEARCH") or "ab").strip() or "ab"
        cmd = build_shobu_cmd(
            str(SHOBU_DIR / f"{date}.json"),
            date=date,
            race_type="all",
            claude_all=True,
            research=(research if research in ("agentic", "prefetch", "ab") else "ab"),
        )
        job = self._registry.new(label=f"shobu-nightly: {date}", cmd=cmd)
        await job.start()
        self.launches += 1
        self.last_job_id = job.id
        self.last_launched_date = date
        try:   # 実行済みガードを永続 (reload/再起動で二重スキャンしない)
            tmp = self.STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "date": date, "job_id": job.id,
                "launched_at": self._now_jst().isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.STATE_FILE)
        except OSError:
            pass

    async def _loop(self) -> None:
        first = True
        while True:
            try:
                await asyncio.sleep(30 if first else self.interval_sec)
            except asyncio.CancelledError:
                raise
            first = False
            try:
                date = self._due()
                if date:
                    await self._launch(date)
                self.last_run_at = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ループは止めない
                pass


NIGHTLY_PRESCANNER = ShobuNightlyPrescanner(JOBS)


class ShobuDailyCatchupScanner:
    """make api 常駐: **当日朝に当日の勝負レースを自動スキャン** する (2026-07-06)。

    台帳の蓄積が ~9R/日しかない主因の一つが「当日スキャンは手動ボタン依存」だったのを解消する
    (nightly prescanner は前夜21時に**翌日**を 1 回スキャンするだけで、その時刻に make api が
    落ちていた日・前夜に出馬表/オッズが無かった場のレースは当日誰かがボタンを押すまで台帳に
    乗らない)。毎日 `KEIBA_DAILY_SCAN_HOUR` (JST, 既定 9・6-15 にクランプ) 以降の最初の tick で
    **当日** の shobu スキャン (race_type=all・claude_all=True) を Job として起動する:
      - 前夜 nightly が走っていれば指数は再利用 (2026-06-24 機構) されるので当日 Claude コストは
        ほぼゼロ。このスキャンの仕事は当日オッズの付与 + 前夜に無かった場/レースの取り込み。
      - JRA 開催日 (週末) が自動で計測対象に入るのが主目的の一つ (JRA は 9R しか台帳に無い)。

    **オッズ発売までリトライする (2026-07-06 実障害の修正)**: 初版は「1日1回」だったが、
    初日 (2026-07-06) 09:02 の実機で **NAR のオッズ発売前** にスキャンし、全 subprocess が
    odds_empty 早期 return (snapshot 無し・数秒で終了) → `.llm.json` は前夜分があるので
    36/36「生成成功」→ 推奨0件 → 以後どの常駐ループも対象にせず (refresh/再score は推奨のみ)、
    **当日の台帳が空のまま確定**する事故を確認した。よって「発火は1回」でなく
    **「当日ファイルの snapshot 被覆 (summary.with_snapshot/evaluated) が
    `DONE_SNAPSHOT_RATIO` (0.5) に達するまで、`KEIBA_DAILY_SCAN_RETRY_MIN` (既定60分) 間隔で
    最大 `KEIBA_DAILY_SCAN_MAX_ATTEMPTS` (既定8) 回」**発火する。間隔判定は当日ファイルの
    mtime 基準なので手動スキャン直後は自然に待つ。前回 Job が実行中なら発火しない。
    無効化は env `KEIBA_DAILY_SCAN=0`。リサーチ方式は env `KEIBA_DAILY_RESEARCH`
    (既定 "ab" = レース毎 50/50 A/B)。試行回数ガードは `SHOBU_DIR/daily_scan_state.json` に
    永続 (uvicorn --reload 耐性、旧形式 {date,job_id} は attempts=1 として読む)。
    """

    STATE_FILE = SHOBU_DIR / "daily_scan_state.json"
    # 当日ファイルの snapshot 被覆がこれ以上なら「オッズ付与済」= 完了。0.9 な理由 (2026-07-06):
    # 0.5 だと 3場開催 (昼12R+ナイター24R) で昼+夕方の2場が付いた時点 (24/36=67%) で打ち切り、
    # 発売が最も遅い場 (実機: 帯広ばんえい) の 12R が永久に台帳へ載らない。
    DONE_SNAPSHOT_RATIO = 0.9

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry
        self._task: asyncio.Task | None = None
        self.interval_sec = 300
        self.enabled = (os.environ.get("KEIBA_DAILY_SCAN") or "1").strip() != "0"
        try:
            h = int((os.environ.get("KEIBA_DAILY_SCAN_HOUR") or "9").strip())
        except ValueError:
            h = 9
        self.hour = min(15, max(6, h))
        try:
            rm = int((os.environ.get("KEIBA_DAILY_SCAN_RETRY_MIN") or "60").strip())
        except ValueError:
            rm = 60
        self.retry_sec = min(240, max(15, rm)) * 60
        try:
            ma = int((os.environ.get("KEIBA_DAILY_SCAN_MAX_ATTEMPTS") or "8").strip())
        except ValueError:
            ma = 8
        self.max_attempts = min(16, max(1, ma))
        self.launches = 0
        self.last_job_id: str | None = None
        self.last_launched_date: str | None = None
        self.last_run_at: float | None = None

    def status(self) -> dict[str, Any]:
        st = self._state()
        today = self._now_jst().strftime("%Y%m%d")
        return {
            "enabled": self.enabled,
            "hour_jst": self.hour,
            "retry_min": self.retry_sec // 60,
            "max_attempts": self.max_attempts,
            "attempts_today": st.get("attempts", 1) if st.get("date") == today else 0,
            "loop_running": self._task is not None and not self._task.done(),
            "launches": self.launches,
            "last_job_id": self.last_job_id,
            "last_launched_date": self.last_launched_date,
            "last_run_at": self.last_run_at,
        }

    def start(self) -> None:
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _now_jst() -> "datetime.datetime":
        import datetime
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Tokyo"))

    def _state(self) -> dict[str, Any]:
        try:
            st = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
            return st if isinstance(st, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _due(self, now: "datetime.datetime | None" = None) -> str | None:
        """発火すべきなら当日日付 (YYYYMMDD) を返す。

        None になる条件: 無効 / 設定時刻前 / 試行上限到達 / 前回 Job 実行中 /
        当日ファイルの snapshot 被覆が DONE_SNAPSHOT_RATIO 以上 (=オッズ付与済で完了) /
        直近スキャン (手動含む・mtime 基準) から retry 間隔が経っていない。
        """
        if not self.enabled:
            return None
        n = now or self._now_jst()
        if n.hour < self.hour:
            return None
        date = n.strftime("%Y%m%d")
        st = self._state()
        attempts = int(st.get("attempts") or 1) if st.get("date") == date else 0
        if attempts >= self.max_attempts:
            return None
        if st.get("date") == date and st.get("job_id"):
            job = self._registry.get(str(st["job_id"]))
            if job is not None and job.status in ("pending", "running"):
                return None   # 前回スキャンがまだ走っている
        path = SHOBU_DIR / f"{date}.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return date   # 当日ファイル無し → 初回発火
        try:
            summ = (json.loads(path.read_text(encoding="utf-8")) or {}).get("summary") or {}
            evaluated = int(summ.get("evaluated") or 0)
            with_snap = int(summ.get("with_snapshot") or 0)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            evaluated, with_snap = 0, 0
        if evaluated > 0 and with_snap / evaluated >= self.DONE_SNAPSHOT_RATIO:
            return None   # オッズ付与済 (snapshot 被覆十分) = 当日の仕事完了
        if (n.timestamp() - mtime) < self.retry_sec:
            return None   # 直近にスキャン済 (オッズ発売待ち) → 間隔を空ける
        return date

    async def _launch(self, date: str) -> None:
        SHOBU_DIR.mkdir(parents=True, exist_ok=True)
        research = (os.environ.get("KEIBA_DAILY_RESEARCH") or "ab").strip() or "ab"
        cmd = build_shobu_cmd(
            str(SHOBU_DIR / f"{date}.json"),
            date=date,
            race_type="all",
            claude_all=True,
            research=(research if research in ("agentic", "prefetch", "ab") else "ab"),
        )
        st = self._state()
        attempts = int(st.get("attempts") or 1) if st.get("date") == date else 0
        job = self._registry.new(label=f"shobu-daily: {date} (#{attempts + 1})", cmd=cmd)
        await job.start()
        self.launches += 1
        self.last_job_id = job.id
        self.last_launched_date = date
        try:   # 試行回数ガードを永続 (reload/再起動で二重スキャンしない)
            tmp = self.STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "date": date, "job_id": job.id, "attempts": attempts + 1,
                "launched_at": self._now_jst().isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.STATE_FILE)
        except OSError:
            pass

    async def _loop(self) -> None:
        first = True
        while True:
            try:
                await asyncio.sleep(45 if first else self.interval_sec)
            except asyncio.CancelledError:
                raise
            first = False
            try:
                date = self._due()
                if date:
                    await self._launch(date)
                self.last_run_at = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ループは止めない
                pass


DAILY_SCANNER = ShobuDailyCatchupScanner(JOBS)


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
    """履歴のレースを今すぐ **最新オッズだけ** 取得して snapshot を更新する (stage="score")。

    **Claude は呼ばない (ユーザ指示 2026-06-20)**: `--no-llm` を付けて score 段の claude -p を
    スキップし、fresh odds の取得 + 市場指数/市場乖離の再計算 + snapshot 保存のみ行う。過去に
    score 段で生成済みの Claude 指数キャッシュ (`<race_id>.llm.json`) は `_load_llm_scores` が
    再読込するので、**指数を呼び直さずに保持したまま**オッズ起因のフィールドだけ最新化される。

    経路は snapshot の odds_source (欠落=netkeiba 経路)、netkeiba rid は内部 race_id から復元。
    --refresh は付けない (締切まで待たず即時取得)。Job を返すので進捗は /api/jobs/{id}/stream で見られる。"""
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
    # 全経路 --no-llm: 最新オッズ取得のみ (Claude 指数は呼ばずキャッシュを保持)。
    if odds_source == "keibago":
        cmd = [PY, "-m", "src.scrape_keibago", rid, "--snapshot", f"--start-at={start_at}", "--phase=score", "--no-llm"]
    elif odds_source == "jra":
        cmd = [PY, "-m", "src.scrape_jra", rid, "--snapshot", f"--start-at={start_at}", "--phase=score", "--no-llm"]
    elif odds_source == "oddspark":
        cmd = [PY, "-m", "src.scrape_oddspark", rid, "--snapshot", f"--start-at={start_at}", "--phase=score", "--no-llm"]
    elif odds_source in ("", "netkeiba"):
        # netkeiba 経路: rid から出馬表 URL を組む (JRA=race. / NAR=nar.)。即時 score (--refresh なし)。
        is_jra = rid[4:6] in {f"{i:02d}" for i in range(1, 11)}
        host = "race.netkeiba.com" if is_jra else "nar.netkeiba.com"
        url = f"https://{host}/race/shutuba.html?race_id={rid}"
        cmd = build_analyze_cmd(url, phase="score", no_llm=True)
    else:
        raise HTTPException(422, f"unknown odds_source: {odds_source}")
    # 既存の Claude 指数キャッシュ (.llm.json) を **古くても** 読み込ませる (age gate を実質無効化)。
    # --no-llm で claude は呼ばないが、_load_llm_scores が 30 分超を stale 扱いで落とすと指数が
    # 消えてしまうため、refresh-odds だけ age 上限を引き上げて指数を保持する。
    job = JOBS.new(label=f"refresh-odds: {race_id}", cmd=cmd,
                   env_extra={"KEIBA_LLM_SCORE_MAX_AGE_SEC": "86400"})
    await job.start()
    return job.to_dict()


# --- shobu (今日の勝負レース) ---

class ShobuScanRequest(BaseModel):
    # 評価日 (YYYYMMDD)。None なら当日 JST。
    date: str | None = None
    # 対象 (all / jra / nar=地方平地 / banei=帯広ばんえい)。Literal で値検証。
    # banei は別競技なので nar から分離 (確率モデルも ev.segment_of_rd で分離済)。
    race_type: Literal["all", "jra", "nar", "banei"] = "all"
    # 推奨判定は **基準B (市場との順位乖離) 単独** (ユーザ指示 2026-06-28: 基準A=強弱は廃止)。
    # 乖離馬の指数差フロア (claude−market ≥ これ。順位だけでなく数値の裏付け)。
    edge_margin: float = Field(default=3.0, ge=0, le=100)
    # 市場乖離スコア (Claude本命の市場順位ギャップ主軸) がこの値以上で勝負レース。
    edge_threshold: float = Field(default=25.0, ge=0, le=100)
    # 発走前のみ (締切前) を対象にするか。False で締切済も含む。
    upcoming_only: bool = True
    # ボタン押下で **全レースの Claude 指数を一括生成** するか (claude -p を一斉実行)。
    # 既定 True (ユーザ指示 2026-06-20: ボタンで一気に取得)。Claude 指数が無い発走前レースが対象。
    claude_all: bool = True
    # claude_all=False のとき、発走が近い順 N 件だけ score ステージで指数を新規生成 (0=しない)。
    claude_eval: int = Field(default=0, ge=0, le=50)
    # 評価レース数の上限。発走が近い (早い) 順に N 件だけ評価。None=全件。
    max_races: int | None = Field(default=None, ge=1, le=300)
    # Claude 指数一括生成の across-race 並列数 (= 同時にスクレイプ+score する **レース数**)。
    # **keiba.go.jp / JRA公式は 1 IP からの同時アクセスをレート制限**し、~20 並列だと odds が
    # 空で返る → KeibagoError → score subprocess が rc=1 で死に Claude 指数がつかない (2026-06-21
    # 実機確認: 21 並列スクレイプで全件 win 空 + 以後の sequential も一時ブロック)。なので across-race は
    # 低く保つ。「並列20」は下の llm_max_concurrent (claude -p 同時数=keiba.go.jp 非依存) が担い、
    # 各レースは score_parallel の per-race シャードで 20-wide の深い検索を維持する。
    claude_eval_parallel: int = Field(default=4, ge=1, le=50)
    # score 段の検索並列化 (KEIBA_SCORE_PARALLEL)。既定 ON。
    score_parallel: bool = True
    # 1馬あたり検索クエリ数 (KEIBA_SCORE_QUERIES_PER_HORSE)。ユーザ指示 (2026-06-28): 10。
    # 「頭数 × これ」クエリが流れる (並列パスは全シャードで被覆、単一セッションも同 env を尊重)。
    score_queries_per_horse: int = Field(default=10, ge=2, le=12)
    # claude -p 同時数上限 (KEIBA_LLM_MAX_CONCURRENT)。claude の並列は keiba.go.jp と無関係なので 20 維持。
    llm_max_concurrent: int = Field(default=20, ge=1, le=50)
    # Claude 指数を生成する claude -p のモデル (ユーザ指示 2026-07-05: opus/sonnet/haiku で
    # 指数の質・速度・コストが変わるか比較したい)。既定 opus (従来挙動と同じ)。
    model: Literal["opus", "sonnet", "haiku"] = "opus"
    # リサーチ方式 (ARCH-B, ユーザ指示 2026-07-05「固定クエリを自動で Tavily 検索できるか」):
    # agentic = Claude が MCP で対話的に検索 (従来) / prefetch = 固定テンプレクエリを Python が
    # Tavily API 直叩き → 採点 claude 1 回 (速い・輻輳なし。dossier 不可時は agentic に自動降格)。
    # ab = レース毎に決定論ハッシュで agentic/prefetch を 50/50 割当 (research_mode A/B 計測を
    # 自動蓄積する既定, 2026-07-06。prefetch の劣化検証が済むまでこの既定で比較データを貯める)。
    research: Literal["agentic", "prefetch", "ab"] = "ab"


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
        edge_margin=req.edge_margin,
        edge_threshold=req.edge_threshold,
        upcoming_only=req.upcoming_only,
        claude_all=req.claude_all,
        claude_eval=req.claude_eval,
        claude_eval_parallel=req.claude_eval_parallel,
        score_parallel=req.score_parallel,
        score_queries_per_horse=req.score_queries_per_horse,
        llm_max_concurrent=req.llm_max_concurrent,
        model=req.model,
        research=req.research,
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


class ShobuRefreshRequest(BaseModel):
    # 再採点する日付 (YYYYMMDD)。None なら当日 JST。
    date: str | None = None


@app.post("/api/shobu/refresh")
async def api_shobu_refresh(req: ShobuRefreshRequest) -> dict[str, Any]:
    """勝負レース (推奨) のみ最新オッズで再採点 (Claude 呼ばず単勝 1 fetch/レース)。

    勝負レースページを開いている間 2 分毎に叩く軽量更新。強弱 (基準A) と 市場乖離 (基準B=
    market_index を最新オッズで再計算) を recompute して勝負スコアを更新、スコア履歴に追記して
    前回比 (score_delta) 付きの更新済 ShobuResult を返す。スキャン結果が無ければ 404。
    discovery も Claude -p も呼ばない (= netkeiba 規制リスク無し・即時)。
    """
    date = (req.date or shobu_today_jst()).strip()
    import re as _re
    if not _re.fullmatch(r"\d{8}", date):
        raise HTTPException(400, f"invalid date (YYYYMMDD expected): {req.date}")
    from src.shobu import refresh_recommended  # 遅延 import (scrape は呼ばれる時のみ)
    doc = await asyncio.to_thread(refresh_recommended, date)
    if doc is None:
        raise HTTPException(404, "shobu result not found (まだスキャンしていません)")
    # get_shobu_result と同様、各レースに仮想購入の的中券種ラベルを付与して返す。
    return await asyncio.to_thread(attach_hit_labels, doc)


@app.get("/api/shobu/pnl")
def api_shobu_pnl(point_cost: int = 100, box_size: int = 5,
                  venue: Literal["nar", "jra", "banei"] | None = None) -> dict[str, Any]:
    """勝負レース専用の **仮想収支** (Claude 指数上位5頭の3連単 BOX を買ったと仮定)。

    recommended 勝負レースで Claude 指数上位 box_size 頭の3連単 BOX (5頭=60点) を組み、実際の
    1・2・3着が全て上位5頭内なら的中として trifecta 配当で収支集計 (ダッシュボードに表示)。
    `venue` ("nar"=地方 (ばんえい含む) / "jra"=中央) でダッシュボードを分離 (ユーザ指示 2026-07-05)。
    """
    return compute_shobu_pnl(point_cost=point_cost, box_size=box_size, venue=venue)


@app.get("/api/shobu/indexed-pnl")
def api_shobu_indexed_pnl(point_cost: int = 100, box_size: int = 5,
                          version: str | None = None,
                          venue: Literal["nar", "jra", "banei"] | None = None) -> dict[str, Any]:
    """**全 Claude 指数レース** (recommended に限らない) の仮想収支 (ユーザ指示 2026-06-28)。

    全出走馬に Claude 指数が付いて結果が確定したレースを上位 box_size 頭の3連単 BOX で集計。
    勝負レース(推奨)収支 (/api/shobu/pnl) とは別カードでダッシュボードに併記する全数指標。
    `version` ("v1"/"v2"/"v3"/"β") で Claude 指数バージョン毎に分離 (ユーザ指示 2026-06-30)。
    `venue` ("nar"/"jra") で 地方/中央 に分離 (ユーザ指示 2026-07-05)。
    """
    return compute_indexed_pnl(point_cost=point_cost, box_size=box_size, version=version,
                               venue=venue)


@app.get("/api/shobu/strategies-pnl")
def api_shobu_strategies_pnl(point_cost: int = 100,
                             version: str | None = None,
                             venue: Literal["nar", "jra", "banei"] | None = None) -> dict[str, Any]:
    """勝負レース (推奨) の **Claude 指数 単純戦略くらべ** 仮想収支 (ユーザ指示 2026-06-30)。

    各レースで win1(1位単勝) / place1,2,3(複勝) / quinella12(馬連) / wide12,13(ワイド) /
    exacta12(馬単) / trifecta123 / trio123 / trio1234box / wide123box を仮定し戦略ごとに収支集計。
    `version` ("v1"/"v2"/"v3"/"β") で Claude 指数バージョン毎に分離。
    `venue` ("nar"/"jra") で 地方/中央 に分離 (ユーザ指示 2026-07-05)。
    """
    return compute_shobu_strategies_pnl(point_cost=point_cost, version=version, venue=venue)


@app.get("/api/shobu/indexed-strategies-pnl")
def api_shobu_indexed_strategies_pnl(point_cost: int = 100,
                                     version: str | None = None,
                                     venue: Literal["nar", "jra", "banei"] | None = None) -> dict[str, Any]:
    """**shobu 評価レース全体** (recommended に限らない) の Claude 指数 単純戦略くらべ 仮想収支。

    ユーザ指示 (2026-06-30): 「単勝のみ・複勝のみ・指数1-2の馬連も計測して過去分全て表示」。
    母集団は BOX の indexed-pnl と揃える (shobu 評価レース全体) → ダッシュボードに併記。
    `version` ("v1"/"v2"/"v3"/"β") で Claude 指数バージョン毎に分離 (新しい版が上)。
    `venue` ("nar"/"jra") で 地方/中央 に分離 (ユーザ指示 2026-07-05)。
    """
    return compute_indexed_strategies_pnl(point_cost=point_cost, version=version, venue=venue)


@app.get("/api/shobu/venue-breakdown")
def api_shobu_venue_breakdown(point_cost: int = 100,
                              version: str | None = None) -> dict[str, Any]:
    """**競馬場 (venue) 毎の内訳** 仮想収支 (ユーザ指示 2026-06-30: 競馬場毎にカードで内訳)。

    BOX収支 + 戦略くらべ を venue で group 集計。市場由来 Claude 指数 (〜2026-06-21 19:04) は
    計測対象外。`version` ("v1"/"v2"/"v3"/"β") で Claude 指数バージョン毎に分離。
    """
    return compute_venue_breakdown(point_cost=point_cost, version=version)


@app.get("/api/shobu/market-agreement")
def api_shobu_market_agreement() -> dict[str, Any]:
    """**市場一致シグナル** の現在値 + 蓄積履歴 (ユーザ指示 2026-06-30)。

    Claude#1==市場1番人気 (一致) か否かで券種 ROI を分割し、差Δの bootstrap CI が 0 から
    離れる (=確証) まで結果取得ループが自動蓄積する。current=現在値・history=時系列。
    """
    from api.store import compute_market_agreement, market_agreement_history
    return {
        "current": compute_market_agreement(),
        "history": market_agreement_history(),
        "appends": RESULTS_AUTO.market_agreement_appends,
    }


@app.get("/api/shobu/signal-rules")
def api_shobu_signal_rules() -> dict[str, Any]:
    """**プレレジ済シグナルルール** の検証状況 + walk-forward ガードレール (2026-07-05)。

    研究中シグナルで見つけたルールを定義固定 (プレレジ) し、**登録日以降のレースのみ** の
    ROI + bootstrap CI で 確証★ (CI下限>1.0) / 破綻 (CI上限<1.0) を自動判定する。
    walkforward は買い方マトリクスの best セルをそのまま追従した場合の正直な成績
    (look-ahead なし) — これが 100% を割る限りセル追従は機能していない、の誤用ガード。
    """
    from api.store import compute_signal_rules, signal_rules_history
    return {
        "current": compute_signal_rules(),
        "history": signal_rules_history(),
        "appends": RESULTS_AUTO.signal_rules_appends,
    }


@app.get("/api/results/auto")
def api_results_auto_status() -> dict[str, Any]:
    """予測分析履歴の結果 自動取得ループの状態 (interval / 次回・前回実行 / 直近サマリ)。

    make api 稼働中は既定 10 分毎に**発走済の全予測** (日付不問) を pending に enqueue →
    process_pending で確定結果を取得し、calibrate / 予測分析履歴 / ダッシュボードに反映する
    (watch-auto 非依存)。
    """
    return RESULTS_AUTO.status()


@app.get("/api/shobu/paddock-rescore")
def api_shobu_paddock_rescore_status() -> dict[str, Any]:
    """勝負レース(推奨)の **締切5-7分前 自動再score (パドック込み)** ループの状態 (ユーザ指示 2026-06-30)。

    make api 稼働中は 1 分毎に当日の推奨レースを見て、締切 5-7 分前に入ったものを score 段で
    再生成 (Claude 指数をパドック評価込みで更新 → 乖離/市場一致シグナルも自動反映)。実弾投票はしない。
    """
    return SHOBU_RESCORER.status()


@app.get("/api/shobu/daily")
def api_shobu_daily_status() -> dict[str, Any]:
    """**当日朝の自動キャッチアップスキャン** ループの状態 (2026-07-06)。

    make api 稼働中、毎日 KEIBA_DAILY_SCAN_HOUR (既定 9時 JST) 以降に当日の shobu スキャン
    (全レース claude_all) を 1 回だけ Job 起動する。当日 06:00 JST 以降に既にスキャン済
    (手動含む) なら skip。KEIBA_DAILY_SCAN=0 で無効化。
    """
    return DAILY_SCANNER.status()


@app.get("/api/shobu/nightly")
def api_shobu_nightly_status() -> dict[str, Any]:
    """**前日夜間の翌日一括解析** ループの状態 (ユーザ指示 2026-07-05)。

    make api 稼働中、毎晩 KEIBA_NIGHTLY_SCAN_HOUR (既定 21時 JST) 以降に翌日の shobu スキャン
    (全レース claude_all) を 1 回だけ Job 起動する。KEIBA_NIGHTLY_SCAN=0 で無効化。
    """
    return NIGHTLY_PRESCANNER.status()


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
