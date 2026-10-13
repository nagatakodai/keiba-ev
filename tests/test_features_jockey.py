"""features.jockey_change: 今日の騎手が分からないときは乗替りにしない (2026-10-14)。"""
from __future__ import annotations

from src.features import build_features
from src.models import Horse, PastRun, Race, RaceData


def _rd(today_jockey: str, prev_jockey: str):
    h = Horse(number=1, name="A", jockey_name=today_jockey)
    h.past_runs = [PastRun(date="2026.09.01", venue="大井", surface="ダート", distance=1200, going="良",
                           winner_time_sec=73.0, jockey=prev_jockey)]
    race = Race(cup_id="", schedule_index=0, race_number=1, venue_id=44, venue_name="大井", race_class="",
                distance=1200, surface="ダート", horses=[h])
    return RaceData(race=race, trifecta=[], other_bets={})


def test_jockey_change_needs_both_sides():
    assert build_features(_rd("", "山本聡"))[1].jockey_change is False          # 今日の騎手不明 → 判定しない
    assert build_features(_rd("山本聡", "山本聡"))[1].jockey_change is False
    assert build_features(_rd("武豊", "武 豊"))[1].jockey_change is False        # 空白違いは同じ騎手
    assert build_features(_rd("笹川翼", "山本聡"))[1].jockey_change is True
