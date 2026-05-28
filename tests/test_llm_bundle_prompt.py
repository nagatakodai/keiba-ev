"""build_bundle_selection_prompt の構造テスト。

3連単 evidence 廃止 → 総合オススメ束に対する web 検索補強 への切替に合わせ、
プロンプトが (a) 検索ルールを含む (b) 候補/束を含む (c) 出力 JSON 例を含む を検証。
"""
from __future__ import annotations

from src import llm
from src.models import Horse, Race, RaceData


def _rd():
    horses = [Horse(number=i, name=f"馬{i}") for i in range(1, 6)]
    race = Race(cup_id="X", schedule_index=1, race_number=3,
                venue_id=42, venue_name="笠松", race_class="C",
                distance=1400, surface="ダート", weather_text="晴/稍重",
                horses=horses, start_at=1_756_400_000)
    return RaceData(race=race, trifecta=[], other_bets={})


CANDS = [
    {"bet_type": "win", "key": [1], "odds": 7.1, "prob": 0.21, "px_o": 1.49, "tier": "+"},
    {"bet_type": "wide", "key": [3, 6], "odds": 12.3, "prob": 0.22, "px_o": 2.71, "tier": "++"},
    {"bet_type": "trifecta", "key": [1, 3, 6], "odds": 211.9, "prob": 0.012, "px_o": 2.54, "tier": "++"},
]


def test_prompt_includes_search_rules_and_candidates():
    bundle = {"legs": [{"bet_type": "wide", "key": [3, 6], "stake": 100}],
              "total_stake": 100, "bundle_hit_prob": 0.22, "min_payout_ratio": 1.2}
    p = llm.build_bundle_selection_prompt(_rd(), bundle, CANDS)
    # 検索ルール (CLAUDE.md 準拠) が prompt に組み込まれている
    assert "web 検索" in p
    assert "最大 6 クエリ" in p or "最大6クエリ" in p
    assert "補強根拠" in p
    assert "cuts" in p
    # 全候補が表に乗る (P×O 降順)
    for c in CANDS:
        assert llm.leg_id(c) in p
    # 出力 JSON 例 (picks/cuts/notes/summary/confidence) が含まれる
    assert '"picks"' in p and '"cuts"' in p and '"notes"' in p
    # 束採用 leg(★) が明示
    assert "★" in p
    assert "wide:3-6" in p


def test_prompt_handles_empty_bundle_legs():
    # 束 legs が空(見送り)でも prompt 構築自体は壊れない (呼び出し側で先に弾く想定だが防御)
    p = llm.build_bundle_selection_prompt(_rd(), {"legs": []}, CANDS)
    assert "joint Kelly" in p
    assert "wide:3-6" in p   # 候補表は出る


def test_prompt_includes_kaisai_date_when_start_at_set():
    bundle = {"legs": []}
    p = llm.build_bundle_selection_prompt(_rd(), bundle, CANDS)
    # 検索クエリ用に開催日 (YYYY-MM-DD) が prompt に乗る
    assert "開催日:" in p
    assert "2025-" in p or "2026-" in p


def test_bundle_legs_always_appear_in_candidate_table_even_below_max():
    """束採用 leg (★) は max_candidates でカットされた場合でも prompt の候補表に必ず出る。

    joint Kelly が低 P×O 脚を組み入れることがあるため、★ id が表に出ない不整合を防ぐ。
    """
    # max_candidates=1 → 候補表は本来 P×O 最大の1件だけ。だが束には P×O が低い leg を含める
    bundle = {"legs": [{"bet_type": "trifecta", "key": [1, 3, 6], "stake": 100}]}
    p = llm.build_bundle_selection_prompt(_rd(), bundle, CANDS, max_candidates=1)
    # 最大 P×O は wide:3-6 (2.71)。束は trifecta:1-3-6 (P×O=2.54、本来は cut)
    assert "wide:3-6" in p              # max P×O はもちろん入る
    assert "trifecta:1-3-6" in p        # 束採用なので必ず追補されて入る
    assert "★" in p
