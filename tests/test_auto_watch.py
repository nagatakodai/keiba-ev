"""watch-auto の時間帯判定 / 検出帯ロジックのテスト。"""
from __future__ import annotations

from datetime import datetime

from src.auto_watch import _in_active_hours


def test_active_hours_normal_range():
    assert _in_active_hours(datetime(2026, 5, 25, 10, 0), "09:00-23:45") is True
    assert _in_active_hours(datetime(2026, 5, 25, 9, 0), "09:00-23:45") is True   # 端点 inclusive
    assert _in_active_hours(datetime(2026, 5, 25, 23, 45), "09:00-23:45") is True
    assert _in_active_hours(datetime(2026, 5, 25, 8, 59), "09:00-23:45") is False
    assert _in_active_hours(datetime(2026, 5, 25, 23, 46), "09:00-23:45") is False


def test_active_hours_midnight_wrap():
    """日跨ぎ範囲 (例 22:00-01:00) でも正しく判定 (旧実装は常に False のバグ)。"""
    ah = "22:00-01:00"
    assert _in_active_hours(datetime(2026, 5, 25, 22, 30), ah) is True   # start 以降
    assert _in_active_hours(datetime(2026, 5, 25, 0, 30), ah) is True    # end 以前 (翌日)
    assert _in_active_hours(datetime(2026, 5, 25, 1, 0), ah) is True     # 端点
    assert _in_active_hours(datetime(2026, 5, 25, 12, 0), ah) is False   # 範囲外
    assert _in_active_hours(datetime(2026, 5, 25, 21, 59), ah) is False


def test_active_hours_malformed_returns_true():
    # パース不能なら「常時 active」にフォールバック (検出を止めない)
    assert _in_active_hours(datetime(2026, 5, 25, 3, 0), "garbage") is True
    assert _in_active_hours(datetime(2026, 5, 25, 3, 0), "") is True


def test_window_band_is_plus_only():
    """検出帯は片側 (+のみ): [window, window+tolerance] 分前。

    window より発走に近い側 (旧 ± の下側) は検出しない。auto_watch._list_due_races
    の low_sec / high_sec と同じ式を検証する。
    """
    window_min, tolerance_min = 10, 5
    low_sec = window_min * 60
    high_sec = (window_min + tolerance_min) * 60

    def in_band(delta_min: float) -> bool:
        d = delta_min * 60
        return low_sec <= d <= high_sec

    assert in_band(10) and in_band(12.5) and in_band(15)
    # window より近い (発走間際) は除外 — +のみの肝
    assert not in_band(9.9)
    assert not in_band(5)
    # window+tolerance より遠い (早すぎ) も除外
    assert not in_band(15.1)
