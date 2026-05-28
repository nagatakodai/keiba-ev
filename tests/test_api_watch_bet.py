"""WatchAutoManager の bet_oddspark 連動: --bet-oddspark 付与 + 投票 daemon 起動。

実プロセス (headful ブラウザ / netkeiba) を spawn しないよう Job.start を no-op 化して
コマンド構築と daemon 起動の有無だけを検証する。
"""
from __future__ import annotations

import asyncio

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
