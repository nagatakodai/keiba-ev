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
