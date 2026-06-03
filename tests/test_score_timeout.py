"""_score_timeout: score ステージ timeout は常に 15 分 (900s) 固定 (env で上書き可)。"""
from __future__ import annotations

import time
import types

from src import analyze as az


def _rd(close_at=None, start_at=None):
    return types.SimpleNamespace(
        race=types.SimpleNamespace(close_at=close_at, start_at=start_at, horses=[])
    )


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("KEIBA_SCORE_TIMEOUT", "777")
    assert az._score_timeout(_rd(close_at=int(time.time()) + 1200), 7) == 777


def test_env_override_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("KEIBA_SCORE_TIMEOUT", "abc")
    assert az._score_timeout(_rd(close_at=int(time.time()) + 3600), 7) == az.SCORE_TIMEOUT_SEC


def test_always_15min_small_field(monkeypatch):
    """7 頭立てでも常に 900s (旧: floor 300s 張り付き / runway 短縮)。"""
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    assert az._score_timeout(_rd(close_at=int(time.time()) + 3600), 7) == 900


def test_always_15min_large_field(monkeypatch):
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    assert az._score_timeout(_rd(close_at=int(time.time()) + 3600), 18) == 900


def test_always_15min_even_when_deadline_near(monkeypatch):
    """締切が近くても runway で頭打ちせず常に 900s (ユーザ指示: 常に15分)。"""
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    assert az._score_timeout(_rd(close_at=int(time.time()) + 120), 7) == 900


def test_always_15min_no_deadline(monkeypatch):
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    assert az._score_timeout(_rd(), 7) == 900
