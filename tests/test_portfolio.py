"""joint Kelly まとめ買い (src/portfolio.py) の不変量テスト。"""
from __future__ import annotations

import math

from src.models import Probabilities
from src import portfolio as pf


def _uniform_probs(n: int) -> Probabilities:
    """n 頭が等確率の単純な Probabilities。"""
    win = {i: 1.0 / n for i in range(1, n + 1)}
    place = {i: 1.0 for i in range(1, n + 1)}
    return Probabilities(win=win, place2=dict(place), place3=dict(place))


def test_bundle_no_torigami_invariant():
    """avoid_torigami=True なら、全脚で payout(odds×stake) ≥ 投資総額。

    これにより「どの的中 outcome でも収支マイナス (トリガミ) にならない」が保証される。
    人気馬 (低オッズ) を含む非一様分布で検証。
    """
    win = {1: 0.5, 2: 0.2, 3: 0.15, 4: 0.1, 5: 0.05}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    cands = [
        {"bet_type": "win", "key": [1], "odds": 2.2, "prob": 0.5, "px_o": 1.10, "tier": "honsen"},
        {"bet_type": "win", "key": [2], "odds": 6.0, "prob": 0.2, "px_o": 1.20, "tier": "chuana"},
        {"bet_type": "win", "key": [3], "odds": 8.0, "prob": 0.15, "px_o": 1.20, "tier": "chuana"},
    ]
    b = pf.build_bundle(cands, probs, avoid_torigami=True)
    if b["legs"]:
        S = b["total_stake"]
        for leg in b["legs"]:
            assert leg["odds"] * leg["stake"] >= S - 1e-6, leg
            assert leg["payout_if_hit"] >= S
        assert b["min_payout_ratio"] >= 1.0 - 1e-9


def test_bundle_torigami_margin_enforced():
    """torigami_margin>1 なら全脚 payout ≥ 投資総額 × margin (オッズ下振れ緩衝)。

    レンジ型 bet (ワイド/複勝) の確定オッズや締切直前ドリフトで保存オッズから
    下振れしても収支マイナスにならないことを保証する。
    """
    win = {1: 0.4, 2: 0.25, 3: 0.2, 4: 0.1, 5: 0.05}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    cands = [
        {"bet_type": "win", "key": [1], "odds": 3.0, "prob": 0.4, "px_o": 1.20, "tier": "honsen"},
        {"bet_type": "win", "key": [2], "odds": 5.0, "prob": 0.25, "px_o": 1.25, "tier": "chuana"},
        {"bet_type": "win", "key": [3], "odds": 7.0, "prob": 0.2, "px_o": 1.40, "tier": "chuana"},
    ]
    margin = 1.10
    b = pf.build_bundle(cands, probs, torigami_margin=margin)
    assert b["torigami_margin"] == margin
    if b["legs"]:
        S = b["total_stake"]
        for leg in b["legs"]:
            assert leg["odds"] * leg["stake"] >= S * margin - 1e-6, leg
        assert b["min_payout_ratio"] >= margin - 1e-9
    # マージンを上げると脚数は増えない (より厳しい除去のみ)
    n_lo = len(pf.build_bundle(cands, probs, torigami_margin=1.0)["legs"])
    n_hi = len(pf.build_bundle(cands, probs, torigami_margin=1.5)["legs"])
    assert n_hi <= n_lo


def test_bundle_total_never_exceeds_bankroll():
    """多脚でも丸め誤差で総額が bankroll を超えない (floor 丸め)。

    round 丸めだと per-leg 切り上げの累積で ¥10,100 等になるバグの回帰防止。
    """
    win = {i: 1.0 / 16 for i in range(1, 17)}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    p_win = 1.0 / 16
    cands = [
        {"bet_type": "win", "key": [i], "odds": 30.0, "prob": p_win,
         "px_o": p_win * 30.0, "tier": "oana"}
        for i in range(1, 13)
    ]
    b = pf.build_bundle(cands, probs, bankroll=10_000, avoid_torigami=False)
    assert b["total_stake"] <= 10_000


def test_bundle_kelly_fraction_gt1_respects_bankroll():
    """kelly_fraction>1 でも総額 ≤ bankroll (スケール後の再射影で cap を守る)。"""
    win = {i: 1.0 / 6 for i in range(1, 7)}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    p_win = 1.0 / 6
    cands = [
        {"bet_type": "win", "key": [i], "odds": 9.0, "prob": p_win,
         "px_o": p_win * 9.0, "tier": "chuana"}
        for i in range(1, 5)
    ]
    b = pf.build_bundle(cands, probs, bankroll=10_000, kelly_fraction=2.0,
                        avoid_torigami=False)
    assert b["total_stake"] <= 10_000


def test_bundle_torigami_filter_does_not_add_legs():
    """avoid_torigami=True は False より脚数を増やさない (除去のみ)。"""
    probs = _uniform_probs(8)
    p_win = 1.0 / 8
    cands = [
        {"bet_type": "win", "key": [i], "odds": 12.0, "prob": p_win,
         "px_o": p_win * 12.0, "tier": "oana"}
        for i in range(1, 7)
    ]
    n_off = len(pf.build_bundle(cands, probs, avoid_torigami=False)["legs"])
    n_on = len(pf.build_bundle(cands, probs, avoid_torigami=True)["legs"])
    assert n_on <= n_off


def test_outcomes_sum_to_one():
    for n in (4, 6, 8):
        outs, p = pf.enumerate_outcomes(_uniform_probs(n))
        assert len(outs) == n * (n - 1) * (n - 2)
        assert math.isclose(float(p.sum()), 1.0, rel_tol=1e-9)


def test_bet_hits_all_types():
    a, b, c = 3, 7, 5  # 1着=3, 2着=7, 3着=5
    assert pf._bet_hits("win", [3], a, b, c)
    assert not pf._bet_hits("win", [7], a, b, c)
    assert pf._bet_hits("place", [5], a, b, c)
    assert not pf._bet_hits("place", [9], a, b, c)
    assert pf._bet_hits("exacta", [3, 7], a, b, c)
    assert not pf._bet_hits("exacta", [7, 3], a, b, c)  # 順序
    assert pf._bet_hits("quinella", [7, 3], a, b, c)    # 順不同
    assert pf._bet_hits("wide", [5, 7], a, b, c)        # 両馬 top3
    assert not pf._bet_hits("wide", [5, 9], a, b, c)
    assert pf._bet_hits("trio", [5, 3, 7], a, b, c)     # 順不同 top3
    assert pf._bet_hits("trifecta", [3, 7, 5], a, b, c)
    assert not pf._bet_hits("trifecta", [3, 5, 7], a, b, c)


def test_empty_bundle_when_no_positive_ev():
    """全候補 P×O < floor なら見送り (legs 空)。"""
    probs = _uniform_probs(6)
    cands = [{"bet_type": "win", "key": [1], "odds": 1.5, "prob": 0.1,
              "px_o": 0.15, "tier": "minus"}]
    b = pf.build_bundle(cands, probs)
    assert b["legs"] == []
    assert b["total_stake"] == 0


def test_single_bet_matches_scalar_kelly():
    """単一 +EV bet の joint Kelly は scalar Kelly f*=(pO-1)/(O-1) に一致。"""
    n = 6
    probs = _uniform_probs(n)  # p(win=horse1)=1/6
    p_win = 1.0 / n
    odds = 9.0  # P×O = 1.5 (+EV)
    cands = [{"bet_type": "win", "key": [1], "odds": odds, "prob": p_win,
              "px_o": p_win * odds, "tier": "chuana"}]
    b = pf.build_bundle(cands, probs, min_stake=1, stake_unit=1)
    assert len(b["legs"]) == 1
    f_expected = (p_win * odds - 1.0) / (odds - 1.0)
    assert math.isclose(b["legs"][0]["kelly"], f_expected, abs_tol=2e-3)


def test_budget_and_hit_prob_bounds():
    """総額 ≤ bankroll、bundle_hit_prob ∈ [0,1]。"""
    probs = _uniform_probs(8)
    p_win = 1.0 / 8
    cands = [
        {"bet_type": "win", "key": [i], "odds": 12.0, "prob": p_win,
         "px_o": p_win * 12.0, "tier": "oana"}
        for i in range(1, 5)
    ]
    b = pf.build_bundle(cands, probs, bankroll=10_000)
    assert b["total_stake"] <= 10_000
    assert 0.0 <= b["bundle_hit_prob"] <= 1.0
    assert b["total_fraction"] <= 1.0 + 1e-9
