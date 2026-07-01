"""provisional.py (仮指数) のユニットテスト。

最重要: **市場人気に一切触れない** (PastRun.popularity / Horse.win_odds を変えても不変) を担保。
加えて 中立50 anchor / 決定論 / 強い過去走ほど高い / 連勝・馬場適性の効き / 重み合計=1。
すべて OFFLINE (合成 RaceData)。
"""
from __future__ import annotations

from src import provisional as P
from src.models import Horse, PastRun, Race, RaceData, Weather


def _past(
    *,
    finish_pos=None,
    winner_time=95.0,
    time_diff=0.5,
    distance=1600,
    surface="芝",
    going="良",
    race_class="1勝",
    last_3f=35.0,
    field_size=16,
    popularity=8,
) -> PastRun:
    return PastRun(
        date="2026.04.11", venue="東京", race_no=1, race_class=race_class, race_id="",
        surface=surface, distance=distance, going=going,
        winner_time_sec=winner_time, time_diff_sec=time_diff, field_size=field_size,
        popularity=popularity, jockey="J", weight_kg=55.0, last_3f_sec=last_3f,
        finish_pos=finish_pos,
    )


def _horse(number: int, name: str, past, win_odds: float = 5.0) -> Horse:
    h = Horse(number=number, name=name)
    h.past_runs = list(past)
    h.win_odds = win_odds
    return h


def _race(horses, track_condition="良", distance=1600, surface="芝", race_class="1勝") -> RaceData:
    race = Race(
        cup_id="X", schedule_index=1, race_number=1, venue_id=5, venue_name="東京",
        race_class=race_class, distance=distance, surface=surface,
        weather=Weather(code=100, track_condition=track_condition),
    )
    race.horses = list(horses)
    return RaceData(race=race, trifecta=[])


def _strong_runs():
    return [_past(finish_pos=1, winner_time=93.0, time_diff=0.0),
            _past(finish_pos=1, winner_time=93.5, time_diff=0.0),
            _past(finish_pos=2, winner_time=94.0, time_diff=0.3)]


def _weak_runs():
    return [_past(finish_pos=None, winner_time=99.0, time_diff=2.5),
            _past(finish_pos=None, winner_time=99.5, time_diff=3.0),
            _past(finish_pos=None, winner_time=100.0, time_diff=2.8)]


# ── 最重要: 市場人気に一切触れない ─────────────────────────────────
def test_ignores_past_popularity_and_win_odds():
    """PastRun.popularity / Horse.win_odds をどう変えても仮指数は不変 (= 市場を見ていない)。"""
    base = _race([_horse(1, "A", _strong_runs()), _horse(2, "B", _weak_runs())])
    out1 = P.provisional_index(base)
    # 過去人気を極端に (1番人気/最下位人気) 入れ替え + 今日の単勝オッズも激変させる
    for h in base.race.horses:
        h.win_odds = 999.0 if h.number == 1 else 1.1
        for r in h.past_runs:
            r.popularity = 18 if h.number == 1 else 1
    out2 = P.provisional_index(base)
    assert out1 == out2, "仮指数が過去人気/オッズに反応している (市場リーク)"


# ── 中立50 anchor ────────────────────────────────────────────────
def test_no_past_runs_is_neutral_50():
    """過去走ゼロの馬は中立50 (最弱に落とさない)。"""
    rd = _race([_horse(1, "A", _strong_runs()), _horse(2, "New", [])])
    out = P.provisional_index(rd)
    assert out[2] == P.NEUTRAL == 50.0
    assert out[1] > out[2]   # 実績馬は中立より上


def test_determinism():
    rd = _race([_horse(1, "A", _strong_runs()), _horse(2, "B", _weak_runs())])
    assert P.provisional_index(rd) == P.provisional_index(rd)


# ── 予測方向: 強い過去走ほど高い ──────────────────────────────────
def test_strong_beats_weak():
    rd = _race([_horse(1, "Strong", _strong_runs()), _horse(2, "Weak", _weak_runs())])
    out = P.provisional_index(rd)
    assert out[1] > out[2]


def test_winning_streak_lifts_form_momentum():
    """同水準の時計でも、連勝中の馬は form_momentum が着外続きの馬より高い。"""
    streak = [_past(finish_pos=1, winner_time=95.0, time_diff=0.0),
              _past(finish_pos=1, winner_time=95.0, time_diff=0.0),
              _past(finish_pos=1, winner_time=95.0, time_diff=0.0)]
    slump = [_past(finish_pos=None, winner_time=95.0, time_diff=0.4),
             _past(finish_pos=None, winner_time=95.0, time_diff=0.4),
             _past(finish_pos=None, winner_time=95.0, time_diff=0.4)]
    rd = _race([_horse(1, "Streak", streak), _horse(2, "Slump", slump)])
    bd = P.provisional_breakdown(rd)
    assert bd[1]["form_momentum"] > bd[2]["form_momentum"]


def test_going_aptitude_prefers_today_going_record():
    """今日が重馬場。重で勝つ馬は、良でしか走っていない馬より going 因子が高い。"""
    mudder = [_past(finish_pos=1, going="重", winner_time=96.0, time_diff=0.0),
              _past(finish_pos=1, going="不", winner_time=96.0, time_diff=0.0)]
    dry = [_past(finish_pos=1, going="良", winner_time=96.0, time_diff=0.0),
           _past(finish_pos=1, going="良", winner_time=96.0, time_diff=0.0)]
    rd = _race([_horse(1, "Mud", mudder), _horse(2, "Dry", dry)], track_condition="重")
    bd = P.provisional_breakdown(rd)
    assert bd[1]["going"] > bd[2]["going"]


def test_weights_sum_to_one():
    for name, w in (("NAR", P.WEIGHTS_NAR), ("JRA", P.WEIGHTS_JRA),
                    ("BANEI", P.WEIGHTS_BANEI), ("default", P.WEIGHTS)):
        assert abs(sum(w.values()) - 1.0) < 1e-6, f"{name} 重み合計 != 1"
        assert set(w) == set(P._FACTORS), f"{name} の因子キーが不一致"


def test_segment_weight_selection():
    """venue_id で NAR/JRA/banei の重みが切り替わる。"""
    def _rd(vid):
        rd = _race([_horse(1, "A", _strong_runs())])
        rd.race.venue_id = vid
        return rd
    assert P.weights_for(_rd(5)) is P.WEIGHTS_JRA        # 中央 (05=東京)
    assert P.weights_for(_rd(30)) is P.WEIGHTS_NAR       # 地方
    assert P.weights_for(_rd(65)) is P.WEIGHTS_BANEI     # 帯広ばんえい
    assert P._segment_of_rd(_rd(0)) == "nar"             # 未知は NAR


def test_all_scores_in_range_and_absent_excluded():
    a = _horse(1, "A", _strong_runs())
    b = _horse(2, "B", _weak_runs())
    c = _horse(3, "Gone", _strong_runs())
    c.absent = True
    rd = _race([a, b, c])
    out = P.provisional_index(rd)
    assert 3 not in out                      # absent は除外
    assert all(0.0 <= v <= 100.0 for v in out.values())
