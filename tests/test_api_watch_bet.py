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


def test_plan_t_uses_plan_t_multiplier(no_spawn):
    """Plan T 投票時は bet_plan_t_multiplier が daemon に渡る (EV束倍率は無視)。"""
    mgr = runner.WatchAutoManager()
    asyncio.run(mgr.start(
        bet_oddspark=True, bet_plan_t=True,
        bet_stake_multiplier=3.0,      # EV束用 — Plan T 投票時は使われない
        bet_plan_t_multiplier=2.0,     # Plan T 用 — これが効く
    ))
    assert os.environ.get("KEIBA_BET_BUNDLE") == "plan_t"
    assert mgr.config["bet_plan_t"] is True
    assert mgr.config["bet_plan_t_multiplier"] == 2.0
    # daemon には Plan T 倍率 (2.0) が渡り、EV束倍率 (3.0) は渡らない。
    assert "--stake-multiplier=2.0" in mgr.bet_job.cmd
    assert "--stake-multiplier=3.0" not in mgr.bet_job.cmd


def test_ev_bundle_uses_ev_multiplier_not_plan_t(no_spawn):
    """EV束投票 (既定) は bet_stake_multiplier を使い、Plan T 倍率は無視する。"""
    mgr = runner.WatchAutoManager()
    asyncio.run(mgr.start(
        bet_oddspark=True, bet_plan_t=False,
        bet_stake_multiplier=3.0,      # EV束用 — これが効く
        bet_plan_t_multiplier=2.0,     # Plan T 用 — EV束投票時は使われない
    ))
    assert os.environ.get("KEIBA_BET_BUNDLE") == "recommended"
    # daemon には EV束倍率 (3.0) が渡り、Plan T 倍率 (2.0) は渡らない。
    assert "--stake-multiplier=3.0" in mgr.bet_job.cmd
    assert "--stake-multiplier=2.0" not in mgr.bet_job.cmd
