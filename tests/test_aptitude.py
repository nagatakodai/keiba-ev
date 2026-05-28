"""aptitude.py のユニットテスト。

重賞 weight, 因子集約, normalize_to_100 を検証。
"""
from __future__ import annotations

import pytest

from src.aptitude import (
    _normalize_to_100,
    _WEIGHTS,
    _W_SUM,
    compute_aptitudes,
    graded_score,
    graded_summary,
)
from src.features import _normalize_going
from src.models import Horse, PastRun, Race, RaceData, Weather


def _mk_past(
    race_class: str = "",
    finish_pos: int | None = None,
    distance: int = 1600,
    surface: str = "芝",
    going: str = "良",
    last_3f_sec: float = 35.0,
) -> PastRun:
    return PastRun(
        date="2026.04.11",
        venue="東京",
        race_no=1,
        race_class=race_class,
        race_id="",
        surface=surface,
        distance=distance,
        going=going,
        winner_time_sec=95.0,
        time_diff_sec=0.5,
        field_size=16,
        horse_number=1,
        popularity=3,
        jockey="騎手A",
        weight_kg=55.0,
        passing="3-3-1-1",
        last_3f_sec=last_3f_sec,
        finish_pos=finish_pos,
    )


# --- graded_score ---


def test_graded_score_G1_winner():
    """G1 1 着は最高 weight 10 × multiplier 2 = 20。"""
    assert graded_score([_mk_past("G1", finish_pos=1)]) == 20.0


def test_graded_score_G3_third():
    """G3 3 着は 3 × 1.2 = 3.6。"""
    assert graded_score([_mk_past("G3", finish_pos=3)]) == pytest.approx(3.6)


def test_graded_score_OP_no_finish():
    """OP 4 着以下は 1.0 × 0.5 = 0.5。"""
    assert graded_score([_mk_past("OP", finish_pos=None)]) == 0.5


def test_graded_score_jpn_g1_matches_g1():
    """JpnG1 / Jpn1 も G1 として扱う。"""
    assert graded_score([_mk_past("JpnG1", finish_pos=1)]) == 20.0
    assert graded_score([_mk_past("Jpn1", finish_pos=1)]) == 20.0


def test_graded_score_non_graded_returns_zero():
    """平場クラスは 0。"""
    for c in ("3勝クラス", "2勝クラス", "1勝クラス", "新馬", "未勝利", "B1B2"):
        assert graded_score([_mk_past(c, finish_pos=1)]) == 0.0


def test_graded_summary_aggregates_by_grade():
    """同 grade 内で勝ち/連/3 着を集計してテキスト化。"""
    runs = [
        _mk_past("G1", finish_pos=1),
        _mk_past("G1", finish_pos=2),
        _mk_past("G3", finish_pos=3),
    ]
    summary = graded_summary(runs)
    assert "G1" in summary
    assert "G3" in summary
    assert "1着1" in summary
    assert "連1" in summary
    assert "3着1" in summary


# --- _normalize_to_100 ---


def test_normalize_to_100_min_max_spread():
    """min-max scaling: 最弱を APTITUDE_FLOOR、最強を APTITUDE_CEIL に揃え、線形 scaling。

    旧 spec (max のみで割る) では raw 拮抗時に全頭が 99-100 に張り付いて弁別不能
    だったので min-max に変更。raw が僅差でも刻みが残る。
    """
    from src.aptitude import APTITUDE_FLOOR, APTITUDE_CEIL
    d = {1: 5.0, 2: 10.0, 3: 2.5}
    n = _normalize_to_100(d)
    # 最強 (10.0) → CEIL (100)、最弱 (2.5) → FLOOR (60)、中央 (5.0) は線形補間
    assert n[2] == APTITUDE_CEIL == 100.0
    assert n[3] == APTITUDE_FLOOR == 60.0
    # 5.0 は (5-2.5)/(10-2.5) = 1/3 の位置 → FLOOR + 40*1/3 ≈ 73.3
    assert abs(n[1] - (APTITUDE_FLOOR + (5.0 - 2.5) / (10.0 - 2.5) * (APTITUDE_CEIL - APTITUDE_FLOOR))) < 1e-6


def test_normalize_to_100_all_zero():
    """全 0 入力は全 0 出力 (uninformative の signal で 表示で「データ無し」を示す)。"""
    n = _normalize_to_100({1: 0.0, 2: 0.0})
    assert all(v == 0.0 for v in n.values())


def test_normalize_to_100_all_same_returns_mid():
    """全頭同値 (mx-mn≈0 だが情報あり) は (FLOOR+CEIL)/2 を返す (ゼロ割回避)。"""
    from src.aptitude import APTITUDE_FLOOR, APTITUDE_CEIL
    n = _normalize_to_100({1: 5.0, 2: 5.0, 3: 5.0})
    mid = (APTITUDE_FLOOR + APTITUDE_CEIL) / 2.0
    assert all(abs(v - mid) < 1e-9 for v in n.values())


def test_normalize_to_100_clips_negative():
    """負値は 0 にクリップしてから scaling。クリップ後の最弱 (=0) が FLOOR になる。"""
    from src.aptitude import APTITUDE_FLOOR, APTITUDE_CEIL
    n = _normalize_to_100({1: -3.0, 2: 10.0})
    assert n[1] == APTITUDE_FLOOR == 60.0   # クリップ後 0 が最弱 → FLOOR
    assert n[2] == APTITUDE_CEIL == 100.0


def test_normalize_to_100_empty():
    assert _normalize_to_100({}) == {}


# --- compute_aptitudes ---


def _mk_race(horses: list[Horse], track_condition: str = "良") -> RaceData:
    race = Race(
        cup_id="X",
        schedule_index=1,
        race_number=1,
        venue_id=5,
        venue_name="東京",
        race_class="3勝クラス",
        distance=1600,
        surface="芝",
        weather=Weather(code=100, track_condition=track_condition),
    )
    race.horses = horses
    return RaceData(race=race, trifecta=[])


def test_compute_aptitudes_empty_race():
    rd = _mk_race([])
    assert compute_aptitudes(rd) == {}


def test_compute_aptitudes_absent_horses_excluded():
    horses = [
        Horse(number=1, name="A", absent=True),
        Horse(number=2, name="B", absent=True),
    ]
    rd = _mk_race(horses)
    assert compute_aptitudes(rd) == {}


def test_compute_aptitudes_returns_index_per_horse():
    horses = [
        Horse(number=i, name=f"H{i}", past_runs=[_mk_past("G3", finish_pos=2)])
        for i in (1, 2, 3)
    ]
    rd = _mk_race(horses)
    ap = compute_aptitudes(rd)
    assert set(ap) == {1, 2, 3}
    for ai in ap.values():
        # 全 sub-score は 0-100 範囲
        assert 0 <= ai.total <= 100
        assert 0 <= ai.ability <= 100
        assert 0 <= ai.going_fit <= 100


def test_weights_sum_consistent():
    """_W_SUM が _WEIGHTS の合計と一致 (重みリファクタ時の保護)。"""
    assert _W_SUM == pytest.approx(sum(_WEIGHTS.values()))


def test_weights_include_going_fit():
    """going_fit が _WEIGHTS に含まれる (Phase 9 で追加)。"""
    assert "going_fit" in _WEIGHTS
    assert _WEIGHTS["going_fit"] > 0


# --- _normalize_going ---


def test_features_new_columns_computed():
    """Phase 17 で追加した weight_kg_delta / recent_form_score / popularity_outperformance を確認。"""
    from src.features import build_features

    horse = Horse(
        number=1, name="A", weight_kg=56.0,
        past_runs=[
            _mk_past(finish_pos=1) ,  # ↓ override
        ],
    )
    # 過去走 3 走: pop=3 → finish=1 / pop=5 → finish=2 / pop=10 → 6着
    horse.past_runs = [
        PastRun(date="2026.04.11", venue="東京", race_no=1, race_class="3勝クラス",
                race_id="", surface="芝", distance=1600, going="良",
                winner_time_sec=95.0, time_diff_sec=0.0, field_size=16,
                horse_number=1, popularity=3, jockey="J", weight_kg=55.0,
                passing="3-1-1", last_3f_sec=34.5, finish_pos=1),
        PastRun(date="2026.03.20", venue="東京", race_no=2, race_class="2勝クラス",
                race_id="", surface="芝", distance=1600, going="良",
                winner_time_sec=95.0, time_diff_sec=0.2, field_size=16,
                horse_number=1, popularity=5, jockey="J", weight_kg=55.5,
                passing="2-1-2", last_3f_sec=34.7, finish_pos=2),
        PastRun(date="2026.02.20", venue="東京", race_no=3, race_class="2勝クラス",
                race_id="", surface="芝", distance=1600, going="良",
                winner_time_sec=95.0, time_diff_sec=1.5, field_size=16,
                horse_number=1, popularity=10, jockey="J", weight_kg=54.5,
                passing="3-4-6", last_3f_sec=36.0, finish_pos=None),  # 4着以下
    ]
    rd = _mk_race([horse])
    fv = build_features(rd)[1]
    # 斤量変化: 56.0 - (55.0+55.5+54.5)/3 = 56.0 - 55.0 = 1.0
    assert fv.weight_kg_delta == pytest.approx(1.0, abs=0.01)
    # recent_form: ((4-1) + (4-2) + 0) / 3 = (3 + 2 + 0) / 3 = 1.67
    assert fv.recent_form_score == pytest.approx(5 / 3, abs=0.01)
    # popularity_outperformance: 過去 3 走
    # (3 - 1) + (5 - 2) + (10 - 16=field_size) = 2 + 3 + (-6) = -1, /3 = -0.33
    assert fv.popularity_outperformance == pytest.approx((2 + 3 + (10 - 16)) / 3, abs=0.01)


def test_normalize_going_variants():
    assert _normalize_going("良") == "良"
    assert _normalize_going("稍") == "稍"
    assert _normalize_going("稍重") == "稍"
    assert _normalize_going("重") == "重"
    assert _normalize_going("不") == "不"
    assert _normalize_going("不良") == "不"
    assert _normalize_going("") == ""
    assert _normalize_going("不明") == "不"  # 不で始まる → 不 (誤検出だが許容)
