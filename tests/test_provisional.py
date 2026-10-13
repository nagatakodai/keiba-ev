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
        # 重みは既知因子の部分集合 (計測中の因子は _FACTORS に居るが未採用でよい)。
        assert set(w).issubset(set(P._FACTORS)), f"{name} に未知因子キー"


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


def test_class_is_ignored_outside_jra_but_used_in_jra():
    """地方・ばんえいの仮指数はクラスを使わない (2026-10-13: keiba.go.jp の過去走は自走時計しか無く、クラスが
    時計を潰して公式データ再現で勝ち馬 AUC −0.024)。全角/半角・クラスの高低を変えても同じ値。中央は使う。"""
    def build(vid, cls_a, cls_b, target):
        a = _horse(1, "A", [_past(finish_pos=None, time_diff=0.8, race_class=cls_a, surface="ダート")
                           for _ in range(3)])
        b = _horse(2, "B", [_past(finish_pos=None, time_diff=0.8, race_class=cls_b, surface="ダート")
                           for _ in range(3)])
        c = _horse(3, "C", _weak_runs())
        rd = _race([a, b, c], surface="ダート", race_class=target)
        rd.race.venue_id = vid
        return P.provisional_index(rd), P.provisional_breakdown(rd)
    nar1, br1 = build(44, "Ｃ２四組", "Ａ１", "Ｂ３－５")
    nar2, br2 = build(44, "C3", "C3", "")
    assert nar1 == nar2 and nar1[1] == nar1[2] and br1 == br2
    banei, _ = build(65, "Ｃ２－１", "Ａ１－１", "Ｂ２－１")
    assert banei[1] == banei[2]
    jra, _ = build(5, "1勝", "3勝", "2勝")
    assert jra[2] > jra[1]                       # 中央: 上のクラスで同じ時計の B が上


def test_nar_speed_index_ignores_past_weight_but_jra_uses_it():
    """地方の仮指数の SI は斤量の項を足さない (公式データで過去走の斤量が埋まると 1kg=2点が時計より効いた)。"""
    def build(vid, wa, wb):
        def runs(w):
            out = []
            for _ in range(3):
                r = _past(finish_pos=None, time_diff=0.8, surface="ダート")
                r.weight_kg = w
                out.append(r)
            return out
        rd = _race([_horse(1, "A", runs(wa)), _horse(2, "B", runs(wb)), _horse(3, "C", _weak_runs())],
                   surface="ダート")
        rd.race.venue_id = vid
        return P.provisional_index(rd)
    nar = build(44, 50.0, 58.0)
    assert nar[1] == nar[2]
    jra = build(5, 50.0, 58.0)
    assert jra[2] > jra[1]


def test_robust_scores_do_not_overflow_on_tiny_mad():
    raws = {1: 1.0, 2: 1.0 + 1e-12, 3: 1.0, 4: -5e5}
    out = P._robust_scores(raws)
    assert all(0.0 <= v <= 100.0 for v in out.values())
