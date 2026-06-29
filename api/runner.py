"""subprocess を asyncio で管理する Job ランナー。

- `Job` は単一の python -m src.analyze プロセス。
- 出力は逐次バッファに溜め、SSE で配信。
- `WatchAutoManager` は make watch-auto 相当の永続プロセスを管理 (シングルトン)。

実装ノート:
- subprocess は `start_new_session=True` で新しいプロセスグループに分離する。
- cancel は `killpg(pgid, SIGINT → SIGTERM → SIGKILL)` で **グループ全体** を倒す。
  これをしないと bash の while ループから孫の python 子プロセスにシグナルが届かず、
  停止ボタンを押しても auto_watch が生き残るバグになる。
- FastAPI shutdown 時には登録された全ジョブをまとめて止める (uvicorn --reload で
  オーファン化するのを防ぐ)。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
if not Path(PY).exists():
    PY = sys.executable  # fallback

# bet 発火の既定 lead (秒)。src/auto_watch.BET_LEAD_SEC_DEFAULT と同値を保つこと
# (auto_watch を import すると uvicorn に重依存が乗るため数値を複製)。
# 2026-06-11: 60 → 150 統一の取り漏らし (runner に 60 が残り、旧 persisted state 経由で
# 「締切超過 enqueue」構成が復活し得た) の修正。
BET_LEAD_SEC_DEFAULT = 150


def _migrate_lead(v) -> int:
    """persisted config の bet_lead_sec を読む。旧既定 60 は legacy 値として 150 に migrate
    (60 は実測で締切に間に合わない構成 — ユーザが意図的に 60 を選んだ可能性より、旧既定の
    残骸である可能性が圧倒的に高い)。それ以外の明示値は尊重する。"""
    try:
        lead = int(v)
    except (TypeError, ValueError):
        return BET_LEAD_SEC_DEFAULT
    return BET_LEAD_SEC_DEFAULT if lead == 60 else lead


# watch-auto の「動いていた / 動かしたい」状態を永続化するファイル。
# lifespan startup で読んで auto-resume する。ローカル運用なのでファイル直書き。
WATCH_STATE_FILE = ROOT / "data" / "cache" / "watch_auto_state.json"

# プロセス追跡 (shutdown hook で一括停止するため)。Job._alive_jobs に弱参照は使わず、
# 終了時に手動で discard する。
_ALIVE_JOBS: "set[Job]" = set()


def _per_race_cap(bet_bundle: str, ev_bankroll: int, trifecta_bankroll: int,
                  stake_multiplier: float, max_stake_multiplier: float | None) -> int:
    """投票 daemon の per-race 上限 (--max-stake, 円)。

    基準は**実際に束を組む予算** (ev=EV束なら ev_bankroll / trifecta なら trifecta_bankroll、
    最低 ¥10,000 = 旧基準) × 倍率 (上限専用倍率があればそれ、無ければ掛金倍率≥1)。
    旧来は daemon 側の基準が ¥10,000 固定で、ev_bankroll を上げると束合計が上限を超え
    全 req が「投入しない」になっていた (2026-06-11 bughunt #5)。
    """
    base = max(10_000, int(ev_bankroll if bet_bundle == "ev" else trifecta_bankroll))
    if max_stake_multiplier is not None and max_stake_multiplier > 0:
        mult = float(max_stake_multiplier)
    else:
        mult = max(1.0, float(stake_multiplier or 1.0))
    return max(100, int(round(base * mult / 100.0)) * 100)


def _child_preexec() -> None:
    """子プロセスの fork 後 / exec 前に呼ばれる初期化。

    1. 新セッション (= 新プロセスグループ) を作る → killpg で一括停止可能。
    2. Linux の PR_SET_PDEATHSIG=SIGKILL を設定 → 親 (uvicorn) が死んだ瞬間、
       カーネルが直の子 (bash) に SIGKILL を送る。
       これがないと uvicorn --reload で再起動時、bash + python がオーファン化する。
    """
    os.setsid()
    if sys.platform == "linux":
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        except Exception:
            # ベストエフォート。失敗しても致命的ではない (UI 停止ボタンと
            # lifespan shutdown でカバーできる)。
            pass


class Job:
    """analyze / refresh のワンショット実行。"""

    def __init__(self, job_id: str, label: str, cmd: list[str],
                 env_extra: dict[str, str] | None = None) -> None:
        self.id = job_id
        self.label = label
        self.cmd = cmd
        # この Job だけに効かせる追加 env (例: score タブの KEIBA_SCORE_* チューニング)。
        # os.environ をいじらないので uvicorn プロセスや他 Job に漏れない (per-job 隔離)。
        self._env_extra = dict(env_extra or {})
        self.lines: deque[dict[str, Any]] = deque(maxlen=4000)
        # `seq` は単調増加するカウンタ。deque が maxlen を超えて古い entry を
        # evict しても seq は更新し続ける (旧実装は len(self.lines) を使って
        # おり 4000 件で頭打ちになり stream の since= 比較が壊れていた)。
        self._seq_counter: int = 0
        self.status: str = "pending"  # pending / running / done / failed / cancelled
        self.return_code: int | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._cond = asyncio.Condition()
        self._pgid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "return_code": self.return_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "line_count": len(self.lines),
        }

    async def start(self) -> None:
        self.status = "running"
        self.started_at = time.time()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["FORCE_COLOR"] = "0"
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        if self._env_extra:
            env.update(self._env_extra)  # per-job override (score タブのチューニング等)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                cwd=str(ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                # StreamReader の行バッファ上限 (既定 64KB)。子の 1 行が超えると readline が
                # ValueError → 未読 pipe で wait() が永久に返らず Job が running 固定になる
                # (2026-06-11 bughunt #9)。1MB に拡大 + _pump 側でチャンク読み退避。
                limit=2 ** 20,
                # setsid + PR_SET_PDEATHSIG: グループで殺せて、親死亡時にも安全。
                preexec_fn=_child_preexec,
            )
        except Exception as e:
            self.status = "failed"
            self.return_code = -1
            self.finished_at = time.time()
            await self._append({"stream": "system", "text": f"failed to spawn: {e}"})
            return
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except (ProcessLookupError, OSError):
            self._pgid = None
        _ALIVE_JOBS.add(self)
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                try:
                    raw = await self._proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # 1 行が limit 超 — 行として読めないのでチャンクで読み捨てて続行
                    # (ここで抜けると未読 pipe で wait() が固まり Job が running 固定になる)。
                    raw = await self._proc.stdout.read(65536)
                    if not raw:
                        break
                    await self._append({"stream": "stdout",
                                        "text": f"[line>limit を {len(raw)}B 切詰め] "
                                                + raw[:500].decode("utf-8", "replace")})
                    continue
                if not raw:
                    break
                try:
                    text = raw.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    text = repr(raw)
                await self._append({"stream": "stdout", "text": text})
        finally:
            # wait 前に残データを drain する (未読データで transport が paused のままだと
            # pipes disconnected にならず wait() が永久に返らない)。
            try:
                while True:
                    leftover = await asyncio.wait_for(self._proc.stdout.read(65536), timeout=5)
                    if not leftover:
                        break
            except (asyncio.TimeoutError, ValueError, Exception):  # noqa: BLE001
                pass
            await self._proc.wait()
            self.return_code = self._proc.returncode
            # cancel 経由で止まった場合は status を上書きしない
            if self.status == "running":
                self.status = "done" if self.return_code == 0 else "failed"
            self.finished_at = time.time()
            _ALIVE_JOBS.discard(self)
            await self._append(
                {"stream": "system", "text": f"[exit {self.return_code}]"}
            )

    async def _append(self, entry: dict[str, Any]) -> None:
        # seq は単調増加 (deque eviction の影響を受けない)。これで stream の
        # since=N 比較が長時間 job (>4000 lines) でも壊れない。
        seq = self._seq_counter
        self._seq_counter += 1
        entry = {"seq": seq, "ts": time.time(), **entry}
        self.lines.append(entry)
        async with self._cond:
            self._cond.notify_all()

    def _signal_group(self, sig: int) -> None:
        """プロセスグループ全体にシグナルを送る。失敗は無視。"""
        if self._pgid is None:
            return
        try:
            os.killpg(self._pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    async def cancel(self) -> None:
        if not (self._proc and self._proc.returncode is None):
            self.status = "cancelled" if self.status == "running" else self.status
            return
        # status を先に倒して、_pump が finished 時に "running" 判定で上書きするのを防ぐ
        self.status = "cancelled"
        # SIGINT → SIGTERM → SIGKILL を group 単位でエスカレーション
        self._signal_group(signal.SIGINT)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=3)
            return
        except asyncio.TimeoutError:
            pass
        self._signal_group(signal.SIGKILL)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass

    async def stream(self, since: int = 0) -> AsyncIterator[dict[str, Any]]:
        """seq>=since のログをリアルタイム配信。プロセス終了まで継続。"""
        idx = since
        while True:
            snapshot = list(self.lines)
            # SSE 切断 → 再接続時、要求 idx より前の seq が deque eviction で
            # 消えている場合がある (lines は maxlen=4000)。
            # silent に skip すると consumer は欠落に気付けないので警告を流す。
            if snapshot and snapshot[0]["seq"] > idx:
                dropped = snapshot[0]["seq"] - idx
                yield {
                    "seq": idx,
                    "ts": time.time(),
                    "stream": "system",
                    "text": f"[dropped {dropped} old lines (buffer overflow, seq {idx}..{snapshot[0]['seq']-1})]",
                }
                idx = snapshot[0]["seq"]
            new = [e for e in snapshot if e["seq"] >= idx]
            for e in new:
                yield e
                idx = e["seq"] + 1
            if self.status not in ("pending", "running"):
                # 終了済みなら残りを流して終了
                snapshot = list(self.lines)
                new = [e for e in snapshot if e["seq"] >= idx]
                for e in new:
                    yield e
                return
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass


async def shutdown_all_jobs() -> None:
    """FastAPI shutdown 時に呼ぶ。生き残っているジョブを並列で**強制終了**する。
    通常の cancel と違い、SIGINT を待たず即 SIGTERM → 2s → SIGKILL。
    uvicorn の graceful shutdown 時間が短い (デフォルト数秒) ため、
    丁寧にやっていると uvicorn 側がタイムアウトして孤児が残る。
    """
    jobs = [j for j in list(_ALIVE_JOBS) if j._proc and j._proc.returncode is None]
    if not jobs:
        return

    async def _fast_kill(j: "Job") -> None:
        j.status = "cancelled"
        j._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(j._proc.wait(), timeout=2.0)  # type: ignore[union-attr]
            return
        except asyncio.TimeoutError:
            pass
        j._signal_group(signal.SIGKILL)
        try:
            await asyncio.wait_for(j._proc.wait(), timeout=1.0)  # type: ignore[union-attr]
        except asyncio.TimeoutError:
            pass

    await asyncio.gather(*(_fast_kill(j) for j in jobs), return_exceptions=True)


class JobRegistry:
    """ジョブを保持するレジストリ。

    終了済 (done/failed/cancelled) ジョブを最大 MAX_TERMINAL 件まで保持し、
    超えた古い順から evict する。これがないと長時間稼働中に Job (deque 数 MB) が
    永久に貯まり container OOM する。実行中ジョブは evict 対象外。
    """

    MAX_TERMINAL = 100

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def new(self, label: str, cmd: list[str], env_extra: dict[str, str] | None = None) -> Job:
        job = Job(job_id=str(uuid.uuid4()), label=label, cmd=cmd, env_extra=env_extra)
        self._jobs[job.id] = job
        self._evict_old_terminal()
        return job

    def add(self, job: Job) -> None:
        self._jobs[job.id] = job
        self._evict_old_terminal()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._jobs.values())
        items.sort(key=lambda j: j.started_at or 0, reverse=True)
        return [j.to_dict() for j in items[:limit]]

    def _evict_old_terminal(self) -> None:
        """終了済ジョブを finished_at 昇順に並べて MAX_TERMINAL を超えた分を捨てる。"""
        terminals = [
            j for j in self._jobs.values()
            if j.status in ("done", "failed", "cancelled")
        ]
        if len(terminals) <= self.MAX_TERMINAL:
            return
        terminals.sort(key=lambda j: j.finished_at or 0)
        for j in terminals[: len(terminals) - self.MAX_TERMINAL]:
            self._jobs.pop(j.id, None)


def build_analyze_cmd(
    url: str,
    *,
    refresh: bool = False,
    no_llm: bool = False,
    llm_model: str = "opus",
    ev_max: float | None = None,
    min_prob: float | None = None,
    market_blend: float | None = None,
    aptitude_top: int | None = None,
    with_exacta: bool = False,
    with_trio: bool = False,
    phase: str = "bet",
) -> list[str]:
    cmd = [PY, "-m", "src.analyze", url, "--llm-model", llm_model]
    # phase=score = Claude 指数を出して暫定 snapshot を保存するだけ (束選定・実弾なし)。
    # bet (既定) = 指数+市場で P→束→確定 snapshot。bet は src.analyze の既定なので明示不要。
    if phase == "score":
        cmd += ["--phase", "score"]
    if refresh:
        cmd.append("--refresh")
    if no_llm:
        cmd.append("--no-llm")
    if ev_max is not None:
        cmd += ["--ev-max", str(ev_max)]
    if min_prob is not None:
        cmd += ["--min-prob", str(min_prob)]
    if market_blend is not None:
        cmd += ["--market-blend", str(market_blend)]
    if aptitude_top is not None:
        cmd += ["--aptitude-top", str(aptitude_top)]
    if with_exacta:
        cmd.append("--with-exacta")
    if with_trio:
        cmd.append("--with-trio")
    return cmd


def build_shobu_cmd(
    out_path: str,
    *,
    date: str | None = None,
    race_type: str = "all",
    edge_margin: float = 3.0,
    edge_threshold: float = 25.0,
    upcoming_only: bool = True,
    claude_all: bool = False,
    claude_eval: int = 0,
    claude_eval_parallel: int = 4,
    score_parallel: bool = False,
    score_queries_per_horse: int | None = None,
    llm_max_concurrent: int | None = None,
    max_races: int | None = None,
) -> list[str]:
    """`python -m src.shobu` (今日の勝負レース スキャン) コマンドを組む。結果は out_path に書かれる。

    判定は基準B (市場との順位乖離) 単独 (ユーザ指示 2026-06-28: 基準A=強弱は廃止)。
    """
    cmd = [
        PY, "-m", "src.shobu",
        "--out", out_path,
        "--race-type", race_type,
        "--edge-margin", str(edge_margin),
        "--edge-threshold", str(edge_threshold),
        "--claude-eval", str(claude_eval),
    ]
    if date:
        cmd += ["--date", date]
    if not upcoming_only:
        cmd.append("--include-finished")
    if claude_all:
        cmd.append("--claude-all")
    if max_races is not None:
        cmd += ["--max-races", str(max_races)]
    cmd += ["--claude-eval-parallel", str(claude_eval_parallel)]
    # CLI 側は --score-parallel が既定 ON なので、False のときは明示的に打ち消す
    # (省略すると CLI 既定の ON が効いてしまう)。
    cmd.append("--score-parallel" if score_parallel else "--no-score-parallel")
    if score_queries_per_horse:
        cmd += ["--score-queries-per-horse", str(score_queries_per_horse)]
    if llm_max_concurrent:
        cmd += ["--llm-max-concurrent", str(llm_max_concurrent)]
    return cmd


class WatchAutoManager:
    """make watch-auto 相当の永続プロセスを 1 つだけ保持。

    uvicorn --reload 等で in-memory 状態が失われても、`WATCH_STATE_FILE` に
    should_run / config を書いておけば lifespan startup から `resume_if_needed()`
    で前回の設定で再起動できる。
    """

    def __init__(self, registry: "JobRegistry | None" = None) -> None:
        # 投票 daemon の Job を共有レジストリに登録するための参照 (main.py が JOBS を渡す)。
        # 登録すると /api/jobs/<id> でログを取得/stream でき、Web UI から daemon の
        # 「ブラウザでログインしてください」や X server エラーが見える (未登録だと無言だった)。
        self._registry = registry
        self.job: Job | None = None
        # オッズパーク投票 daemon (headful ブラウザ・人がログイン)。bet_oddspark=True の時だけ
        # 起動し、watch-auto 停止で一緒に倒す。watch loop は毎tick フレッシュ subprocess で
        # ブラウザを保持できないため、ブラウザはこの別の常駐 daemon プロセスが持つ。
        self.bet_job: Job | None = None
        # JRA 即PAT 投票 daemon (headful ブラウザ・人がログイン)。bet_ipat=True のとき起動。
        # oddspark (NAR) と独立に持てる (土日 JRA 開催日に JRA ブラウザも一緒に立てる用途)。
        self.ipat_bet_job: Job | None = None
        # 投票発火デーモン (bet_scheduler)。betting (oddspark/ipat) ON のとき起動。
        # watch poll とは独立に締切 bet_lead_sec 秒前に精密発火する。
        self.scheduler_job: Job | None = None
        # 前回使った設定を persist 済 state file から復元しておく。停止中 / API 再起動後
        # でも status.config に「前回値」が乗るので、frontend がそれを form の default に
        # 使える (watch-auto 設定パネルの prefill)。
        self._config: dict[str, Any] = _load_watch_state().get("config") or {}
        # start/stop/resume の concurrent 呼び出しで Job が二重 spawn → 孤児化
        # するのを防ぐ。POST /api/watch-auto/start を 2 ブラウザから同時クリックで
        # 起こる: POST_A が `await self.job.start()` で suspend している間に
        # POST_B が `self.running` check (status=pending のまま False) を通過し、
        # self.job を上書きするので POST_A の subprocess が stop() でも倒せない。
        self._lock: asyncio.Lock | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        # lazy 初期化: import 時に running event loop が無くても OK にする。
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    @property
    def bet_running(self) -> bool:
        return self.bet_job is not None and self.bet_job.status == "running"

    @property
    def ipat_bet_running(self) -> bool:
        return self.ipat_bet_job is not None and self.ipat_bet_job.status == "running"

    @property
    def scheduler_running(self) -> bool:
        return self.scheduler_job is not None and self.scheduler_job.status == "running"

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    async def start(
        self,
        *,
        window: float = 1,
        tolerance: float = 1.5,
        score_window: float = 5,
        score_tolerance: float = 2,
        llm_blend: float | None = None,
        bet_lead_sec: int = BET_LEAD_SEC_DEFAULT,
        interval_sec: int = 60,
        ev_max: float | None = None,
        min_prob: float | None = None,
        market_blend: float | None = None,
        aptitude_top: int | None = None,
        with_exacta: bool = False,
        with_trio: bool = False,
        no_llm: bool = False,
        active_hours: str = "09:00-23:45",
        bet_oddspark: bool = False,
        bet_auto_purchase: bool = False,
        bet_daily_cap: int = 50000,
        bet_stake_multiplier: float = 1.0,
        bet_max_stake_multiplier: float | None = None,
        bet_payment_method: str = "opcoin",
        bet_auto_login: bool = False,
        bet_ipat: bool = False,
        trifecta_bankroll: int = 10_000,
        bet_bundle: str = "ev",
        ev_bankroll: int = 5_000,
        score_parallel: bool = False,
        score_queries_per_horse: int = 6,
    ) -> Job:
        async with self._ensure_lock():
            return await self._start_locked(
                window=window, tolerance=tolerance,
                score_window=score_window, score_tolerance=score_tolerance,
                llm_blend=llm_blend, bet_lead_sec=bet_lead_sec, interval_sec=interval_sec,
                ev_max=ev_max, min_prob=min_prob, market_blend=market_blend,
                aptitude_top=aptitude_top, with_exacta=with_exacta,
                with_trio=with_trio, no_llm=no_llm, active_hours=active_hours,
                bet_oddspark=bet_oddspark,
                bet_auto_purchase=bet_auto_purchase,
                bet_daily_cap=bet_daily_cap,
                bet_stake_multiplier=bet_stake_multiplier,
                bet_max_stake_multiplier=bet_max_stake_multiplier,
                bet_payment_method=bet_payment_method,
                bet_auto_login=bet_auto_login,
                bet_ipat=bet_ipat,
                trifecta_bankroll=trifecta_bankroll,
                bet_bundle=bet_bundle,
                ev_bankroll=ev_bankroll,
                score_parallel=score_parallel,
                score_queries_per_horse=score_queries_per_horse,
            )

    async def _start_locked(
        self,
        *,
        window: float,
        tolerance: float,
        score_window: float = 5,
        score_tolerance: float = 2,
        llm_blend: float | None = None,
        bet_lead_sec: int = BET_LEAD_SEC_DEFAULT,
        interval_sec: int,
        ev_max: float | None,
        min_prob: float | None,
        market_blend: float | None,
        aptitude_top: int | None,
        with_exacta: bool,
        with_trio: bool,
        no_llm: bool = False,
        active_hours: str,
        bet_oddspark: bool = False,
        bet_auto_purchase: bool = False,
        bet_daily_cap: int = 50000,
        bet_stake_multiplier: float = 1.0,
        bet_max_stake_multiplier: float | None = None,
        bet_payment_method: str = "opcoin",
        bet_auto_login: bool = False,
        bet_ipat: bool = False,
        trifecta_bankroll: int = 10_000,
        bet_bundle: str = "ev",
        ev_bankroll: int = 5_000,
        score_parallel: bool = False,
        score_queries_per_horse: int = 6,
    ) -> Job:
        # 投票束/予算系の値検証 (env への適用は early-return 判定の**後** — 下記参照)。
        if bet_bundle not in ("ev", "trifecta"):
            bet_bundle = "ev"
        # daemon に渡す掛金倍率 (各脚 stake を ×N)。
        eff_stake_multiplier = bet_stake_multiplier
        # self.job が既に "running" なら早期 return。pending (spawn 中) も
        # 二重 spawn 防止のため return する。
        # ただし**投票ブラウザ daemon が死んでいれば貼り直す**: resume (startup の should_run)
        # でループが先に起動済の状態で「開始」を押す/ブラウザを閉じた後に再度「開始」を押した
        # ときに、early-return だけだと**ブラウザが出ない**ため (ユーザ報告: 開始してもブラウザが
        # 起動しない)。ループ稼働中でも要求された daemon が未稼働なら起動だけ補う。
        # 【重要 2026-06-10 bughunt】この貼り直し経路では **env (KEIBA_BET_BUNDLE 等) を
        # リクエスト値で書き換えない**。env は稼働中 watch loop が spawn 時に継承した値の
        # ままにしておくことで、貼り直した daemon/scheduler が loop と同じ束/予算を継承する。
        # 以前はここより前で env を書き換えており「loop=旧束 / 再起動 daemon=新束」に分裂し、
        # レースごとに実弾の束が非決定的に変わるバグだった。束/予算の変更は停止→開始のみ。
        if self.job is not None and self.job.status in ("pending", "running"):
            # 【重要 2026-06-10 bughunt #2】貼り直す daemon のパラメタも**リクエスト値でなく
            # persist 済み self._config の値**を使う (env と同じ規則。scheduler は元々 config
            # から読んでおり、daemon だけリクエスト値だと「loop=旧束 × daemon=新倍率/自動購入」
            # という誰も設定していない混成で実弾が動き、UI 表示 (config) とも矛盾していた)。
            # 設定変更は停止→開始のフル再起動のみで反映される。
            cfg = self._config or {}
            cfg_auto = (bool(cfg["bet_auto_purchase"]) if "bet_auto_purchase" in cfg
                        else bet_auto_purchase)
            cfg_cap = (int(cfg["bet_daily_cap"]) if cfg.get("bet_daily_cap") is not None
                       else bet_daily_cap)
            cfg_mult = (float(cfg["bet_stake_multiplier"])
                        if cfg.get("bet_stake_multiplier") is not None
                        else eff_stake_multiplier)
            cfg_max_mult = cfg.get("bet_max_stake_multiplier", bet_max_stake_multiplier)
            cfg_pay = (str(cfg["bet_payment_method"]) if cfg.get("bet_payment_method")
                       else bet_payment_method)
            cfg_login = (bool(cfg["bet_auto_login"]) if "bet_auto_login" in cfg
                         else bet_auto_login)
            # 【2026-06-11 bughunt #10】どの daemon を貼り直すかの toggle 自体も cfg 由来に
            # する (リクエスト値だと投票 OFF で稼働中に stale form から ON の start が来ると
            # 投票 daemon+scheduler が起動し、config/persist=OFF のまま実弾経路が動く)。
            cfg_oddspark = bool(cfg.get("bet_oddspark")) if cfg else bet_oddspark
            cfg_ipat = bool(cfg.get("bet_ipat")) if cfg else bet_ipat
            cfg_max_stake = _per_race_cap(
                str(cfg.get("bet_bundle") or "trifecta"),
                int(cfg.get("ev_bankroll") or 5_000),
                int(cfg.get("trifecta_bankroll") or 10_000),
                cfg_mult, cfg_max_mult)
            if cfg_oddspark and not self.bet_running:
                await self._start_betting_daemon(
                    auto_purchase=cfg_auto, daily_cap=cfg_cap,
                    stake_multiplier=cfg_mult,
                    max_stake_multiplier=cfg_max_mult,
                    max_stake=cfg_max_stake,
                    payment_method=cfg_pay, auto_login=cfg_login)
            if cfg_ipat and not self.ipat_bet_running:
                await self._start_ipat_daemon(
                    auto_purchase=cfg_auto, daily_cap=cfg_cap,
                    stake_multiplier=cfg_mult,
                    max_stake_multiplier=cfg_max_mult,
                    max_stake=cfg_max_stake,
                    auto_login=cfg_login)
            if not self.scheduler_running:   # 2026-06-11: paper でも常時併走 (上記参照)
                await self._start_scheduler(
                    bet_oddspark=cfg_oddspark, bet_ipat=cfg_ipat,
                    bet_lead_sec=_migrate_lead(self._config.get("bet_lead_sec")),
                    market_blend=self._config.get("market_blend"),
                    aptitude_top=self._config.get("aptitude_top"),
                    no_llm=bool(self._config.get("no_llm")),
                    llm_blend=self._config.get("llm_blend"))
            return self.job

        # フル開始経路でも legacy 60 を migrate する (2026-06-11 第5R): UI は表示中の
        # config 値をそのまま送り返すので、ここで migrate しないと「旧 state を一度
        # UI から再開始 → 60 が watch loop コマンドと persist に再生産」される
        # (scheduler 側だけ migrate されて発火タイミングが二重化する)。
        bet_lead_sec = _migrate_lead(bet_lead_sec)
        # ── フル開始経路: ここで初めて env をリクエスト値で確定する ──
        # 投票束の切替 (2026-06-10 復活): env KEIBA_BET_BUNDLE で auto_watch (enqueue 判定) と
        # 投票 daemon (oddspark/ipat, 同 env を継承) に伝播。ev=EV束 (既定) / trifecta=3連単束。
        os.environ["KEIBA_BET_BUNDLE"] = bet_bundle
        # EV束の1レース予算 (build_bundle の bankroll)。env で全 dispatch subprocess に伝播。
        os.environ["KEIBA_EV_BANKROLL"] = str(int(ev_bankroll))
        # 3連単の1レース購入予算は env で全 dispatch subprocess (analyze/keibago/jra/oddspark) に
        # 伝播する (_save_prediction_snapshot が _trifecta_bankroll で尊重)。束を組む時点の予算なので、
        # 投票倍率 (bet_stake_multiplier) とは別物。変更はループ再起動が必要 (spawn 時に env が固定)。
        os.environ["KEIBA_TRIFECTA_BANKROLL"] = str(int(trifecta_bankroll))
        # score ステージ (Claude 指数) の検索並列化と検索回数 (src/llm.py score_horses が尊重)。
        # _env_truthy は未設定=False なので、ON のときだけ "1" を立てる (OFF 時は積極的に消す)。
        if score_parallel:
            os.environ["KEIBA_SCORE_PARALLEL"] = "1"
        else:
            os.environ.pop("KEIBA_SCORE_PARALLEL", None)
        os.environ["KEIBA_SCORE_QUERIES_PER_HORSE"] = str(int(score_queries_per_horse))

        # Python ラッパで while ループを回す (api/_watch_loop.py)。
        # bash を挟むと SIGKILL 時に孫プロセスが孤児化するため。
        inner = [
            PY, "-m", "src.auto_watch",
            "--window", str(window),
            "--tolerance", str(tolerance),
            "--score-window", str(score_window),
            "--score-tolerance", str(score_tolerance),
            "--bet-lead-sec", str(bet_lead_sec),
            "--active-hours", active_hours,
        ]
        if llm_blend is not None:
            inner += ["--llm-blend", str(llm_blend)]
        if ev_max is not None:
            inner += ["--ev-max", str(ev_max)]
        if min_prob is not None:
            inner += ["--min-prob", str(min_prob)]
        if market_blend is not None:
            inner += ["--market-blend", str(market_blend)]
        if aptitude_top is not None:
            inner += ["--aptitude-top", str(aptitude_top)]
        if with_exacta:
            inner.append("--with-exacta")
        if with_trio:
            inner.append("--with-trio")
        if no_llm:
            inner.append("--no-llm")
        if bet_oddspark:
            inner.append("--bet-oddspark")
        if bet_ipat:
            inner.append("--bet-ipat")
        cmd = [
            PY, "-m", "api._watch_loop",
            "--interval", str(interval_sec),
            "--",
            *inner,
        ]
        self._config = {
            "window": window,
            "tolerance": tolerance,
            "score_window": score_window,
            "score_tolerance": score_tolerance,
            "llm_blend": llm_blend,
            "bet_lead_sec": bet_lead_sec,
            "active_hours": active_hours,
            "interval_sec": interval_sec,
            "ev_max": ev_max,
            "min_prob": min_prob,
            "market_blend": market_blend,
            "aptitude_top": aptitude_top,
            "with_exacta": with_exacta,
            "with_trio": with_trio,
            "no_llm": no_llm,
            "bet_oddspark": bet_oddspark,
            "bet_auto_purchase": bet_auto_purchase,
            "bet_daily_cap": bet_daily_cap,
            "bet_stake_multiplier": bet_stake_multiplier,
            "bet_max_stake_multiplier": bet_max_stake_multiplier,
            "bet_payment_method": bet_payment_method,
            "bet_auto_login": bet_auto_login,
            "bet_ipat": bet_ipat,
            "trifecta_bankroll": trifecta_bankroll,
            "bet_bundle": bet_bundle,
            "ev_bankroll": ev_bankroll,
            "score_parallel": score_parallel,
            "score_queries_per_horse": score_queries_per_horse,
        }
        self.job = Job(
            job_id=f"watch-auto-{int(time.time())}",
            label="watch-auto",
            cmd=cmd,
        )
        await self.job.start()
        # spawn 失敗 (status="failed") で should_run=True を persist してしまうと
        # 次回 lifespan startup の resume_if_needed が同じ broken cmd を起動し
        # 続ける無限ループになる。spawn が成功 (running/pending) の時のみ persist。
        if self.job.status in ("pending", "running"):
            _save_watch_state({"should_run": True, "config": self._config})
        # bet_oddspark なら投票 daemon (headful ブラウザ) を別 subprocess で起動。
        # watch loop は毎tick フレッシュ subprocess でブラウザを保持できないため、
        # ブラウザ常駐はこの daemon が担う。env 継承で DISPLAY が無いと headful 起動に失敗する。
        if bet_oddspark:
            # ここも `or 既定` を避ける (cap=0 等の意図的な値が消えるバグ防止)
            cfg = self._config
            await self._start_betting_daemon(
                auto_purchase=bool(cfg.get("bet_auto_purchase")),
                daily_cap=int(cfg["bet_daily_cap"])
                    if cfg.get("bet_daily_cap") is not None else 50000,
                # 投票束に対応する倍率 (束の各脚に掛かる倍率)。上で算出済の eff を使う。
                stake_multiplier=eff_stake_multiplier,
                max_stake_multiplier=cfg.get("bet_max_stake_multiplier"),
                # per-race 上限は実際の束予算に連動 (2026-06-11 bughunt #5)
                max_stake=_per_race_cap(bet_bundle, ev_bankroll, trifecta_bankroll,
                                        eff_stake_multiplier,
                                        cfg.get("bet_max_stake_multiplier")),
                payment_method=str(cfg["bet_payment_method"])
                    if cfg.get("bet_payment_method") else "opcoin",
                auto_login=bool(cfg.get("bet_auto_login")),
            )
        # bet_ipat なら JRA 即PAT 投票 daemon を別 subprocess で起動 (土日 JRA 開催日用)。
        if bet_ipat:
            cfg = self._config
            await self._start_ipat_daemon(
                auto_purchase=bool(cfg.get("bet_auto_purchase")),
                daily_cap=int(cfg["bet_daily_cap"])
                    if cfg.get("bet_daily_cap") is not None else 50000,
                # 投票束に対応する倍率 (束の各脚に掛かる倍率)。上で算出済の eff を使う。
                stake_multiplier=eff_stake_multiplier,
                max_stake_multiplier=cfg.get("bet_max_stake_multiplier"),
                # per-race 上限は実際の束予算に連動 (2026-06-11 bughunt #5)
                max_stake=_per_race_cap(bet_bundle, ev_bankroll, trifecta_bankroll,
                                        eff_stake_multiplier,
                                        cfg.get("bet_max_stake_multiplier")),
                auto_login=bool(cfg.get("bet_auto_login")),
            )
        # 投票発火デーモン (締切 bet_lead_sec 秒前に精密発火, watch poll とは独立)。
        # 【2026-06-11 bughunt #4】投票 OFF (paper 計測) でも常時併走させる:
        # scheduler 無しだと発火が watch tick (score dispatch 中央値 274s でブロック) に
        # 量子化され、6場開催日は ~22% のレースが予約破棄 = bet snapshot も result fetch
        # 予約も無く計測から無痕跡に消えていた (混雑時間帯に系統集中する選択バイアス)。
        # enqueue は bet フラグで別途ゲート済みなので、paper でも snapshot 保存+結果取得
        # のみで実弾は飛ばない。
        await self._start_scheduler(
            bet_oddspark=bet_oddspark, bet_ipat=bet_ipat,
            bet_lead_sec=_migrate_lead(self._config.get("bet_lead_sec")),
            market_blend=self._config.get("market_blend"),
            aptitude_top=self._config.get("aptitude_top"),
            no_llm=bool(self._config.get("no_llm")),
            llm_blend=self._config.get("llm_blend"),
        )
        return self.job

    async def _start_scheduler(self, *, bet_oddspark: bool, bet_ipat: bool,
                               bet_lead_sec: int = BET_LEAD_SEC_DEFAULT, market_blend=None,
                               aptitude_top=None, no_llm: bool = False,
                               llm_blend=None) -> None:
        """bet_scheduler (締切 bet_lead_sec 秒前に精密発火) を別 subprocess で起動。

        watch-auto は毎 tick フレッシュ subprocess なので精密タイマを持てない。発火だけを
        この常駐デーモンに分離し、poll/tick に縛られず締切1分前ちょうどに投票を撃つ。
        """
        if self.scheduler_job is not None and self.scheduler_job.status in ("pending", "running"):
            return
        cmd = [PY, "-m", "src.bet_scheduler", f"--bet-lead-sec={bet_lead_sec}"]
        if bet_oddspark:
            cmd.append("--bet-oddspark")
        if bet_ipat:
            cmd.append("--bet-ipat")
        if market_blend is not None:
            cmd.append(f"--market-blend={market_blend}")
        if aptitude_top is not None:
            cmd.append(f"--aptitude-top={aptitude_top}")
        if llm_blend is not None:
            cmd.append(f"--llm-blend={llm_blend}")
        if no_llm:
            cmd.append("--no-llm")
        self.scheduler_job = Job(
            job_id=f"bet-scheduler-{int(time.time())}",
            label="bet-scheduler",
            cmd=cmd,
        )
        if self._registry is not None:
            self._registry.add(self.scheduler_job)
        await self.scheduler_job.start()

    async def _start_ipat_daemon(self, *, auto_purchase: bool = False,
                                 daily_cap: int = 50000,
                                 stake_multiplier: float = 1.0,
                                 max_stake_multiplier: float | None = None,
                                 max_stake: int | None = None,
                                 auto_login: bool = False) -> None:
        """JRA 即PAT 投票 daemon (`ipat_bet --session`) を起動。oddspark daemon の JRA 版。

        max_stake_multiplier: per-race 上限の専用倍率 (基準¥10,000×N)。None なら掛金倍率に連動。

        headful ブラウザを開き、ログイン → queue (ipat_bet_queue) を消費。
        - auto_login=False (既定): 人が headful ブラウザで手でログイン (poll 検出, 最も安全)。
        - auto_login=True: `--auto-login` を付け env 認証 (IPAT_INETID/SUBSCRIBER/PARS/PIN)。
          uvicorn (`make api`) の env に設定しておくこと (未設定だと daemon が起動直後に失敗)。
        - auto_purchase=False (既定): カート投入のみ、購入確定は人。True で実弾だが
          `AUTO_PURCHASE_VERIFIED=False` の間は src 側 fail-safe で実弾を撃たない。
        DISPLAY 継承のため `make api` を DISPLAY のある端末で起動しておくこと。
        """
        if self.ipat_bet_job is not None and self.ipat_bet_job.status in ("pending", "running"):
            return
        cmd = [PY, "-m", "src.ipat_bet", "--session", f"--daily-cap={daily_cap}"]
        if max_stake is not None and max_stake > 0:
            # per-race 上限を実際の束予算 (ev_bankroll/trifecta_bankroll) に連動させる
            # (2026-06-11 bughunt #5: 旧来は基準 ¥10,000 固定で、ev_bankroll>10,000 だと
            # 束合計が上限超過 → 全 req が「投入しない」で .done = 賭け逃しになっていた)。
            cmd.append(f"--max-stake={max_stake}")
        if auto_login:
            cmd.append("--auto-login")
        if auto_purchase:
            cmd.append("--auto-purchase")
        if stake_multiplier != 1.0:
            cmd.append(f"--stake-multiplier={stake_multiplier}")
        if max_stake_multiplier is not None and max_stake_multiplier > 0:
            cmd.append(f"--max-stake-multiplier={max_stake_multiplier}")
        label_extra = ""
        if auto_login:
            label_extra += " [auto-login]"
        if auto_purchase:
            label_extra += " [auto-purchase]"
        if stake_multiplier != 1.0:
            label_extra += f" [×{stake_multiplier}]"
        if max_stake_multiplier is not None and max_stake_multiplier > 0:
            label_extra += f" [上限×{max_stake_multiplier:g}]"
        self.ipat_bet_job = Job(
            job_id=f"ipat-session-{int(time.time())}",
            label="ipat-bet-session" + label_extra,
            cmd=cmd,
        )
        if self._registry is not None:
            self._registry.add(self.ipat_bet_job)   # /api/jobs でログ閲覧可に
        await self.ipat_bet_job.start()

    async def _start_betting_daemon(self, *, auto_purchase: bool = False,
                                    daily_cap: int = 50000,
                                    stake_multiplier: float = 1.0,
                                    max_stake_multiplier: float | None = None,
                                    max_stake: int | None = None,
                                    payment_method: str = "opcoin",
                                    auto_login: bool = False) -> None:
        """オッズパーク投票 daemon (`oddspark_bet --session`) を起動。

        max_stake_multiplier: per-race 上限の専用倍率 (基準¥10,000×N)。None なら掛金倍率に連動。

        headful ブラウザを開き、ログイン → queue を消費。
        - auto_login=False (既定): 人が headful ブラウザで手でログイン (poll 検出, 最も安全)。
        - auto_login=True: `--auto-login` を付け、env 認証 (`ODDSPARK_ID`/`ODDSPARK_PASSWORD`/
          `ODDSPARK_PIN`) で自動ログイン。**uvicorn (`make api`) の env にこれらを設定しておくこと**
          (未設定だと daemon が起動直後に失敗 → ライブログにエラー)。認証情報はコード/ログに残さない。
        - auto_purchase=False (既定): カート投入のみ、購入確定は人。
        - auto_purchase=True (実弾): #gotobuy → 確認 → 確定 まで自動。daily_cap で日次上限ガード。
          (`AUTO_PURCHASE_VERIFIED=False` の間は src 側で fail-safe で実弾を撃たない。)
        uvicorn の env (DISPLAY) を継承するので `make api` を DISPLAY のある端末で起動していれば
        ブラウザが画面に出る。
        """
        if self.bet_job is not None and self.bet_job.status in ("pending", "running"):
            return
        cmd = [PY, "-m", "src.oddspark_bet", "--session", f"--daily-cap={daily_cap}",
               f"--payment={payment_method}"]
        if max_stake is not None and max_stake > 0:
            # per-race 上限を実際の束予算に連動 (2026-06-11 bughunt #5 — ipat 側と同じ)
            cmd.append(f"--max-stake={max_stake}")
        if auto_login:
            cmd.append("--auto-login")
        if auto_purchase:
            cmd.append("--auto-purchase")
        if stake_multiplier != 1.0:
            cmd.append(f"--stake-multiplier={stake_multiplier}")
        if max_stake_multiplier is not None and max_stake_multiplier > 0:
            cmd.append(f"--max-stake-multiplier={max_stake_multiplier}")
        label_extra = ""
        if auto_login:
            label_extra += " [auto-login]"
        if auto_purchase:
            label_extra += " [auto-purchase]"
        if stake_multiplier != 1.0:
            label_extra += f" [×{stake_multiplier}]"
        if max_stake_multiplier is not None and max_stake_multiplier > 0:
            label_extra += f" [上限×{max_stake_multiplier:g}]"
        if payment_method != "opcoin":
            label_extra += f" [{payment_method}]"
        self.bet_job = Job(
            job_id=f"oddspark-session-{int(time.time())}",
            label="oddspark-bet-session" + label_extra,
            cmd=cmd,
        )
        if self._registry is not None:
            self._registry.add(self.bet_job)   # /api/jobs でログ閲覧可に
        await self.bet_job.start()

    async def stop(self) -> None:
        async with self._ensure_lock():
            if self.scheduler_job:
                await self.scheduler_job.cancel()   # 投票発火デーモンも止める
            if self.bet_job:
                await self.bet_job.cancel()   # 投票ブラウザも一緒に閉じる
            if self.ipat_bet_job:
                await self.ipat_bet_job.cancel()   # JRA 即PAT ブラウザも閉じる
            if self.job:
                await self.job.cancel()
            _save_watch_state({"should_run": False, "config": self._config})

    async def resume_if_needed(self) -> Job | None:
        """lifespan startup から呼ぶ。should_run=true なら前回の config で再起動。"""
        state = _load_watch_state()
        if not state.get("should_run"):
            return None
        cfg = state.get("config") or {}
        try:
            return await self.start(
                window=float(cfg.get("window", 1)),
                tolerance=float(cfg.get("tolerance", 1.5)),
                score_window=float(cfg.get("score_window", 5)),
                score_tolerance=float(cfg.get("score_tolerance", 2)),
                llm_blend=(float(cfg["llm_blend"])
                           if cfg.get("llm_blend") is not None else None),
                bet_lead_sec=_migrate_lead(cfg.get("bet_lead_sec")),
                interval_sec=int(cfg.get("interval_sec", 60)),
                ev_max=cfg.get("ev_max"),
                min_prob=cfg.get("min_prob"),
                market_blend=cfg.get("market_blend"),
                aptitude_top=cfg.get("aptitude_top"),
                with_exacta=bool(cfg.get("with_exacta")),
                with_trio=bool(cfg.get("with_trio")),
                no_llm=bool(cfg.get("no_llm")),
                active_hours=cfg.get("active_hours", "09:00-23:45"),
                bet_oddspark=bool(cfg.get("bet_oddspark")),
                bet_auto_purchase=bool(cfg.get("bet_auto_purchase")),
                # `or default` だと意図的に 0 / 1.0 / "opcoin" を入れた場合に既定で上書き
                # されてしまう (例: cap=0 で無効化したつもりが 50000 に戻る)。None のときだけ既定。
                bet_daily_cap=int(cfg["bet_daily_cap"])
                    if cfg.get("bet_daily_cap") is not None else 50000,
                # 旧 state 互換: 旧トグル時代の cfg (bet_plan_t=True) は 3連単専用倍率を
                # 使っていたので、そちらを掛金倍率として引き継ぐ (束は今や常に3連単束)。
                bet_stake_multiplier=float(cfg["bet_plan_t_multiplier"])
                    if (cfg.get("bet_plan_t") and cfg.get("bet_plan_t_multiplier") is not None)
                    else float(cfg["bet_stake_multiplier"])
                    if cfg.get("bet_stake_multiplier") is not None else 1.0,
                bet_max_stake_multiplier=float(cfg["bet_max_stake_multiplier"])
                    if cfg.get("bet_max_stake_multiplier") is not None else None,
                bet_payment_method=str(cfg["bet_payment_method"])
                    if cfg.get("bet_payment_method") else "opcoin",
                bet_auto_login=bool(cfg.get("bet_auto_login")),
                bet_ipat=bool(cfg.get("bet_ipat")),
                # 旧 state 互換: 旧キー plan_t_bankroll で persist された予算も引き継ぐ。
                trifecta_bankroll=int(cfg["trifecta_bankroll"])
                    if cfg.get("trifecta_bankroll") is not None
                    else int(cfg["plan_t_bankroll"])
                    if cfg.get("plan_t_bankroll") is not None else 10_000,
                # 旧 state (bet_bundle キー無し) は旧挙動 = 3連単束を維持 (resume で投票束が
                # 黙って EV束に切り替わるのを防ぐ)。新規開始の既定は ev (main.py の Literal 既定)。
                bet_bundle=str(cfg["bet_bundle"])
                    if cfg.get("bet_bundle") else "trifecta",
                ev_bankroll=int(cfg["ev_bankroll"])
                    if cfg.get("ev_bankroll") is not None else 5_000,
                # 旧 state (キー無し) は score 並列 OFF / 検索 6 (= src/llm.py 既定) に倒す。
                score_parallel=bool(cfg.get("score_parallel")),
                score_queries_per_horse=int(cfg["score_queries_per_horse"])
                    if cfg.get("score_queries_per_horse") is not None else 6,
            )
        except Exception as e:  # noqa: BLE001 - startup なので拾って続行
            print(f"[WatchAutoManager.resume] failed: {e}", file=sys.stderr, flush=True)
            return None


def _load_watch_state() -> dict[str, Any]:
    """watch-auto の永続状態を読む。ファイル無し / 壊れていれば空 dict。"""
    if not WATCH_STATE_FILE.exists():
        return {}
    try:
        return json.loads(WATCH_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_watch_state(state: dict[str, Any]) -> None:
    """watch-auto の永続状態を書く。"""
    try:
        WATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WATCH_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[WatchAutoManager._save_watch_state] failed: {e}", file=sys.stderr, flush=True)
