"""_score_timeout: score ステージ timeout を runway + 頭数から決める (timeout 頻発の修正)。"""
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
    # 不正値は無視して通常ロジック (runway 十分な小頭数 → floor)
    assert az._score_timeout(_rd(close_at=int(time.time()) + 3600), 7) == az.SCORE_TIMEOUT_FLOOR


def test_small_field_gets_full_15min(monkeypatch):
    """研究に 15 分使ってよい: 7 頭立てでも runway が足りれば floor=cap=900s (旧 300s 張り付きを解消)。"""
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    assert az._score_timeout(_rd(close_at=int(time.time()) + 3600), 7) == az.SCORE_TIMEOUT_CAP


def test_large_field_capped_at_15min(monkeypatch):
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    # 大頭数でも上限 15 分 (900s) で頭打ち
    assert az._score_timeout(_rd(close_at=int(time.time()) + 3600), 18) == az.SCORE_TIMEOUT_CAP


def test_capped_by_runway(monkeypatch):
    """締切が近いと runway (締切−now−buffer) で頭打ち = bet 段に食い込まない。"""
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    # 締切まで 600s → runway ≈ 600 − 180 = 420 < need(600) → ~420 に capped
    t = az._score_timeout(_rd(close_at=int(time.time()) + 600), 7)
    assert 410 <= t <= 420


def test_uses_start_at_when_no_close(monkeypatch):
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    # close_at 無し → start_at − 120 を締切とみなす。1h 先なら floor。
    assert az._score_timeout(_rd(start_at=int(time.time()) + 3600), 7) == az.SCORE_TIMEOUT_FLOOR


def test_no_deadline_returns_need(monkeypatch):
    monkeypatch.delenv("KEIBA_SCORE_TIMEOUT", raising=False)
    assert az._score_timeout(_rd(), 7) == az.SCORE_TIMEOUT_FLOOR
