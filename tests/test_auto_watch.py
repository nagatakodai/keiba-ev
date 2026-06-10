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


def _write_snapshot(root, race_id, legs, rank_source="claude", ev_legs=None):
    """3連単束 (recommended_bundle_t) + EV束 (recommended_bundle) を持つ snapshot を書く。

    投票束は env KEIBA_BET_BUNDLE で切替 (2026-06-10 復活, 既定 ev)。3連単フローのテストは
    env=trifecta を設定して使う。
    """
    pred = root / "data" / "predictions"
    pred.mkdir(parents=True, exist_ok=True)
    (pred / f"{race_id}.json").write_text(json.dumps({
        "race_id": race_id,
        "recommended_bundle": {"legs": ev_legs or []},
        "recommended_bundle_t": {"legs": legs, "rank_source": rank_source},
    }, ensure_ascii=False), encoding="utf-8")


def test_enqueue_oddspark_bet_writes_req(tmp_path, monkeypatch):
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "trifecta")
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
    assert d["bundle_source"] == "trifecta"   # 投票束は常に 3連単的中モード
    # 二重投入は False (既存 req)
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is False


def test_enqueue_skips_empty_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "trifecta")
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-5", [])   # 見送り
    assert aw._enqueue_oddspark_bet("2026500527-527-5", "202650052705") is False
    assert not (tmp_path / "queue" / "202650052705.req").exists()


def test_enqueue_skips_jra(tmp_path, monkeypatch):
    """JRA (投票 joCode 無し) は oddspark で投票不可 → enqueue しない。"""
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "trifecta")
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026940527-527-9",
                    [{"bet_type": "win", "key": [1], "stake": 500}])
    assert aw._enqueue_oddspark_bet("2026940527-527-9", "202694052709") is False


def test_enqueue_skips_when_no_claude_index(tmp_path, monkeypatch):
    """Claude 指数なし (rank_source=model に縮退した 3連単束) は投票しない (enqueue しない)。"""
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "trifecta")
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [{"bet_type": "trifecta", "key": [1, 2, 3], "stake": 100}],
                    rank_source="model")   # Claude 指数なし → model 縮退
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is False
    assert not (tmp_path / "queue" / "202650052709.req").exists()


def test_enqueue_votes_with_claude_index(tmp_path, monkeypatch):
    """Claude 指数あり (rank_source=claude) なら通常どおり enqueue。"""
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "trifecta")
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [{"bet_type": "trifecta", "key": [1, 2, 3], "stake": 100}],
                    rank_source="claude")
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is True
    req = tmp_path / "queue" / "202650052709.req"
    assert req.exists()
    assert json.loads(req.read_text())["bundle_source"] == "trifecta"


# --- 投票束切替 (env KEIBA_BET_BUNDLE, 2026-06-10 復活) ---


def test_bundle_source_default_is_ev(monkeypatch):
    monkeypatch.delenv("KEIBA_BET_BUNDLE", raising=False)
    assert aw._bet_bundle_source() == "ev"
    assert aw._bet_bundle_field() == "recommended_bundle"
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "trifecta")
    assert aw._bet_bundle_field() == "recommended_bundle_t"
    monkeypatch.setenv("KEIBA_BET_BUNDLE", "bogus")   # 不正値は既定 ev に倒す
    assert aw._bet_bundle_source() == "ev"


def test_enqueue_ev_bundle(tmp_path, monkeypatch):
    """EV束モード: recommended_bundle の legs で enqueue。rank_source ゲートは適用しない。"""
    monkeypatch.delenv("KEIBA_BET_BUNDLE", raising=False)   # 既定 ev
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [],   # 3連単束は空 (EV束モードでは見ない)
                    rank_source="model",
                    ev_legs=[{"bet_type": "win", "key": [4], "stake": 300},
                             {"bet_type": "wide", "key": [4, 10], "stake": 200}])
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is True
    d = json.loads((tmp_path / "queue" / "202650052709.req").read_text())
    assert d["bundle_source"] == "ev"
    assert d["legs"] == 2 and d["total_stake"] == 500


def test_enqueue_ev_bundle_skips_empty(tmp_path, monkeypatch):
    """EV束モード: EV束が空 (見送り) なら 3連単束に legs があっても enqueue しない。"""
    monkeypatch.delenv("KEIBA_BET_BUNDLE", raising=False)
    monkeypatch.setattr(aw, "ROOT", tmp_path)
    monkeypatch.setattr(aw, "BET_QUEUE_DIR", tmp_path / "queue")
    _write_snapshot(tmp_path, "2026500527-527-9",
                    [{"bet_type": "trifecta", "key": [1, 2, 3], "stake": 100}],
                    rank_source="claude", ev_legs=[])
    assert aw._enqueue_oddspark_bet("2026500527-527-9", "202650052709") is False


# --- bet 予約の atomic claim (二重 dispatch 防止, 2026-06-10 bughunt) ---


def test_claim_bet_schedule_atomic(tmp_path, monkeypatch):
    """予約 claim は 1 プロセスだけが成功し、unclaim で予約に戻り、release で消える。"""
    monkeypatch.setattr(aw, "BET_SCHEDULE_DIR", tmp_path / "sched")
    race = {"race_id": "2026500527-527-9", "netkeiba_race_id": "202650052709",
            "close_at": 9999999999, "start_at": 9999999999}
    aw._write_bet_schedule(race)
    sched_json = tmp_path / "sched" / "2026500527-527-9.json"
    firing = tmp_path / "sched" / "2026500527-527-9.firing"
    assert sched_json.exists()
    # 予約の mtime を古く偽装 (score 帯を広く取る運用 = 締切16分以上前の予約を模倣)
    import os as _os
    import time as _time
    old_t = _time.time() - 1100
    _os.utime(sched_json, (old_t, old_t))
    # 1 回目の claim は成功し .firing に移る
    assert aw._claim_bet_schedule("2026500527-527-9") is True
    assert not sched_json.exists() and firing.exists()
    # claim は mtime を claim 時刻に更新する (2026-06-11 bughunt: rename が予約書込時の
    # 古い mtime を引き継ぐと、併走プロセスの _cleanup_stale_claims (900s 判定) が
    # 発火処理中の .firing を即削除し、失敗時の unclaim 再試行が消えていた)
    assert _time.time() - firing.stat().st_mtime < 60
    aw._cleanup_stale_claims(max_age_sec=900)
    assert firing.exists()   # 掃除に消されない
    # 2 回目 (併走プロセス相当) は失敗 = 二重 dispatch しない
    assert aw._claim_bet_schedule("2026500527-527-9") is False
    # dispatch 失敗 → unclaim で予約に戻る (次 tick で再試行可能)
    aw._unclaim_bet_schedule("2026500527-527-9")
    assert sched_json.exists() and not firing.exists()
    # 成功時は claim → release で完全に消える
    assert aw._claim_bet_schedule("2026500527-527-9") is True
    aw._release_bet_claim("2026500527-527-9")
    assert not sched_json.exists() and not firing.exists()


def test_cleanup_stale_claims(tmp_path, monkeypatch):
    """孤児 .firing (claim したまま死んだプロセスの残骸) は max_age 超で掃除される。"""
    import os
    import time as _time
    monkeypatch.setattr(aw, "BET_SCHEDULE_DIR", tmp_path / "sched")
    (tmp_path / "sched").mkdir()
    stale = tmp_path / "sched" / "old.firing"
    fresh = tmp_path / "sched" / "new.firing"
    stale.write_text("{}")
    fresh.write_text("{}")
    old_t = _time.time() - 1200
    os.utime(stale, (old_t, old_t))
    aw._cleanup_stale_claims(max_age_sec=900)
    assert not stale.exists()      # 15 分超 → 掃除
    assert fresh.exists()          # 直近 claim は残す
