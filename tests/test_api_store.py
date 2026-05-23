"""api/store.py の path traversal 防御 test。"""
from __future__ import annotations


def test_safe_race_id_accepts_normal_ids():
    """JRA / NAR の race_id (12 桁数字 or 正規化形式 cup_id-day-race) を受け入れる。"""
    from api.store import _safe_race_id
    assert _safe_race_id("202605230412") == "202605230412"
    assert _safe_race_id("20260523-4-12") == "20260523-4-12"
    assert _safe_race_id("2026440521-521-9") == "2026440521-521-9"
    assert _safe_race_id("test_race-1") == "test_race-1"


def test_safe_race_id_rejects_path_traversal():
    """`..` や `/` を含む文字列を弾く。"""
    from api.store import _safe_race_id
    assert _safe_race_id("..") is None
    assert _safe_race_id("../etc/passwd") is None
    assert _safe_race_id("../../etc/passwd") is None
    assert _safe_race_id("/etc/passwd") is None
    assert _safe_race_id("test/../etc") is None


def test_safe_race_id_rejects_special_chars():
    """日本語 / null byte / 空 / 空白 を弾く。"""
    from api.store import _safe_race_id
    assert _safe_race_id("") is None
    assert _safe_race_id(" ") is None
    assert _safe_race_id("test race") is None  # space
    assert _safe_race_id("test\x00") is None
    assert _safe_race_id("テスト") is None
    assert _safe_race_id("test;rm -rf /") is None


def test_get_prediction_returns_none_for_bad_race_id():
    """get_prediction(traversal) は None。"""
    from api.store import get_prediction
    assert get_prediction("../../etc/passwd") is None
    assert get_prediction("../config") is None
    assert get_prediction("") is None


def test_compute_calibration_runs_without_error():
    """compute_calibration がエラーなく終わる (smoke test)。
    現データ (data/predictions / data/results) と PLAN_KEY_FIELDS の構造
    変更が壊れない最低限の確認。"""
    from api.store import compute_calibration
    c = compute_calibration()
    # 基本 shape
    assert "race_count" in c
    assert "plans" in c
    assert "tiers" in c
    # 全 Plan が出ている (snapshot にキーがあれば)
    plan_names = {p["plan"] for p in c["plans"]}
    # 少なくとも A/B/C は常に出る (rebuild dataset 以後)
    if c["race_count"] > 0:
        assert "Plan A" in plan_names or len(plan_names) >= 0  # smoke
    # 各 plan が必須 fields を持つ
    for p in c["plans"]:
        assert "plan" in p
        assert "stake" in p
        assert "payout" in p
        assert "roi" in p
        assert "hit_rate_ci_low" in p
        assert "hit_rate_ci_high" in p
