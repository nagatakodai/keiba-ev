"""WatchAutoManager の bet_oddspark 連動: --bet-oddspark 付与 + 投票 daemon 起動。

実プロセス (headful ブラウザ / netkeiba) を spawn しないよう Job.start を no-op 化して
コマンド構築と daemon 起動の有無だけを検証する。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from api import runner


@pytest.fixture
def no_spawn(monkeypatch):
    async def _fake_start(self):   # 実 subprocess を起動しない
        self.status = "running"
    monkeypatch.setattr(runner.Job, "start", _fake_start)
    monkeypatch.setattr(runner, "_save_watch_state", lambda d: None)


def test_bet_oddspark_on_appends_flag_and_starts_daemon(no_spawn):
    mgr = runner.WatchAutoManager()
    job = asyncio.run(mgr.start(bet_oddspark=True))
    assert "--bet-oddspark" in job.cmd                 # watch loop に投入フラグ
    assert mgr.config["bet_oddspark"] is True
    assert mgr.bet_job is not None                     # 投票 daemon を起動
    assert "src.oddspark_bet" in mgr.bet_job.cmd and "--session" in mgr.bet_job.cmd
    assert mgr.bet_running is True


def test_bet_oddspark_off_no_daemon(no_spawn):
    mgr = runner.WatchAutoManager()
    job = asyncio.run(mgr.start(bet_oddspark=False))
    assert "--bet-oddspark" not in job.cmd
    assert mgr.bet_job is None                          # daemon は起動しない
    assert mgr.bet_running is False
    assert mgr.config.get("bet_oddspark") is False


def test_bet_auto_login_appends_flag(no_spawn):
    mgr = runner.WatchAutoManager()
    asyncio.run(mgr.start(bet_oddspark=True, bet_auto_login=True))
    assert mgr.config["bet_auto_login"] is True
    assert "--auto-login" in mgr.bet_job.cmd            # daemon に自動ログイン付与


def test_bet_auto_login_default_off(no_spawn):
    mgr = runner.WatchAutoManager()
    asyncio.run(mgr.start(bet_oddspark=True))
    assert mgr.config.get("bet_auto_login") is False
    assert "--auto-login" not in mgr.bet_job.cmd        # 既定は人が手でログイン


def test_stake_multiplier_passed_to_daemon(no_spawn):
    """掛金倍率 (3連単束) が daemon に --stake-multiplier で渡る。"""
    mgr = runner.WatchAutoManager()
    asyncio.run(mgr.start(bet_oddspark=True, bet_stake_multiplier=2.0))
    assert mgr.config["bet_stake_multiplier"] == 2.0
    assert "--stake-multiplier=2.0" in mgr.bet_job.cmd


def test_trifecta_bankroll_env_propagated(no_spawn):
    """3連単の1レース購入予算が env KEIBA_TRIFECTA_BANKROLL で全 subprocess に伝播する。"""
    mgr = runner.WatchAutoManager()
    asyncio.run(mgr.start(bet_oddspark=True, trifecta_bankroll=20_000))
    assert os.environ.get("KEIBA_TRIFECTA_BANKROLL") == "20000"
    assert mgr.config["trifecta_bankroll"] == 20_000
