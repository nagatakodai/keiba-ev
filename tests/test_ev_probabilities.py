"""ev.py の Plackett-Luce 連鎖確率関数の合計整合性テスト。

各 bet type の確率合計が理論値と一致することを確認する:
  - 単勝 / 馬連 / 馬単 / 3 連複 / 3 連単: 1.0
  - 複勝 / ワイド: 3.0 (3 ポジション × 1)
"""
from __future__ import annotations

from itertools import combinations, permutations

import pytest

from src.ev import (
    BLEND_APTITUDE_GATE,
    BLEND_DEFAULT,
    BLEND_HIT_PURE,
    LGBM_TEMPERATURE,
    build_table,
    estimate_probs,
    exacta_prob,
    place_prob,
    plan_aptitude_ev,
    plan_aptitude_ev_bet,
    plan_hit_pure,
    quinella_prob,
    trifecta_prob,
    trio_prob,
    wide_prob,
    win_prob,
)
from src.models import BetEvRow, EvRow, Horse, Probabilities, Race, RaceData, TrifectaOdds


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


# --- Phase 19: bet-type-specific market_blend ---


def test_blend_constants():
    """Phase 19/20/21 で導入した β / T の既定値定数を確認。
    変更時は holdout eval を再走して根拠を更新すること。"""
    assert BLEND_DEFAULT == 0.78         # Plan A/B/C/H2/単勝/3連単 EV table
    assert BLEND_HIT_PURE == 0.0         # Plan H1 (確率上位 3 点) 専用
    assert BLEND_APTITUDE_GATE == 1.0    # Plan G (適性ゲート) 専用 (Phase 20)
    assert LGBM_TEMPERATURE == 0.4       # LGBM softmax sharpening (Phase 21)


def _make_race_data_5h() -> RaceData:
    """5 馬の最小レース。本命 1 番 (オッズ 2.0)、最大穴 5 番 (オッズ 30.0)。"""
    horses = [
        Horse(number=1, name="A", win_odds=2.0),
        Horse(number=2, name="B", win_odds=4.0),
        Horse(number=3, name="C", win_odds=8.0),
        Horse(number=4, name="D", win_odds=15.0),
        Horse(number=5, name="E", win_odds=30.0),
    ]
    race = Race(
        cup_id="x", schedule_index=1, race_number=1,
        venue_id=1, venue_name="test", race_class="OP",
        distance=1600, surface="芝", horses=horses,
    )
    # 5*4*3 = 60 ordered triples。市場では人気馬寄りに低いオッズ、大穴寄りに高い。
    # 馬の win_odds の積に粗く比例させた合成オッズで十分。
    trifecta: list[TrifectaOdds] = []
    pop = 1
    rows: list[tuple[float, tuple[int, int, int]]] = []
    for i in range(1, 6):
        for j in range(1, 6):
            if j == i:
                continue
            for k in range(1, 6):
                if k == i or k == j:
                    continue
                synth = horses[i - 1].win_odds * horses[j - 1].win_odds * horses[k - 1].win_odds * 0.5
                rows.append((synth, (i, j, k)))
    rows.sort()
    for synth, key in rows:
        trifecta.append(TrifectaOdds(key=key, odds=synth, popularity=pop))
        pop += 1
    return RaceData(race=race, trifecta=trifecta)


def test_plan_h1_picks_differ_under_bet_type_specific_blend():
    """Phase 19: Plan H1 を β=0 と β=0.78 で回すと picks が変わる。

    β=0 (pure model): past_runs が無く features 全 0 → 一様 prob → PL 連鎖で
      ほぼ全 triple が同確率。Plan H1 の top-3 は sort order 依存の任意 3 点。
    β=0.78 (default): market 暗黙率が反映 → 1-2-3 寄りの favorite triple が
      Plan H1 picks に来る。
    両 picks が完全一致しないことだけ確認 (具体的 picks は実装詳細)。
    """
    rd = _make_race_data_5h()
    probs_hit = estimate_probs(rd, market_blend=BLEND_HIT_PURE)
    probs_def = estimate_probs(rd, market_blend=BLEND_DEFAULT)
    rows_hit = build_table(rd, probs_hit)
    rows_def = build_table(rd, probs_def)
    picks_hit = {tuple(r.key) for r in plan_hit_pure(rows_hit, target=3)}
    picks_def = {tuple(r.key) for r in plan_hit_pure(rows_def, target=3)}
    assert len(picks_hit) == 3
    assert len(picks_def) == 3
    # 一致しないことを確認 (= bet-type-specific β が実効的に効いている)
    assert picks_hit != picks_def


def test_estimate_probs_default_blend_matches_constant():
    """estimate_probs の market_blend 既定値が BLEND_DEFAULT 定数と一致する。"""
    import inspect
    sig = inspect.signature(estimate_probs)
    assert sig.parameters["market_blend"].default == BLEND_DEFAULT


# test_training_data_block_present_and_safe は削除 (2026-06-06): `_training_data_block` は
# 回収優先AI (build_prompt / build_refresh_prompt) 専用 helper で、回収優先AI 撤去と共に消えた。


def test_llm_allows_read_tool():
    """Phase 24: claude -p の --allowedTools に Read が含まれる。
    学習データ / snapshot を LLM が参照できる前提を test で固定する。"""
    from src.llm import ALLOWED_TOOLS
    assert "Read" in ALLOWED_TOOLS


def test_lgbm_predict_reads_temperature_from_metadata(monkeypatch):
    """_lgbm_predict が metadata の softmax_temperature を優先して読むこと。

    metadata に T=2.0 を仕込むと softmax 出力が大きく flatten される、
    metadata なしの場合は LGBM_TEMPERATURE=0.4 で sharpen される。
    """
    from src import ev as ev_mod

    # ダミーモデル: predict が 5 馬の固定 score を返す
    class DummyBooster:
        def predict(self, rows):
            return [1.0, 0.5, 0.2, 0.0, -0.5][: len(rows)]

    horses = [
        type("H", (), {"number": i + 1, "absent": False})() for i in range(5)
    ]
    from dataclasses import asdict
    from src.features import FeatureVec
    feats = {i + 1: FeatureVec(number=i + 1) for i in range(5)}

    # T=0.4 (fallback)
    monkeypatch.setattr(ev_mod, "_LGBM_MODEL", DummyBooster())
    monkeypatch.setattr(ev_mod, "_LGBM_META", {"feature_cols": list(asdict(FeatureVec(number=0)).keys())})
    probs_fallback = ev_mod._lgbm_predict(horses, feats)
    assert probs_fallback is not None
    # T=0.4 で top horse の prob が大きくなる (sharpened)
    p_top_T04 = max(probs_fallback.values())

    # T=2.0 (metadata override)
    monkeypatch.setattr(ev_mod, "_LGBM_META", {
        "feature_cols": list(asdict(FeatureVec(number=0)).keys()),
        "softmax_temperature": 2.0,
    })
    probs_T2 = ev_mod._lgbm_predict(horses, feats)
    assert probs_T2 is not None
    p_top_T2 = max(probs_T2.values())

    # T=2.0 (flatter) は T=0.4 (sharper) より top horse prob が小さい
    assert p_top_T2 < p_top_T04, (
        f"T=2.0 should be flatter: got T=0.4 → {p_top_T04:.3f}, T=2.0 → {p_top_T2:.3f}"
    )
