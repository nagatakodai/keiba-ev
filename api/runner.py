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

# watch-auto の「動いていた / 動かしたい」状態を永続化するファイル。
# lifespan startup で読んで auto-resume する。ローカル運用なのでファイル直書き。
WATCH_STATE_FILE = ROOT / "data" / "cache" / "watch_auto_state.json"

# プロセス追跡 (shutdown hook で一括停止するため)。Job._alive_jobs に弱参照は使わず、
# 終了時に手動で discard する。
_ALIVE_JOBS: "set[Job]" = set()


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

    def __init__(self, job_id: str, label: str, cmd: list[str]) -> None:
        self.id = job_id
        self.label = label
        self.cmd = cmd
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
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                cwd=str(ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
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
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                try:
                    text = raw.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    text = repr(raw)
                await self._append({"stream": "stdout", "text": text})
        finally:
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

    def new(self, label: str, cmd: list[str]) -> Job:
        job = Job(job_id=str(uuid.uuid4()), label=label, cmd=cmd)
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
) -> list[str]:
    cmd = [PY, "-m", "src.analyze", url, "--llm-model", llm_model]
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


class WatchAutoManager:
    """make watch-auto 相当の永続プロセスを 1 つだけ保持。

    uvicorn --reload 等で in-memory 状態が失われても、`WATCH_STATE_FILE` に
    should_run / config を書いておけば lifespan startup から `resume_if_needed()`
    で前回の設定で再起動できる。
    """

    def __init__(self) -> None:
        self.job: Job | None = None
        # オッズパーク投票 daemon (headful ブラウザ・人がログイン)。bet_oddspark=True の時だけ
        # 起動し、watch-auto 停止で一緒に倒す。watch loop は毎tick フレッシュ subprocess で
        # ブラウザを保持できないため、ブラウザはこの別の常駐 daemon プロセスが持つ。
        self.bet_job: Job | None = None
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
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    async def start(
        self,
        *,
        window: int = 5,
        tolerance: int = 4,
        interval_sec: int = 60,
        ev_max: float | None = None,
        min_prob: float | None = None,
        market_blend: float | None = None,
        aptitude_top: int | None = None,
        with_exacta: bool = False,
        with_trio: bool = False,
        active_hours: str = "09:00-23:45",
        bet_oddspark: bool = False,
        bet_auto_purchase: bool = False,
        bet_daily_cap: int = 50000,
        bet_stake_multiplier: float = 1.0,
    ) -> Job:
        async with self._ensure_lock():
            return await self._start_locked(
                window=window, tolerance=tolerance, interval_sec=interval_sec,
                ev_max=ev_max, min_prob=min_prob, market_blend=market_blend,
                aptitude_top=aptitude_top, with_exacta=with_exacta,
                with_trio=with_trio, active_hours=active_hours,
                bet_oddspark=bet_oddspark,
                bet_auto_purchase=bet_auto_purchase,
                bet_daily_cap=bet_daily_cap,
                bet_stake_multiplier=bet_stake_multiplier,
            )

    async def _start_locked(
        self,
        *,
        window: int,
        tolerance: int,
        interval_sec: int,
        ev_max: float | None,
        min_prob: float | None,
        market_blend: float | None,
        aptitude_top: int | None,
        with_exacta: bool,
        with_trio: bool,
        active_hours: str,
        bet_oddspark: bool = False,
        bet_auto_purchase: bool = False,
        bet_daily_cap: int = 50000,
        bet_stake_multiplier: float = 1.0,
    ) -> Job:
        # self.job が既に "running" なら早期 return。pending (spawn 中) も
        # 二重 spawn 防止のため return する。
        if self.job is not None and self.job.status in ("pending", "running"):
            return self.job

        # Python ラッパで while ループを回す (api/_watch_loop.py)。
        # bash を挟むと SIGKILL 時に孫プロセスが孤児化するため。
        inner = [
            PY, "-m", "src.auto_watch",
            "--window", str(window),
            "--tolerance", str(tolerance),
            "--active-hours", active_hours,
        ]
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
        if bet_oddspark:
            inner.append("--bet-oddspark")
        cmd = [
            PY, "-m", "api._watch_loop",
            "--interval", str(interval_sec),
            "--",
            *inner,
        ]
        self._config = {
            "window": window,
            "tolerance": tolerance,
            "active_hours": active_hours,
            "interval_sec": interval_sec,
            "ev_max": ev_max,
            "min_prob": min_prob,
            "market_blend": market_blend,
            "aptitude_top": aptitude_top,
            "with_exacta": with_exacta,
            "with_trio": with_trio,
            "bet_oddspark": bet_oddspark,
            "bet_auto_purchase": bet_auto_purchase,
            "bet_daily_cap": bet_daily_cap,
            "bet_stake_multiplier": bet_stake_multiplier,
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
            await self._start_betting_daemon(
                auto_purchase=bool(self._config.get("bet_auto_purchase")),
                daily_cap=int(self._config.get("bet_daily_cap") or 50000),
                stake_multiplier=float(self._config.get("bet_stake_multiplier") or 1.0),
            )
        return self.job

    async def _start_betting_daemon(self, *, auto_purchase: bool = False,
                                    daily_cap: int = 50000,
                                    stake_multiplier: float = 1.0) -> None:
        """オッズパーク投票 daemon (`oddspark_bet --session`) を起動。

        headful ブラウザを開き、人がログイン (poll 検出) → queue を消費。
        - auto_purchase=False (既定): カート投入のみ、購入確定は人。
        - auto_purchase=True (実弾): #gotobuy → 確認 → 確定 まで自動。daily_cap で日次上限ガード。
          (`AUTO_PURCHASE_VERIFIED=False` の間は src 側で fail-safe で実弾を撃たない。)
        uvicorn の env (DISPLAY) を継承するので `make api` を DISPLAY のある端末で起動していれば
        ブラウザが画面に出る。
        """
        if self.bet_job is not None and self.bet_job.status in ("pending", "running"):
            return
        cmd = [PY, "-m", "src.oddspark_bet", "--session", f"--daily-cap={daily_cap}"]
        if auto_purchase:
            cmd.append("--auto-purchase")
        if stake_multiplier != 1.0:
            cmd.append(f"--stake-multiplier={stake_multiplier}")
        label_extra = ""
        if auto_purchase:
            label_extra += " [auto-purchase]"
        if stake_multiplier != 1.0:
            label_extra += f" [×{stake_multiplier}]"
        self.bet_job = Job(
            job_id=f"oddspark-session-{int(time.time())}",
            label="oddspark-bet-session" + label_extra,
            cmd=cmd,
        )
        await self.bet_job.start()

    async def stop(self) -> None:
        async with self._ensure_lock():
            if self.bet_job:
                await self.bet_job.cancel()   # 投票ブラウザも一緒に閉じる
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
                window=int(cfg.get("window", 5)),
                tolerance=int(cfg.get("tolerance", 4)),
                interval_sec=int(cfg.get("interval_sec", 60)),
                ev_max=cfg.get("ev_max"),
                min_prob=cfg.get("min_prob"),
                market_blend=cfg.get("market_blend"),
                aptitude_top=cfg.get("aptitude_top"),
                with_exacta=bool(cfg.get("with_exacta")),
                with_trio=bool(cfg.get("with_trio")),
                active_hours=cfg.get("active_hours", "09:00-23:45"),
                bet_oddspark=bool(cfg.get("bet_oddspark")),
                bet_auto_purchase=bool(cfg.get("bet_auto_purchase")),
                bet_daily_cap=int(cfg.get("bet_daily_cap") or 50000),
                bet_stake_multiplier=float(cfg.get("bet_stake_multiplier") or 1.0),
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
