"""自動予想・自動投票の封鎖スイッチ (src/automation_lock.py, 2026-10-10)。

既定は封鎖 (KEIBA_AUTOMATION=1 を明示したときだけ解除)。`make dev` 起動直後に当日
キャッチアップスキャンが全レースを Claude で予想し始めた実障害の再発防止。
"""
from __future__ import annotations

import asyncio

import pytest

import src.automation_lock as lock


@pytest.fixture
def no_dotenv(monkeypatch):
    """リポジトリの実 .env に左右されないよう .env 側の値を空にする。"""
    monkeypatch.setattr(lock, "_dotenv_value", lambda: "")


def test_default_is_blocked(monkeypatch, no_dotenv):
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    assert lock.automation_enabled() is False


@pytest.mark.parametrize("val,want", [("1", True), (" 1 ", True), ("0", False),
                                      ("", False), ("true", False), ("yes", False)])
def test_only_explicit_one_unlocks(monkeypatch, no_dotenv, val, want):
    monkeypatch.setenv("KEIBA_AUTOMATION", val)
    assert lock.automation_enabled() is want


def test_dotenv_unlocks_when_env_unset(monkeypatch):
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    monkeypatch.setattr(lock, "_dotenv_value", lambda: "1")
    assert lock.automation_enabled() is True


def test_shell_env_overrides_dotenv(monkeypatch):
    monkeypatch.setenv("KEIBA_AUTOMATION", "0")
    monkeypatch.setattr(lock, "_dotenv_value", lambda: "1")
    assert lock.automation_enabled() is False


def test_exit_if_blocked(monkeypatch, no_dotenv, capsys):
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    with pytest.raises(SystemExit) as ei:
        lock.exit_if_blocked("auto_watch")
    assert ei.value.code == 3
    assert "一時停止中" in capsys.readouterr().err
    monkeypatch.setenv("KEIBA_AUTOMATION", "1")
    lock.exit_if_blocked("auto_watch")   # 解除中は何もしない


@pytest.mark.parametrize("module", ["src.bet_scheduler", "src.oddspark_bet", "src.ipat_bet"])
def test_betting_cli_entrypoints_blocked(monkeypatch, no_dotenv, module):
    """投票系 CLI は引数解析・ブラウザ起動より前に rc=3 で止まる。"""
    import importlib
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    mod = importlib.import_module(module)
    monkeypatch.setattr("sys.argv", [module, "--session"])
    with pytest.raises(SystemExit) as ei:
        mod._main()
    assert ei.value.code == 3


def test_auto_watch_entrypoint_blocked(monkeypatch, no_dotenv):
    import src.auto_watch as aw
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    with pytest.raises(SystemExit) as ei:
        aw.main()   # 封鎖チェックは本体の先頭 (discovery 等のネットワークに触れる前)
    assert ei.value.code == 3


def test_api_watch_start_returns_423(monkeypatch, no_dotenv):
    from fastapi import HTTPException
    import api.main as m
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)

    async def _must_not_start(**_kw):
        raise AssertionError("封鎖中に WATCH.start が呼ばれた")

    monkeypatch.setattr(m.WATCH, "start", _must_not_start)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(m.api_watch_start(m.WatchAutoStartRequest()))
    assert ei.value.status_code == 423
    assert m.api_watch_status()["automation_locked"] is True


def test_watch_manager_refuses_start_and_resume(monkeypatch, no_dotenv):
    import api.runner as r
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    mgr = r.WatchAutoManager(r.JobRegistry())
    with pytest.raises(RuntimeError):
        asyncio.run(mgr.start())
    # should_run=true の永続 state があっても resume しない (state file は読みすらしない)
    monkeypatch.setattr(r, "_load_watch_state",
                        lambda: (_ for _ in ()).throw(AssertionError("state を読んだ")))
    assert asyncio.run(mgr.resume_if_needed()) is None


def test_auto_loops_do_not_start_when_blocked(monkeypatch, no_dotenv):
    """当日/前夜スキャン・締切前再score は start() しても task を作らない (lifespan 以外の経路も封鎖)。"""
    import api.main as m
    monkeypatch.delenv("KEIBA_AUTOMATION", raising=False)
    loops = [m.ShobuPaddockRescorer(), m.ShobuNightlyPrescanner(m.JOBS),
             m.ShobuDailyCatchupScanner(m.JOBS)]
    for lp in loops:
        if hasattr(lp, "enabled"):
            lp.enabled = True
        lp.start()   # 封鎖中は event loop 不要 (create_task に到達しない)
        assert lp._task is None
        assert lp.status()["automation_locked"] is True
