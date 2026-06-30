"""compute_shobu_pnl / compute_indexed_pnl の母集団スコープ + superset 不変条件の test。

ユーザ指摘 (2026-06-28): 「参考(全レース)が 153 件と多すぎ・ほとんど推奨のはず」。
原因は indexed が data/predictions 全体 (betting pipeline の過去スコア含む) を母集団にしていたこと。
修正後は **shobu が評価したレースのみ** を母集団にし、推奨カードの proper superset になる。
ここでは tmp dir に shobu/predictions/results を作って検証する (ネットワーク不要)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import api.store as store


def _snap(n_runners: int, idx: dict[int, float]) -> dict:
    """全馬に Claude 指数を付けた snapshot (index_compare 形式)。"""
    return {
        "n_runners": n_runners,
        "saved_at": "2026-06-28T11:00:00",
        "index_compare": [
            {"number": k, "claude_index": v, "market_index": 50.0} for k, v in idx.items()
        ],
    }


def _result(order: list[int], payout: int) -> dict:
    return {"finish_order": order, "trifecta_payout": payout,
            "recorded_at": "2026-06-28T17:00:00"}


def _result_fo(order: list[int], payout: int, final_odds: dict) -> dict:
    """final_odds (win:/place: の ×100 オッズ) 付き result (単複 P/L 用)。"""
    return {"finish_order": order, "trifecta_payout": payout,
            "final_odds": final_odds, "recorded_at": "2026-06-28T17:00:00"}


def _shobu_doc(rid: str, n_runners: int, recommended: bool = True) -> dict:
    return {
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [{"race_id": rid, "recommended": recommended, "venue": "A",
                   "race_no": 1, "race_type": "jra", "n_runners": n_runners}],
    }


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    sh = tmp_path / "shobu"; pr = tmp_path / "pred"; rs = tmp_path / "res"
    for d in (sh, pr, rs):
        d.mkdir()
    monkeypatch.setattr(store, "SHOBU_DIR", sh)
    monkeypatch.setattr(store, "PRED_DIR", pr)
    monkeypatch.setattr(store, "RESULT_DIR", rs)
    return sh, pr, rs


def test_indexed_is_superset_scoped_to_shobu(dirs):
    sh, pr, rs = dirs
    idx = {1: 90.0, 2: 80.0, 3: 70.0, 4: 60.0, 5: 50.0, 6: 40.0, 7: 30.0, 8: 20.0}
    # shobu scan: 1 recommended + 1 非recommended (どちらも評価対象)。
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [
            {"race_id": "rec-1", "recommended": True, "venue": "A", "race_no": 1,
             "race_type": "jra", "shobu_score": 100, "matched": ["claude"], "n_runners": 8},
            {"race_id": "non-1", "recommended": False, "venue": "B", "race_no": 2,
             "race_type": "nar", "shobu_score": 10, "matched": [], "n_runners": 8},
        ],
    }), encoding="utf-8")
    # snapshots + results for both
    for rid in ("rec-1", "non-1"):
        (pr / f"{rid}.json").write_text(json.dumps(_snap(8, idx)), encoding="utf-8")
        (rs / f"{rid}.json").write_text(json.dumps(_result([1, 2, 3], 5000)), encoding="utf-8")
    # betting-archive race: predictions/results にあるが shobu には無い → indexed に含めない
    (pr / "archive-9.json").write_text(json.dumps(_snap(8, idx)), encoding="utf-8")
    (rs / "archive-9.json").write_text(json.dumps(_result([1, 2, 3], 5000)), encoding="utf-8")

    rec = store.compute_shobu_pnl()
    allr = store.compute_indexed_pnl()
    rec_ids = {r["race_id"] for r in rec["races_detail"]}
    all_ids = {r["race_id"] for r in allr["races_detail"]}

    assert rec_ids == {"rec-1"}                       # 推奨のみ
    assert all_ids == {"rec-1", "non-1"}              # shobu 評価の全レース (推奨+非推奨)
    assert "archive-9" not in all_ids                 # ★ data/predictions 全体は混ぜない
    assert rec_ids <= all_ids                         # superset 不変条件
    assert allr["races"] == 2 and rec["races"] == 1
    assert allr["recommended_total"] == 2             # shobu 評価レース総数


def test_index_below_three_skipped(dirs):
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [{"race_id": "rec-1", "recommended": True, "venue": "A", "race_no": 1,
                   "race_type": "jra", "n_runners": 8}],
    }), encoding="utf-8")
    # 指数 2 頭のみ = BOX 不能 → no_index でカウントされ races=0
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, {1: 90.0, 2: 80.0})), encoding="utf-8")
    (rs / "rec-1.json").write_text(json.dumps(_result([1, 2, 3], 5000)), encoding="utf-8")
    rec = store.compute_shobu_pnl()
    assert rec["races"] == 0 and rec["skipped_no_index"] == 1


def test_box_hit_and_payout(dirs):
    sh, pr, rs = dirs
    idx = {1: 90.0, 2: 80.0, 3: 70.0, 4: 60.0, 5: 50.0, 6: 40.0, 7: 30.0, 8: 20.0}
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [{"race_id": "rec-1", "recommended": True, "venue": "A", "race_no": 1,
                   "race_type": "jra", "n_runners": 8}],
    }), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, idx)), encoding="utf-8")
    # 8頭 → 上位5頭BOX。着順 1-2-3 は上位5頭内 → 的中。
    (rs / "rec-1.json").write_text(json.dumps(_result([1, 2, 3], 12000)), encoding="utf-8")
    rec = store.compute_shobu_pnl(point_cost=100)
    d = rec["races_detail"][0]
    assert d["box"] == 5 and d["n_points"] == 60         # P(5,3)
    assert d["stake"] == 6000 and d["hit"] is True
    assert d["payout"] == 12000                          # trifecta_payout × (100/100)
    # 着順に BOX 外 (6着) が入れば不的中
    (rs / "rec-1.json").write_text(json.dumps(_result([1, 2, 6], 12000)), encoding="utf-8")
    rec2 = store.compute_shobu_pnl(point_cost=100)
    assert rec2["races_detail"][0]["hit"] is False and rec2["races_detail"][0]["payout"] == 0


# ----------------------------------------------------------------------------
# Claude 指数 単複戦略 (1位=単勝 / 2,3位=複勝) の仮想収支 (ユーザ指示 2026-06-30)
# ----------------------------------------------------------------------------

# idx 降順 = 馬番昇順 (1=Claude1位 ... 8=8位) になるよう指数を振る。
_IDX8 = {1: 90.0, 2: 80.0, 3: 70.0, 4: 60.0, 5: 50.0, 6: 40.0, 7: 30.0, 8: 20.0}


def test_winplace_full_hit_payout(dirs):
    """8頭(cutoff=3): #1単勝的中 + #2,#3複勝的中 の払戻と stake を検算。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 1-2-3 → top1=1勝ち / top2=2,top3=3 とも複勝圏。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 12000, {"win:1": 3.0, "place:2": 1.5, "place:3": 2.0})), encoding="utf-8")
    d = store.compute_shobu_winplace_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["win_hit"] is True and r["win_payout"] == 300        # 3.0×100
    assert r["place_hits"] == 2 and r["place_payout"] == 350      # 150 + 200
    assert r["stake"] == 300 and r["payout"] == 650               # 3脚 × ¥100
    # 集計: 単勝 1/1・複勝 2/2 (脚単位)。
    assert d["win_bets"] == 1 and d["win_hits"] == 1
    assert d["place_bets"] == 2 and d["place_hits"] == 2
    assert d["stake"] == 300 and d["payout"] == 650


def test_winplace_headcount_top2_only(dirs):
    """6頭(cutoff=2): 3着の馬は複勝圏外。#2は2着で的中・#3は3着で不的中。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 6)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(
        _snap(6, {1: 90, 2: 80, 3: 70, 4: 60, 5: 50, 6: 40})), encoding="utf-8")
    # 着順 4-2-3 → top1=1負け / top2=2 は2着=複勝圏 / top3=3 は3着=圏外(top-2のみ)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [4, 2, 3], 9999, {"win:1": 5.0, "place:2": 1.8, "place:3": 2.5})), encoding="utf-8")
    d = store.compute_shobu_winplace_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["place_cutoff"] == 2
    assert r["win_hit"] is False and r["win_payout"] == 0
    assert r["place_hits"] == 1 and r["place_payout"] == 180     # #2 のみ (1.8×100)
    assert r["stake"] == 300 and r["payout"] == 180


def test_winplace_no_place_when_le4(dirs):
    """4頭(cutoff=0): 複勝は発売なし → 単勝のみ。複勝脚は賭けない。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 4)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(
        _snap(4, {1: 90, 2: 80, 3: 70, 4: 60})), encoding="utf-8")
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 5000, {"win:1": 2.0})), encoding="utf-8")
    d = store.compute_shobu_winplace_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["place_cutoff"] == 0 and r["place_legs"] == []
    assert r["stake"] == 100 and r["payout"] == 200             # 単勝のみ
    assert d["place_bets"] == 0 and d["win_bets"] == 1


def test_winplace_no_odds_skips_when_hit_leg_missing(dirs):
    """的中脚の払戻オッズが欠落 → no_odds で分母外 (races=0)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # #1 勝ちだが final_odds に win:1 が無い → 評価不能。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo([1, 2, 3], 12000, {})), encoding="utf-8")
    d = store.compute_shobu_winplace_pnl(point_cost=100)
    assert d["races"] == 0 and d["skipped_no_odds"] == 1


def test_winplace_all_miss_counts_without_odds(dirs):
    """全脚不的中なら払戻オッズが無くても (外れ=¥0) レースは成立して集計される。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 6-7-8 → top1/2/3 (=1,2,3) は全滅。final_odds 空でも OK。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo([6, 7, 8], 12000, {})), encoding="utf-8")
    d = store.compute_shobu_winplace_pnl(point_cost=100)
    assert d["races"] == 1 and d["skipped_no_odds"] == 0
    assert d["stake"] == 300 and d["payout"] == 0
    assert d["win_hits"] == 0 and d["place_hits"] == 0


def test_winplace_indexed_is_superset(dirs):
    """単複も BOX と同じ母集団スコープ: indexed は recommended の superset・shobu 評価のみ。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [
            {"race_id": "rec-1", "recommended": True, "venue": "A", "race_no": 1,
             "race_type": "jra", "n_runners": 8},
            {"race_id": "non-1", "recommended": False, "venue": "B", "race_no": 2,
             "race_type": "nar", "n_runners": 8},
        ],
    }), encoding="utf-8")
    for rid in ("rec-1", "non-1"):
        (pr / f"{rid}.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
        (rs / f"{rid}.json").write_text(json.dumps(_result_fo(
            [1, 2, 3], 5000, {"win:1": 2.0, "place:2": 1.2, "place:3": 1.3})), encoding="utf-8")
    # archive: shobu に無い → 含めない
    (pr / "arch.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    (rs / "arch.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 5000, {"win:1": 2.0, "place:2": 1.2, "place:3": 1.3})), encoding="utf-8")
    rec = store.compute_shobu_winplace_pnl()
    allr = store.compute_indexed_winplace_pnl()
    assert {r["race_id"] for r in rec["races_detail"]} == {"rec-1"}
    assert {r["race_id"] for r in allr["races_detail"]} == {"rec-1", "non-1"}
    assert allr["recommended_total"] == 2
