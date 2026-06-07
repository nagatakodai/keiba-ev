"""3連単回収モード (穴狙い) の1着除外ゲート + プロンプトの検証。

要件 (2026-06-07 ユーザ指示):
- 市場1番人気は Claude 指数が 90 を超えない限り **1着に置かない** (2着/3着は可)。
- 市場は1番人気の判定のみに使い、ランキング・プロンプトには一切出さない。
"""
from __future__ import annotations

from src.models import Horse, Race, RaceData
from src.analyze import (
    TRIFECTA_RECOVERY_INDEX_GATE,
    _market_favorite,
    _recovery_exclude_head,
    _trifecta_mode,
)
from src.llm import build_trifecta_select_prompt


def _rd(odds: dict[int, float]) -> RaceData:
    horses = [Horse(number=n, name=f"馬{n}", win_odds=o) for n, o in odds.items()]
    race = Race(cup_id="t", schedule_index=1, race_number=1, venue_id=44,
                venue_name="テスト", race_class="C1", distance=1400,
                surface="ダート", horses=horses)
    return RaceData(race=race, trifecta=[])


def test_market_favorite_lowest_odds():
    rd = _rd({1: 5.2, 2: 1.8, 3: 12.0})
    assert _market_favorite(rd) == 2


def test_market_favorite_none_without_odds():
    rd = _rd({1: 0.0, 2: 0.0})
    assert _market_favorite(rd) is None


def test_recovery_gate_excludes_favorite_below_threshold():
    """1番人気の Claude 指数 ≤ 90 → 1着除外。"""
    rd = _rd({1: 1.8, 2: 5.0, 3: 9.0})
    ex, fav, fidx = _recovery_exclude_head(rd, {1: 85.0, 2: 92.0})
    assert ex == 1 and fav == 1 and fidx == 85.0
    # 指数が無い1番人気も除外 (>90 を確認できない)
    ex2, fav2, fidx2 = _recovery_exclude_head(rd, {2: 95.0})
    assert ex2 == 1 and fav2 == 1 and fidx2 is None


def test_recovery_gate_allows_favorite_above_threshold():
    """1番人気の Claude 指数 > 90 → 1着解禁 (除外なし)。境界 90.0 ちょうどは除外。"""
    rd = _rd({1: 1.8, 2: 5.0})
    ex, fav, fidx = _recovery_exclude_head(rd, {1: 90.5})
    assert ex is None and fav == 1 and fidx == 90.5
    ex_eq, _, _ = _recovery_exclude_head(rd, {1: TRIFECTA_RECOVERY_INDEX_GATE})
    assert ex_eq == 1  # 「超え」のみ解禁


def test_recovery_gate_degrades_without_market():
    """オッズが無く1番人気を特定できない → 除外なし (純 Claude 指数)。"""
    rd = _rd({1: 0.0, 2: 0.0})
    assert _recovery_exclude_head(rd, {1: 50.0}) == (None, None, None)


def test_trifecta_mode_resolution(monkeypatch):
    """明示 → env → 既定 recovery。不正値は無視。"""
    monkeypatch.delenv("KEIBA_TRIFECTA_MODE", raising=False)
    assert _trifecta_mode() == "recovery"
    assert _trifecta_mode("hit") == "hit"
    assert _trifecta_mode("bogus") == "recovery"
    monkeypatch.setenv("KEIBA_TRIFECTA_MODE", "hit")
    assert _trifecta_mode() == "hit"
    assert _trifecta_mode("recovery") == "recovery"   # 明示が env に勝つ


def test_recovery_prompt_has_exclusion_and_no_market():
    """回収モード prompt: 1着除外指示あり・単勝オッズの数値は一切出さない。"""
    rd = _rd({1: 1.8, 2: 5.0, 3: 9.0})
    p = build_trifecta_select_prompt(
        rd, llm_index={1: 80.0, 2: 70.0, 3: 60.0},
        mode="recovery", exclude_head=1)
    assert "回収 (穴狙い)" in p
    assert "1着除外ルール" in p and "馬 1" in p
    assert "2着・3着には置いてよい" in p
    # 市場情報はオッズ数値として漏れない (1.8 / 5.0 / 9.0 が prompt に出ない)
    for s in ("1.8", "5.0", "9.0", "オッズは"):
        assert s not in p


def test_recovery_prompt_gate_passed_no_market_mention():
    """ゲート通過 (指数>90) → exclude_head=None → 市場・1番人気への言及ゼロ。"""
    rd = _rd({1: 1.8, 2: 5.0})
    p = build_trifecta_select_prompt(
        rd, llm_index={1: 95.0, 2: 70.0}, mode="recovery", exclude_head=None)
    assert "1着除外ルール" not in p
    assert "1番人気" not in p


def test_hit_prompt_unchanged():
    """hit モード (旧 全力的中) は従来のタイトル・除外指示なし。"""
    rd = _rd({1: 1.8, 2: 5.0})
    p = build_trifecta_select_prompt(
        rd, llm_index={1: 80.0, 2: 70.0}, mode="hit", exclude_head=None)
    assert "全力的中" in p
    assert "1着除外ルール" not in p and "回収 (穴狙い)" not in p
