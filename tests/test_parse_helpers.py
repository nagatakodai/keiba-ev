"""parse.py の odds dict → BetOdds 変換ヘルパのテスト。"""
from __future__ import annotations

from datetime import datetime

from src.parse import (
    _exacta_dict_to_bets,
    _pair_dict_to_bets,
    _parse_start_at,
    _tanfuku_to_bets,
    _trio_dict_to_bets,
)


def test_parse_start_at_time_before_hassou():
    """netkeiba 現行表記 'HH:MM発走' (時刻が先) を拾えること。

    旧 regex は '発走 HH:MM' 前提で現行表記に当たらず start_at=0 → 締切/発走が
    UI で '—' になるバグの回帰防止。
    """
    ts = _parse_start_at("20:50発走 / ダ1400m (右)", race_id="202654052411")  # NAR 高知 5/24
    assert datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") == "2026-05-24 20:50"


def test_parse_start_at_legacy_order_still_works():
    ts = _parse_start_at("発走 20:50 / ダ1400m", race_id="202654052411")
    assert datetime.fromtimestamp(ts).strftime("%H:%M") == "20:50"


def test_parse_start_at_jra_date_from_title():
    # JRA は race_id から暦日を復元できないので title の 'YYYY年M月D日' を使う
    ts = _parse_start_at("09:45発走 / ダ1700m  2026年4月11日 福島1R", race_id="202603010101")
    assert datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") == "2026-04-11 09:45"


def test_parse_start_at_no_time_returns_zero():
    assert _parse_start_at("ダ1400m (右)", race_id="202654052411") == 0


def test_pair_dict_to_bets_popularity_by_odds_asc():
    """odds 昇順で popularity を 1..N に振る。"""
    d = {(1, 2): 50.0, (1, 3): 20.0, (2, 3): 80.0}
    bets = _pair_dict_to_bets(d, "quinella")
    assert len(bets) == 3
    # 最安オッズ (=最高人気) が popularity 1
    pop1 = next(b for b in bets if b.popularity == 1)
    assert pop1.key == (1, 3)
    assert pop1.odds == 20.0
    assert pop1.bet_type == "quinella"


def test_exacta_dict_to_bets_preserves_order():
    """馬単 (i, j) は順序ありで key を保持。"""
    d = {(1, 2): 30.0, (2, 1): 35.0}
    bets = _exacta_dict_to_bets(d)
    keys = {b.key for b in bets}
    assert (1, 2) in keys
    assert (2, 1) in keys


def test_trio_dict_to_bets_length_3():
    """3 連複は key 長 3。"""
    d = {(1, 2, 3): 50.0, (1, 2, 4): 80.0}
    bets = _trio_dict_to_bets(d)
    assert all(len(b.key) == 3 for b in bets)
    assert all(b.bet_type == "trio" for b in bets)


def test_tanfuku_to_bets_splits_win_place():
    """単複 dict → (wins, places) で分離。複勝は下限 (fuku_min) を採用。"""
    d = {
        1: {"tan": 5.0, "fuku_min": 1.5, "fuku_max": 2.5},
        2: {"tan": 12.0, "fuku_min": 3.0, "fuku_max": 5.0},
        3: {"tan": 0.0, "fuku_min": 0.0, "fuku_max": 0.0},  # オッズ無し → 除外
    }
    wins, places = _tanfuku_to_bets(d)
    win_keys = {b.key for b in wins}
    place_keys = {b.key for b in places}
    assert (1,) in win_keys
    assert (2,) in win_keys
    assert (3,) not in win_keys  # オッズ 0 で除外
    # 複勝は fuku_min
    p1 = next(b for b in places if b.key == (1,))
    assert p1.odds == 1.5


def test_tanfuku_to_bets_popularity_by_odds_asc():
    """単勝も odds 昇順で popularity 1 = 最安。"""
    d = {
        1: {"tan": 5.0, "fuku_min": 1.5},
        2: {"tan": 12.0, "fuku_min": 3.0},
        3: {"tan": 2.5, "fuku_min": 1.2},
    }
    wins, _ = _tanfuku_to_bets(d)
    by_pop = {b.popularity: b.key for b in wins}
    assert by_pop[1] == (3,)  # 2.5 が最安
    assert by_pop[2] == (1,)  # 5.0
    assert by_pop[3] == (2,)  # 12.0
