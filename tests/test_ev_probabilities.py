"""ev.py の Plackett-Luce 連鎖確率関数の合計整合性テスト。

各 bet type の確率合計が理論値と一致することを確認する:
  - 単勝 / 馬連 / 馬単 / 3 連複 / 3 連単: 1.0
  - 複勝 / ワイド: 3.0 (3 ポジション × 1)
"""
from __future__ import annotations

from itertools import combinations, permutations

import pytest

from src.ev import (
    exacta_prob,
    place_prob,
    plan_aptitude_ev,
    plan_aptitude_ev_bet,
    quinella_prob,
    trifecta_prob,
    trio_prob,
    wide_prob,
    win_prob,
)
from src.models import BetEvRow, EvRow, Probabilities


@pytest.fixture
def probs_4h() -> Probabilities:
    """4 馬の Plackett-Luce 確率 (各位置同じ強度ベクトル)。"""
    base = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
    return Probabilities(win=dict(base), place2=dict(base), place3=dict(base))


@pytest.fixture
def probs_8h() -> Probabilities:
    """8 馬の Plackett-Luce 確率 (やや偏りある分布)。"""
    base = {1: 0.30, 2: 0.20, 3: 0.15, 4: 0.10, 5: 0.10, 6: 0.07, 7: 0.05, 8: 0.03}
    return Probabilities(win=dict(base), place2=dict(base), place3=dict(base))


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_win_total_one(probs_name, request):
    probs = request.getfixturevalue(probs_name)
    total = sum(win_prob((i,), probs) for i in probs.win)
    assert total == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_place_total_three(probs_name, request):
    """複勝合計 = 3 (3 ポジション × 1 馬)。"""
    probs = request.getfixturevalue(probs_name)
    total = sum(place_prob((i,), probs) for i in probs.win)
    assert total == pytest.approx(3.0, abs=1e-9)


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_quinella_total_one(probs_name, request):
    probs = request.getfixturevalue(probs_name)
    total = sum(quinella_prob(c, probs) for c in combinations(probs.win, 2))
    assert total == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_wide_total_three(probs_name, request):
    """ワイド合計 = 3 (top3 は常に 3 馬 → C(3,2)=3 ペアが各レースで的中)。"""
    probs = request.getfixturevalue(probs_name)
    total = sum(wide_prob(c, probs) for c in combinations(probs.win, 2))
    assert total == pytest.approx(3.0, abs=1e-9)


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_exacta_total_one(probs_name, request):
    probs = request.getfixturevalue(probs_name)
    total = sum(exacta_prob(p, probs) for p in permutations(probs.win, 2))
    assert total == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_trio_total_one(probs_name, request):
    probs = request.getfixturevalue(probs_name)
    total = sum(trio_prob(c, probs) for c in combinations(probs.win, 3))
    assert total == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("probs_name", ["probs_4h", "probs_8h"])
def test_trifecta_total_one(probs_name, request):
    probs = request.getfixturevalue(probs_name)
    total = sum(trifecta_prob(p, probs) for p in permutations(probs.win, 3))
    assert total == pytest.approx(1.0, abs=1e-9)


def test_place_monotone_with_win_rank(probs_4h):
    """1 着率が高い馬ほど複勝率も高い (単調性)。"""
    ps = [(i, place_prob((i,), probs_4h)) for i in probs_4h.win]
    by_win_desc = sorted(probs_4h.win.items(), key=lambda kv: kv[1], reverse=True)
    by_place_desc = sorted(ps, key=lambda kv: kv[1], reverse=True)
    assert [n for n, _ in by_win_desc] == [n for n, _ in by_place_desc]


def test_quinella_symmetric(probs_4h):
    """馬連 (i, j) は順不同なので key の順序に依存しない。"""
    a = quinella_prob((1, 2), probs_4h)
    b = quinella_prob((2, 1), probs_4h)
    assert a == pytest.approx(b)


def test_exacta_asymmetric(probs_4h):
    """馬単 (i, j) は順序ありなので (1,2) と (2,1) は異なる。"""
    a = exacta_prob((1, 2), probs_4h)
    b = exacta_prob((2, 1), probs_4h)
    assert a != pytest.approx(b)
    # 1 着率の高い 1 が先頭の方が大きい
    assert a > b


def test_zero_win_prob_returns_zero(probs_4h):
    """win prob 0 の馬は全 bet type で 0 を返す。"""
    assert win_prob((99,), probs_4h) == 0.0
    assert place_prob((99,), probs_4h) == 0.0
    assert exacta_prob((99, 1), probs_4h) == 0.0
    assert quinella_prob((99, 1), probs_4h) == 0.0


def test_plan_aptitude_ev_filters_outside_top():
    """Plan G は適性 top N に入らない馬を含む key を全除外する。"""
    rows = [
        EvRow(key=(1, 2, 3), odds=12.0, popularity=1, prob=0.1, px_o=1.2, tier="honsen"),
        EvRow(key=(1, 2, 4), odds=12.0, popularity=2, prob=0.1, px_o=1.2, tier="honsen"),
        EvRow(key=(5, 6, 7), odds=20.0, popularity=3, prob=0.1, px_o=2.0, tier="oana"),
    ]
    apt_top = [1, 2, 3]
    picks = plan_aptitude_ev(rows, apt_top)
    keys = {tuple(r.key) for r in picks}
    assert (1, 2, 3) in keys
    assert (1, 2, 4) not in keys  # 4 が top に居ない
    assert (5, 6, 7) not in keys  # 全員 top 外


def test_plan_aptitude_ev_applies_pxo_floor():
    """P×O が floor 未満の row は除外。"""
    rows = [
        EvRow(key=(1, 2, 3), odds=10.0, popularity=1, prob=0.1, px_o=1.0, tier="minus"),
        EvRow(key=(1, 3, 2), odds=10.0, popularity=2, prob=0.15, px_o=1.5, tier="honsen"),
    ]
    picks = plan_aptitude_ev(rows, [1, 2, 3], pxo_floor=1.02)
    keys = {tuple(r.key) for r in picks}
    assert (1, 2, 3) not in keys  # P×O=1.0 < 1.02
    assert (1, 3, 2) in keys  # P×O=1.5 OK


def test_plan_aptitude_ev_bet_handles_2_horse_keys():
    """BetEvRow 版は可変長 key (馬連 length=2, 3 連複 length=3) に対応。"""
    rows = [
        BetEvRow(bet_type="quinella", key=(1, 2), odds=20.0, popularity=1, prob=0.06, px_o=1.2, tier="honsen"),
        BetEvRow(bet_type="quinella", key=(1, 5), odds=30.0, popularity=2, prob=0.04, px_o=1.2, tier="honsen"),
    ]
    picks = plan_aptitude_ev_bet(rows, [1, 2, 3])
    keys = {tuple(r.key) for r in picks}
    assert (1, 2) in keys
    assert (1, 5) not in keys
