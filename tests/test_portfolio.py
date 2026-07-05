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


def test_bundle_records_dropped_legs_torigami():
    """トリガミで除去した脚を dropped_legs に reason="torigami" で記録する (取り消し線用)。

    記録された脚は (1) reason="torigami" の件数が dropped_torigami と一致 (2) 最終 legs に
    含まれない (3) BundleLeg と同形 + reason を持つ (BundleLegsTable が描画できる) を満たす。
    """
    # 圧倒的本命を作り、低オッズ高 prob 脚をトリガミ除去させる。
    win = {1: 0.5, 2: 0.2, 3: 0.15, 4: 0.1, 5: 0.05}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    cands = [
        {"bet_type": "trifecta", "key": [1, 2, 3], "odds": 2.5, "prob": 0.20, "px_o": 0.50, "tier": "minus"},
        {"bet_type": "trifecta", "key": [1, 3, 2], "odds": 4.0, "prob": 0.15, "px_o": 0.60, "tier": "minus"},
        {"bet_type": "trifecta", "key": [1, 2, 4], "odds": 40.0, "prob": 0.08, "px_o": 3.2, "tier": "oana"},
        {"bet_type": "trifecta", "key": [2, 1, 3], "odds": 60.0, "prob": 0.05, "px_o": 3.0, "tier": "oana"},
        {"bet_type": "trifecta", "key": [1, 4, 5], "odds": 120.0, "prob": 0.02, "px_o": 2.4, "tier": "oana"},
    ]
    b = pf.build_bundle(cands, probs, prioritize="hit", avoid_torigami=True,
                        hit_max_legs=len(cands), max_legs=len(cands))
    dropped = b["dropped_legs"]
    assert isinstance(dropped, list)
    tori = [d for d in dropped if d["reason"] == "torigami"]
    assert len(tori) == b["dropped_torigami"]
    kept_keys = {tuple(l["key"]) for l in b["legs"]}
    leg_fields = {"bet_type", "key", "odds", "prob", "px_o", "tier",
                  "kelly", "fraction", "stake", "payout_if_hit", "reason"}
    for dl in dropped:
        assert tuple(dl["key"]) not in kept_keys           # 除去脚は買わない
        assert leg_fields <= set(dl)                       # BundleLeg + reason 同形
        assert dl["reason"] in ("torigami", "budget")
    for dl in tori:
        assert dl["payout_if_hit"] == round(dl["odds"] * dl["stake"])


def test_bundle_records_dropped_legs_budget():
    """予算 (bankroll) を割り切れず配分0 になった脚を reason="budget" で記録する。

    小さい bankroll では多くの候補が stake<min_stake で買えない → それらが全て
    dropped_legs に reason="budget" で残り、買わなかった買い目が取り消し線で見える。
    """
    win = {1: 0.30, 2: 0.20, 3: 0.15, 4: 0.12, 5: 0.10, 6: 0.08, 7: 0.05}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    keys = [(1, 2, 3), (1, 3, 2), (2, 1, 3), (1, 2, 4), (2, 3, 1),
            (1, 4, 5), (3, 1, 2), (2, 4, 5), (4, 5, 6), (5, 6, 7)]
    prob = [0.05, 0.04, 0.03, 0.025, 0.02, 0.015, 0.012, 0.01, 0.006, 0.003]
    od = [20, 28, 35, 50, 70, 90, 120, 160, 250, 400]
    cands = [{"bet_type": "trifecta", "key": list(k), "odds": float(o),
              "prob": p, "px_o": p * o, "tier": "oana"}
             for k, p, o in zip(keys, prob, od)]
    b = pf.build_bundle(cands, probs, bankroll=600, prioritize="hit", avoid_torigami=True,
                        hit_max_legs=len(cands), max_legs=len(cands),
                        min_stake=100, stake_unit=100)
    budget = [d for d in b["dropped_legs"] if d["reason"] == "budget"]
    assert budget, "小予算では budget で買えない脚が出るはず"
    # 予算内に収まる: 投資総額 ≤ bankroll、かつ買った脚 + 買わなかった脚 = 全候補。
    assert b["total_stake"] <= 600
    assert len(b["legs"]) + len(b["dropped_legs"]) == len(cands)
    kept_keys = {tuple(l["key"]) for l in b["legs"]}
    for d in budget:
        assert d["stake"] == 0 and d["payout_if_hit"] == 0
        assert tuple(d["key"]) not in kept_keys


def test_trifecta_hitmax_propagates_dropped_legs():
    """3連単束 (build_trifecta_hitmax) が build_bundle の除去脚を束に伝搬する。"""
    from src.models import BetOdds
    win = {1: 0.5, 2: 0.2, 3: 0.15, 4: 0.1, 5: 0.05}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    trif = [
        BetOdds(bet_type="trifecta", key=[1, 2, 3], odds=3.0),
        BetOdds(bet_type="trifecta", key=[1, 3, 2], odds=5.0),
        BetOdds(bet_type="trifecta", key=[1, 2, 4], odds=40.0),
        BetOdds(bet_type="trifecta", key=[2, 1, 3], odds=60.0),
        BetOdds(bet_type="trifecta", key=[1, 4, 5], odds=120.0),
        BetOdds(bet_type="trifecta", key=[1, 3, 4], odds=70.0),
        BetOdds(bet_type="trifecta", key=[2, 3, 1], odds=80.0),
    ]
    bt = pf.build_trifecta_hitmax(probs, trif, bankroll=10_000)
    assert "dropped_legs" in bt
    tori = [d for d in bt["dropped_legs"] if d["reason"] == "torigami"]
    assert len(tori) == bt.get("dropped_torigami", 0)


def test_trifecta_hitmax_respects_bankroll_budget():
    """3連単束の合計購入額は指定 bankroll (購入予算) を超えない。"""
    from src.models import BetOdds
    win = {i: 1.0 / 8 for i in range(1, 9)}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    # 全 ordered triple に薄いオッズを付与して広いフォーメーションを作る。
    trif = []
    for a in range(1, 9):
        for b_ in range(1, 9):
            for c in range(1, 9):
                if len({a, b_, c}) == 3:
                    trif.append(BetOdds(bet_type="trifecta", key=[a, b_, c], odds=200.0))
    for budget in (3000, 5000, 10000):
        bt = pf.build_trifecta_hitmax(probs, trif, bankroll=budget,
                                      rank_index={i: 100 - i * 5 for i in range(1, 9)})
        assert bt["total_stake"] <= budget, (budget, bt["total_stake"])
        assert bt["bankroll"] == budget


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
    """単一 +EV bet の joint Kelly は scalar Kelly f*=(pO-1)/(O-1) に一致。

    最適化はドリフトシェード込みの実効オッズ (odds × DRIFT_SHADE[bet_type]) で行うので、
    期待値もシェード込みで計算する (shade 定数の較正でテストが壊れないように, 2026-07-05)。
    """
    n = 6
    probs = _uniform_probs(n)  # p(win=horse1)=1/6
    p_win = 1.0 / n
    odds = 9.0  # P×O = 1.5 (+EV)
    cands = [{"bet_type": "win", "key": [1], "odds": odds, "prob": p_win,
              "px_o": p_win * odds, "tier": "chuana"}]
    b = pf.build_bundle(cands, probs, min_stake=1, stake_unit=1)
    assert len(b["legs"]) == 1
    odds_eff = odds * pf._drift_shade("win")
    f_expected = (p_win * odds_eff - 1.0) / (odds_eff - 1.0)
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


def _tri_odds(keys_odds):
    """[(key, odds), ...] → trifecta BetOdds 風オブジェクト列 (SimpleNamespace)。"""
    from types import SimpleNamespace
    return [SimpleNamespace(key=list(k), odds=float(o), absent=False) for k, o in keys_odds]


def test_build_trifecta_from_keys_filters_and_allocates():
    """Claude 選定 keys から束: 重複/非distinct/出走外/オッズ無しを除外、トリガミ防止 stake 配分。"""
    win = {1: 0.40, 2: 0.25, 3: 0.15, 4: 0.12, 5: 0.08}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    tri = _tri_odds([((1, 2, 3), 60), ((1, 3, 2), 90), ((2, 1, 3), 120), ((1, 2, 4), 75)])
    keys = [[1, 2, 3], [1, 3, 2], [2, 1, 3], [1, 2, 4],
            [1, 2, 3], [1, 1, 2], [9, 9, 9], [1, 2, 5]]  # dup / 非distinct / 出走外 / オッズ無し
    b = pf.build_trifecta_from_keys(probs, tri, keys)
    assert b["objective"] == "trifecta_claude_select"
    assert b["rank_source"] == "claude" and b["selection_source"] == "claude"
    assert b["n_candidates"] == 4                     # 有効 4 keys のみ ([1,2,5]はオッズ無し)
    got = {tuple(l["key"]) for l in b["legs"]}
    assert got <= {(1, 2, 3), (1, 3, 2), (2, 1, 3), (1, 2, 4)}
    # トリガミ防止: 全脚 payout ≥ 投資総額 (min_payout_ratio ≥ margin)
    assert b["min_payout_ratio"] >= b.get("torigami_margin", 1.10) - 1e-9
    assert 0 < b["total_stake"] <= 10_000


def test_build_trifecta_from_keys_empty_when_no_buyable():
    win = {1: 0.5, 2: 0.3, 3: 0.2}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    tri = _tri_odds([((1, 2, 3), 50)])
    # 選定 keys が全て買えない (オッズ無し) → 束空
    b = pf.build_trifecta_from_keys(probs, tri, [[3, 2, 1], [2, 3, 1]])
    assert b["legs"] == [] and b["n_points"] == 0


def test_build_trifecta_from_keys_max_points_cap():
    win = {i: 1.0 / 8 for i in range(1, 9)}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    import itertools
    perms = list(itertools.permutations(range(1, 9), 3))
    tri = _tri_odds([(p, 100 + i) for i, p in enumerate(perms)])
    keys = [list(p) for p in perms]                    # 336 通り
    b = pf.build_trifecta_from_keys(probs, tri, keys, max_points=20)
    assert b["n_candidates"] <= 20                     # max_points で打ち切り


def test_trifecta_hitmax_head_gap_two_horses():
    """build_trifecta_hitmax: 指数 top2 が接戦なら1着列が2頭になる (head_gap 判定)。"""
    from src.models import BetOdds
    win = {i: 1.0 / 8 for i in range(1, 9)}
    probs = Probabilities(win=win, place2=dict(win), place3=dict(win))
    trif = []
    for a in range(1, 9):
        for b_ in range(1, 9):
            for c in range(1, 9):
                if len({a, b_, c}) == 3:
                    trif.append(BetOdds(bet_type="trifecta", key=[a, b_, c], odds=300.0))
    # 馬1=100, 馬2=95 (相対差 5/100 < 0.12) → head 2頭。
    rank = {i: 100 - i * 5 for i in range(1, 9)}
    bt = pf.build_trifecta_hitmax(probs, trif, rank_index=rank,
                                  head_max=2, head_gap=0.12)
    assert bt["head_horses"] == [1, 2]
    assert all(l["key"][0] in (1, 2) for l in bt["legs"])
