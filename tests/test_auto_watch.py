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


# --- oddspark betting queue 連携 (--bet-oddspark) ---
import json

from src import auto_watch as aw


def _write_snapshot(root, race_id, legs, rank_source="claude"):
    """Plan T 束 (recommended_bundle_t) を持つ snapshot を書く。

    投票束は Plan T 固定 (2026-06-06)。recommended_bundle はモデル参考値なので
    enqueue 判定には関与しない (混入していても無視されることを兼ねて書いておく)。
    """
    pred = root / "data" / "predictions"
    pred.mkdir(parents=True, exist_ok=True)
    (pred / f"{race_id}.json").write_text(json.dumps({
        "race_id": race_id,
        "recommended_bundle": {"legs": []},   # EV束 (参考値) — 投票には使われない
        "recommended_bundle_t": {"legs": legs, "rank_source": rank_source},
    }, ensure_ascii=False), encoding="utf-8")


def test_enqueue_oddspark_bet_writes_req(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [{"bet_type": "wide", "key": [4, 10], "stake": 600},
                     {"bet_type": "win", "key": [7], "stake": 0}])  # stake0 は除外
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is True
    req = tmp_path / "queue" / "202650052709.req"
    assert req.exists()
    d = json.loads(req.read_text())
    assert d["legs"] == 1 and d["total_stake"] == 600
    assert d["bundle_source"] == "plan_t"   # 投票束は常に Plan T
    # 二重投入は False (既存 req)
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is False


def test_enqueue_skips_empty_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-5", [])   # 見送り
    assert aw._enqueue_oddspark_bet("2026500527-527-5", "202650052705") is False
    assert not (tmp_path / "queue" / "202650052705.req").exists()


def test_enqueue_skips_jra(tmp_path, monkeypatch):
    """JRA (投票 joCode 無し) は oddspark で投票不可 → enqueue しない。"""
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026940527-527-9",
                    [{"bet_type": "win", "key": [1], "stake": 500}])
    assert aw._enqueue_oddspark_bet("2026940527-527-9", "202694052709") is False


def test_enqueue_skips_when_no_claude_index(tmp_path, monkeypatch):
    """Claude 指数なし (rank_source=model に縮退した Plan T 束) は投票しない (enqueue しない)。"""
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [{"bet_type": "trifecta", "key": [1, 2, 3], "stake": 100}],
                    rank_source="model")   # Claude 指数なし → model 縮退
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is False
    assert not (tmp_path / "queue" / "202650052709.req").exists()


def test_enqueue_votes_with_claude_index(tmp_path, monkeypatch):
    """Claude 指数あり (rank_source=claude) なら通常どおり enqueue。"""
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [{"bet_type": "trifecta", "key": [1, 2, 3], "stake": 100}],
                    rank_source="claude")
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is True
    req = tmp_path / "queue" / "202650052709.req"
    assert req.exists()
    assert json.loads(req.read_text())["bundle_source"] == "plan_t"
