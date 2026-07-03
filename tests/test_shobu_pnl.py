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


def _snap(n_runners: int, idx: dict[int, float], *,
          win_odds: dict[int, float] | None = None,
          place_odds: dict[int, float] | None = None,
          combo_odds: dict[str, dict[tuple, float]] | None = None,
          market: dict[int, float] | None = None,
          index_version: str | None = None) -> dict:
    """全馬に Claude 指数を付けた snapshot (index_compare 形式)。

    win_odds/place_odds を渡すと bet_tables を作る (単勝/複勝 ≤1.1・単複 合成<1 フィルタの
    最終オッズ源)。未指定なら bet_tables 無し = フィルタ no-op (= オッズ不明なら買う)。
    index_version を渡すと補強根拠バージョンを明示 (未指定は saved_at 由来で v2)。
    """
    snap = {
        "n_runners": n_runners,
        "saved_at": "2026-06-28T11:00:00",
        "index_compare": [
            {"number": k, "claude_index": v,
             "market_index": (market or {}).get(k, 50.0)} for k, v in idx.items()
        ],
    }
    if index_version is not None:
        snap["index_version"] = index_version
    bt: dict = {}
    if win_odds is not None:
        bt["win"] = [{"key": [n], "odds": o} for n, o in win_odds.items()]
    if place_odds is not None:
        bt["place"] = [{"key": [n], "odds": o} for n, o in place_odds.items()]
    if combo_odds is not None:   # {bet_type: {(a,b,...): odds}} — 全券種フィルタ用
        for btype, m in combo_odds.items():
            bt[btype] = [{"key": list(k), "odds": o} for k, o in m.items()]
    if bt:
        snap["bet_tables"] = bt
    return snap


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
# Claude 指数 単純戦略くらべ (単勝#1 / 複勝#2,3 / 馬連#1-2 / 単複) (ユーザ指示 2026-06-30)
# ----------------------------------------------------------------------------

# idx 降順 = 馬番昇順 (1=Claude1位 ... 8=8位) になるよう指数を振る。
_IDX8 = {1: 90.0, 2: 80.0, 3: 70.0, 4: 60.0, 5: 50.0, 6: 40.0, 7: 30.0, 8: 20.0}


def _strat(d: dict, key: str) -> dict:
    """strategies リストから key の戦略集計を取り出す。"""
    return next(s for s in d["strategies"] if s["key"] == key)


def test_strategies_full_hit_payout(dirs):
    """8頭(cutoff=3): #1単勝的中 + #2,#3複勝的中 + 馬連は#2が3着で不的中 を検算。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 1-2-3 → top1=1勝ち / #1,#2,#3 すべて複勝圏 / 馬連{1,2}・馬単1→2 とも的中。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 12000,
        {"win:1": 3.0, "place:1": 1.1, "place:2": 1.5, "place:3": 2.0,
         "quinella:1-2": 4.0, "exacta:1-2": 6.0,
         "wide:1-2": 1.5, "wide:1-3": 2.0, "wide:2-3": 2.5,
         "trifecta:1-2-3": 120.0, "trio:1-2-3": 18.0})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert (r["top1"], r["top2"], r["top3"]) == (1, 2, 3)
    # per-race 集計
    assert r["per"]["win1"] == {"stake": 100, "payout": 300, "bets": 1, "hits": 1, "hit": True}
    assert r["per"]["place1"]["payout"] == 110 and r["per"]["place1"]["hits"] == 1   # 1.1×100
    assert r["per"]["place2"]["payout"] == 150 and r["per"]["place2"]["hits"] == 1   # 1.5×100
    assert r["per"]["place3"]["payout"] == 200 and r["per"]["place3"]["hits"] == 1   # 2.0×100
    assert r["per"]["quinella12"]["payout"] == 400 and r["per"]["quinella12"]["hits"] == 1
    assert r["per"]["exacta12"]["payout"] == 600 and r["per"]["exacta12"]["hits"] == 1   # 1→2 着順一致
    assert r["per"]["wide13"]["payout"] == 200 and r["per"]["wide13"]["hits"] == 1   # wide:1-3=2.0
    # 戦略集計 (1レース)
    assert _strat(d, "win1")["roi"] == 3.0 and _strat(d, "win1")["net"] == 200
    assert _strat(d, "place2")["payout"] == 150 and _strat(d, "place3")["payout"] == 200
    assert _strat(d, "quinella12")["payout"] == 400 and _strat(d, "quinella12")["bets"] == 1


def test_strategies_quinella_needs_both_in_top2(dirs):
    """馬連#1-2 は #1,#2 が両方とも上位2着に入って初めて的中 (#1だけ勝ちでは外れ)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 1-5-2 → #1は1着だが #2(=馬番2)は3着 → 馬連{1,2}は上位2着{1,5}に収まらない=外れ。
    # 馬単1→2 も2着が馬番5なので外れ。#1 は1着で複勝圏 → place1 的中 (place:1 が要る)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 5, 2], 8000, {"win:1": 2.0, "place:1": 1.2, "place:2": 1.4, "place:3": 9.9,
                          "wide:1-2": 3.0})),   # ワイド{1,2} は上位3着 {1,5,2} に収まり的中
        encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["per"]["quinella12"]["hit"] is False and r["per"]["quinella12"]["payout"] == 0
    assert r["per"]["exacta12"]["hit"] is False                  # 馬単も外れ (2着が馬番5)
    assert r["per"]["win1"]["hit"] is True                       # 単勝は当たり
    assert r["per"]["place1"]["hits"] == 1                       # #1 は1着 → 複勝圏
    # #2(=馬番2) は3着で複勝圏 (cutoff=3) → place2 的中 / #3(=馬番3) は圏外 → place3 不的中
    assert r["per"]["place2"]["hits"] == 1 and r["per"]["place3"]["hits"] == 0


def test_strategies_headcount_top2_only(dirs):
    """6頭(cutoff=2): 3着の馬は複勝圏外。#2は2着で的中・#3は3着で不的中。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 6)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(
        _snap(6, {1: 90, 2: 80, 3: 70, 4: 60, 5: 50, 6: 40})), encoding="utf-8")
    # 着順 4-2-3 → top1=1負け / top2=2 は2着=複勝圏 / top3=3 は3着=圏外(top-2のみ) / 馬連外れ。
    # 上位3着 {2,3,4} は top4={1,2,3,4} に収まる → 3連複BOX のみ的中 (trio:2-3-4 が要る)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [4, 2, 3], 9999,
        {"win:1": 5.0, "place:2": 1.8, "place:3": 2.5, "trio:2-3-4": 30.0,
         "wide:2-3": 4.0})), encoding="utf-8")   # ワイド{2,3} は上位3着 {4,2,3} に収まり的中
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["place_cutoff"] == 2
    assert r["per"]["win1"]["hit"] is False
    assert r["per"]["place2"]["hits"] == 1 and r["per"]["place2"]["payout"] == 180   # #2 (1.8×100)
    assert r["per"]["place3"]["hits"] == 0 and r["per"]["place3"]["payout"] == 0     # #3 は圏外
    assert r["per"]["quinella12"]["hit"] is False


def test_strategies_no_place_when_le4(dirs):
    """4頭(cutoff=0): 複勝は発売なし → place2/place3 は 0 脚 (賭けない)。単勝・馬連は賭ける。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 4)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(
        _snap(4, {1: 90, 2: 80, 3: 70, 4: 60})), encoding="utf-8")
    # 着順 1-2-3 → 単勝・馬連・馬単・3連単・3連複・BOX 的中 / 複勝は発売なし (4頭以下)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 5000,
        {"win:1": 2.0, "quinella:1-2": 3.0, "exacta:1-2": 4.0,
         "wide:1-2": 1.5, "wide:1-3": 2.0, "wide:2-3": 2.5,
         "trifecta:1-2-3": 50.0, "trio:1-2-3": 10.0})),
        encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["place_cutoff"] == 0
    # 複勝は発売なし → place1/place2/place3 とも 0 脚
    assert r["per"]["place1"]["bets"] == 0
    assert r["per"]["place2"]["bets"] == 0 and r["per"]["place3"]["bets"] == 0
    # place2/place3 はこのレースで 0 脚 → 戦略の対象レースにカウントしない
    assert _strat(d, "place2")["races"] == 0 and _strat(d, "place3")["races"] == 0
    assert _strat(d, "win1")["races"] == 1 and _strat(d, "quinella12")["races"] == 1


def test_strategies_no_odds_skips_when_hit_leg_missing(dirs):
    """的中脚の払戻オッズが欠落 → no_odds で分母外 (races=0)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # #1 勝ちだが final_odds に win:1 が無い → 評価不能。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo([1, 2, 3], 12000, {})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    assert d["races"] == 0 and d["skipped_no_odds"] == 1


def test_strategies_all_miss_counts_without_odds(dirs):
    """全脚不的中なら払戻オッズが無くても (外れ=¥0) レースは成立して集計される。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 6-7-8 → top1/2/3 (=1,2,3) は全滅・馬連も外れ。final_odds 空でも OK。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo([6, 7, 8], 12000, {})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    assert d["races"] == 1 and d["skipped_no_odds"] == 0
    assert _strat(d, "win1")["hits"] == 0 and _strat(d, "quinella12")["hits"] == 0
    assert _strat(d, "trio1234box")["payout"] == 0


def test_strategies_trifecta_trio_box_full_hit(dirs):
    """8頭: 着順 1-2-3 完全一致 → 3連単/3連複/3連複BOX すべて的中・配当を検算。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 9999,
        {"win:1": 2.0, "place:1": 1.1, "place:2": 1.2, "place:3": 1.3,
         "quinella:1-2": 3.0, "exacta:1-2": 5.0,
         "wide:1-2": 1.5, "wide:1-3": 2.0, "wide:2-3": 2.5,
         "trifecta:1-2-3": 100.0, "trio:1-2-3": 20.0})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["per"]["trifecta123"] == {
        "stake": 100, "payout": 10000, "bets": 1, "hits": 1, "hit": True}   # 100.0×100
    assert r["per"]["trio123"]["payout"] == 2000 and r["per"]["trio123"]["bets"] == 1
    # 3連複BOX: top4={1,2,3,4} の C(4,3)=4 点・当たりは {1,2,3} の 1 点 → 配当は trio 1 口分。
    box = r["per"]["trio1234box"]
    assert box["bets"] == 4 and box["stake"] == 400 and box["hits"] == 1 and box["payout"] == 2000


def test_strategies_trio_box_within_top4_only(dirs):
    """8頭: 着順 4-2-1 → 3連単/3連複(1-2-3) は外れ・3連複BOX(1-2-3-4) のみ的中 (4着内に4位馬)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 上位3着 {1,2,4} は {1,2,3} と不一致 → trio123 外れ。だが {1,2,3,4} には収まる → BOX 的中。
    # #1(=馬番1) は3着で複勝圏 → place1 的中 (place:1 が要る)。馬単1→2 は1着が馬番4で外れ。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [4, 2, 1], 9999, {"place:1": 2.2, "place:2": 1.4, "trio:1-2-4": 15.0,
                          "wide:1-2": 2.6})), encoding="utf-8")   # ワイド{1,2} は {4,2,1} に収まり的中
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["per"]["trifecta123"]["hit"] is False            # 順序も集合も違う
    assert r["per"]["trio123"]["hit"] is False                # {1,2,4} ≠ {1,2,3}
    box = r["per"]["trio1234box"]
    assert box["bets"] == 4 and box["hits"] == 1 and box["payout"] == 1500   # trio:1-2-4=15.0
    assert r["per"]["win1"]["hit"] is False and r["per"]["quinella12"]["hit"] is False
    assert r["per"]["exacta12"]["hit"] is False               # 馬単も外れ (1着が馬番4)
    assert r["per"]["place1"]["hits"] == 1                    # #1 は3着 → 複勝圏
    assert r["per"]["place2"]["hits"] == 1 and r["per"]["place3"]["hits"] == 0


def test_strategies_wide(dirs):
    """ワイド (指数1-2位 / 1-3位) と ワイドBOX (1-2-3) — 両馬が上位3着で的中。ユーザ指示 2026-06-30 / wide13 は 2026-07-02。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 1-3-5: 上位3着 {1,3,5}。wide12{1,2}は2が圏外で外れ。wide13{1,3}は両方圏内で的中。
    # wideBOX: (1,2)外れ / (1,3){1,3}⊆{1,3,5}的中 / (2,3)外れ。→ 1点的中。
    # #3(=馬番3) は2着で複勝圏 → place3 的中 (place:3 が要る)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 3, 5], 9999, {"win:1": 2.0, "place:1": 1.5, "place:3": 1.8, "wide:1-3": 2.2})),
        encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["per"]["wide12"]["hit"] is False                 # {1,2}: 2 は圏外
    w13 = r["per"]["wide13"]
    assert w13["hit"] is True and w13["payout"] == 220        # {1,3} 両方圏内・2.2×100
    assert _strat(d, "wide13")["races_hit"] == 1 and _strat(d, "wide13")["stake"] == 100
    box = r["per"]["wide123box"]
    assert box["bets"] == 3 and box["hits"] == 1 and box["payout"] == 220   # (1,3) のみ的中
    assert _strat(d, "wide123box")["races_hit"] == 1          # 母数はレース数


def test_strategies_all_bets_skip_low_odds(dirs):
    """**全券種** で最終オッズ ≤1.1 なら買わない (馬連の例)。ユーザ指示 2026-06-30。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    # 馬連 {1,2} のスナップ最終オッズ 1.05 (≤1.1) → quinella12 は買わない。
    (pr / "rec-1.json").write_text(json.dumps(_snap(
        8, _IDX8, combo_odds={"quinella": {(1, 2): 1.05}})), encoding="utf-8")
    # 着順 6-7-8: 全戦略外れ (余計なオッズ不要)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo([6, 7, 8], 9999, {})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    assert _strat(d, "quinella12")["races"] == 0    # 馬連1.05 ≤1.1 → 買わない
    # ワイド/単勝はスナップに ≤1.1 オッズが無い → 買う (races=1, ただし着順外れ)
    assert _strat(d, "wide12")["races"] == 1
    assert _strat(d, "win1")["races"] == 1


def test_strategies_win_place_skip_low_odds(dirs):
    """単勝/複勝は最終オッズ ≤1.1 なら買わない (races から外れる)。ユーザ指示 2026-06-30。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    # #1 の最終オッズ: 単勝1.1(≤1.1=買わない) / 複勝1.0(≤1.1=買わない)。#2 複勝1.5(>1.1=買う)。
    (pr / "rec-1.json").write_text(json.dumps(_snap(
        8, _IDX8, win_odds={1: 1.1, 2: 4.0}, place_odds={1: 1.0, 2: 1.5})), encoding="utf-8")
    # 着順 2-4-6: 馬連/馬単/3連単/3連複/BOX は全外れ (余計なオッズ不要)。#2 は2着で複勝圏。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [2, 4, 6], 9999, {"place:2": 1.5})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    # win1: #1 単勝1.1 → 買わない → races=0
    assert _strat(d, "win1")["races"] == 0 and _strat(d, "win1")["bets"] == 0
    # place1: #1 複勝1.0 → 買わない → races=0
    assert _strat(d, "place1")["races"] == 0
    # place2: #2 複勝1.5(>1.1) → 買う・的中 (2着)
    assert _strat(d, "place2")["races"] == 1 and _strat(d, "place2")["races_hit"] == 1
    assert _strat(d, "place2")["payout"] == 150


def test_strategies_hit_rate_denominator_is_races(dirs):
    """3連複BOX の的中率の母数は **レース数** (races_hit/races) であって脚数ではない。ユーザ指示 2026-06-30。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 1-2-3 完全的中 (BOX 4脚中1脚的中)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 9999,
        {"win:1": 2.0, "place:1": 3.0, "place:2": 1.5, "place:3": 2.0,
         "quinella:1-2": 3.0, "exacta:1-2": 4.0,
         "wide:1-2": 1.5, "wide:1-3": 2.0, "wide:2-3": 2.5,
         "trifecta:1-2-3": 50.0, "trio:1-2-3": 10.0})),
        encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    box = _strat(d, "trio1234box")
    # BOX: 4脚/1レース、1脚的中。hit_rate は 1/1 (レース母数) であって 1/4 (脚母数) ではない。
    assert box["bets"] == 4 and box["races"] == 1 and box["races_hit"] == 1
    assert box["hit_rate"] == 1.0


def test_strategies_version_split(dirs):
    """version="v1"/"v2" で補強根拠バージョン毎に母集団を分離する (ユーザ指示 2026-06-30)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [
            {"race_id": "v2-1", "recommended": True, "venue": "A", "race_no": 1,
             "race_type": "jra", "n_runners": 8},
            {"race_id": "v1-1", "recommended": True, "venue": "B", "race_no": 2,
             "race_type": "nar", "n_runners": 8},
        ],
    }), encoding="utf-8")
    (pr / "v2-1.json").write_text(json.dumps(_snap(8, _IDX8, index_version="v2")), encoding="utf-8")
    (pr / "v1-1.json").write_text(json.dumps(_snap(8, _IDX8, index_version="v1")), encoding="utf-8")
    for rid in ("v2-1", "v1-1"):
        (rs / f"{rid}.json").write_text(json.dumps(_result_fo(
            [6, 7, 8], 9999, {})), encoding="utf-8")   # 全外れ (オッズ不要)
    v2 = store.compute_shobu_strategies_pnl(version="v2")
    v1 = store.compute_shobu_strategies_pnl(version="v1")
    allv = store.compute_shobu_strategies_pnl()
    assert {r["race_id"] for r in v2["races_detail"]} == {"v2-1"} and v2["version"] == "v2"
    assert {r["race_id"] for r in v1["races_detail"]} == {"v1-1"} and v1["version"] == "v1"
    assert allv["races"] == 2 and allv["version"] is None
    # BOX 側も同様に分離
    b2 = store.compute_shobu_pnl(version="v2")
    assert b2["races"] == 1 and b2["recommended_total"] == 1


def test_index_version_beta_for_market_derived(dirs):
    """市場由来 cutoff (2026-06-21 19:04) 以前に採点した snapshot は β に分類される。"""
    sh, pr, rs = dirs
    (sh / "20260620.json").write_text(json.dumps({
        "generated_at": "2026-06-20T11:00:00+09:00",
        "races": [{"race_id": "b-1", "recommended": True, "venue": "C", "race_no": 1,
                   "race_type": "nar", "n_runners": 8}],
    }), encoding="utf-8")
    snap = _snap(8, _IDX8)
    snap["llm_scored_at"] = "2026-06-20T15:00:00"   # cutoff より前 → β
    (pr / "b-1.json").write_text(json.dumps(snap), encoding="utf-8")
    (rs / "b-1.json").write_text(json.dumps(_result_fo([6, 7, 8], 9999, {})), encoding="utf-8")
    beta = store.compute_shobu_strategies_pnl(version="β")
    v2 = store.compute_shobu_strategies_pnl(version="v2")
    assert {r["race_id"] for r in beta["races_detail"]} == {"b-1"}
    assert v2["races"] == 0   # β レースは v2 に含めない


def test_market_agreement_splits_by_consensus(dirs):
    """Claude#1==市場1番人気か で agree/disagree に分割される (ユーザ指示 2026-06-30)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [
            {"race_id": "ag-1", "recommended": True, "venue": "A", "race_no": 1,
             "race_type": "nar", "n_runners": 8},
            {"race_id": "dis-1", "recommended": True, "venue": "B", "race_no": 2,
             "race_type": "nar", "n_runners": 8},
        ],
    }), encoding="utf-8")
    # ag-1: Claude#1=馬番1 / 市場#1も馬番1 → 一致。
    (pr / "ag-1.json").write_text(json.dumps(_snap(
        8, _IDX8, market={1: 95.0, 2: 60.0})), encoding="utf-8")
    # dis-1: Claude#1=馬番1 だが 市場#1=馬番2 → 不一致 (Claude contrarian)。
    (pr / "dis-1.json").write_text(json.dumps(_snap(
        8, _IDX8, market={1: 40.0, 2: 95.0})), encoding="utf-8")
    for rid in ("ag-1", "dis-1"):
        (rs / f"{rid}.json").write_text(json.dumps(_result_fo([6, 7, 8], 9999, {})), encoding="utf-8")
    m = store.compute_market_agreement()
    assert m["races"] == 2 and m["agree_n"] == 1 and m["disagree_n"] == 1
    # 各 metric は agree/disagree 双方の脚数を持つ
    quin = next(x for x in m["metrics"] if x["key"] == "quinella12")
    assert quin["agree_legs"] == 1 and quin["disagree_legs"] == 1
    # pooled ターゲット (combo=馬連+馬単+ワイド) は **レース単位合算** (2026-07-04):
    # 1 レース 3 脚でも bootstrap の再標本化単位は 1 点 (レース内相関で CI が過小になるのを防ぐ)。
    combo = next(x for x in m["metrics"] if x["key"] == "combo")
    assert combo["agree_legs"] == 1 and combo["disagree_legs"] == 1

    # history 追記 + dedup (races 不変なら no-op)
    import api.store as st
    st.MARKET_AGREEMENT_HISTORY = rs / "mkt_hist.jsonl"   # tmp に向ける
    row = store.append_market_agreement_history()
    assert row is not None and row["races"] == 2
    assert store.append_market_agreement_history() is None   # dedup
    assert len(store.market_agreement_history()) == 1


def test_venue_breakdown_groups_by_venue(dirs):
    """compute_venue_breakdown が per-race を venue で集計する (ユーザ指示 2026-06-30)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps({
        "generated_at": "2026-06-28T11:42:00+09:00",
        "races": [
            {"race_id": "kn-1", "recommended": True, "venue": "金沢", "race_no": 1,
             "race_type": "nar", "n_runners": 8},
            {"race_id": "kn-2", "recommended": True, "venue": "金沢", "race_no": 2,
             "race_type": "nar", "n_runners": 8},
            {"race_id": "ng-1", "recommended": True, "venue": "名古屋", "race_no": 1,
             "race_type": "nar", "n_runners": 8},
        ],
    }), encoding="utf-8")
    for rid in ("kn-1", "kn-2", "ng-1"):
        (pr / f"{rid}.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
        (rs / f"{rid}.json").write_text(json.dumps(_result_fo([6, 7, 8], 9999, {})), encoding="utf-8")
    vb = store.compute_venue_breakdown(version="v2")
    by = {v["venue"]: v for v in vb["venues"]}
    assert by["金沢"]["box"]["races"] == 2 and by["名古屋"]["box"]["races"] == 1
    # venues は対象レース数の多い順 → 金沢 (2) が先頭
    assert vb["venues"][0]["venue"] == "金沢"
    # winplace は撤去済 → 戦略内訳に存在しない
    assert all(s["key"] != "winplace" for s in by["金沢"]["strategies"])


def test_strategies_indexed_is_superset(dirs):
    """戦略くらべも BOX と同じ母集団スコープ: indexed は recommended の superset・shobu 評価のみ。"""
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
            [1, 2, 3], 5000,
        {"win:1": 2.0, "place:1": 1.1, "place:2": 1.2, "place:3": 1.3,
         "quinella:1-2": 3.0, "exacta:1-2": 4.0,
         "wide:1-2": 1.5, "wide:1-3": 2.0, "wide:2-3": 2.5,
         "trifecta:1-2-3": 60.0, "trio:1-2-3": 12.0})), encoding="utf-8")
    # archive: shobu に無い → 含めない
    (pr / "arch.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    (rs / "arch.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 5000,
        {"win:1": 2.0, "place:1": 1.1, "place:2": 1.2, "place:3": 1.3,
         "quinella:1-2": 3.0, "exacta:1-2": 4.0,
         "wide:1-2": 1.5, "wide:1-3": 2.0, "wide:2-3": 2.5,
         "trifecta:1-2-3": 60.0, "trio:1-2-3": 12.0})), encoding="utf-8")
    rec = store.compute_shobu_strategies_pnl()
    allr = store.compute_indexed_strategies_pnl()
    assert {r["race_id"] for r in rec["races_detail"]} == {"rec-1"}
    assert {r["race_id"] for r in allr["races_detail"]} == {"rec-1", "non-1"}
    assert allr["recommended_total"] == 2
    # winplace (単複) は全戦略から撤去済み (ユーザ指示 2026-06-30)。
    assert all(s["key"] != "winplace" for s in allr["strategies"])


def test_eval_races_dedup_before_recommended_filter(dirs):
    """同一 race_id が複数 shobu file にあるとき、recommended 判定は generated_at 後勝ちの
    **最新コピー** で行う (2026-07-04 修正)。旧実装はフィルタが dedup より先で、最新スキャンで
    非推奨化されたレースの古い recommended=True コピーが母集団に残留した。"""
    sh, pr, rs = dirs
    idx = {1: 90.0, 2: 80.0, 3: 70.0}
    # file A (古い): recommended=True / file B (新しい): 同一 race_id が recommended=False に降格
    (sh / "20260701.json").write_text(json.dumps({
        "generated_at": "2026-07-01T10:00:00+09:00",
        "races": [{"race_id": "demoted-1", "recommended": True, "venue": "A",
                   "race_no": 1, "race_type": "nar", "n_runners": 8}],
    }), encoding="utf-8")
    (sh / "20260702.json").write_text(json.dumps({
        "generated_at": "2026-07-02T10:00:00+09:00",
        "races": [{"race_id": "demoted-1", "recommended": False, "venue": "A",
                   "race_no": 1, "race_type": "nar", "n_runners": 8}],
    }), encoding="utf-8")
    (pr / "demoted-1.json").write_text(json.dumps(_snap(8, idx)), encoding="utf-8")
    (rs / "demoted-1.json").write_text(json.dumps(_result([1, 2, 3], 5000)), encoding="utf-8")
    assert store._shobu_eval_races(True) == {}                   # 最新は非推奨 → 推奨母集団外
    assert set(store._shobu_eval_races(False)) == {"demoted-1"}  # 全体母集団には居る


def test_strategy_index_tie_breaks_by_number(dirs):
    """Claude 指数同点は **馬番昇順** で明示タイブレーク (index_compare の行順に依存しない)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("tie-1", 8)), encoding="utf-8")
    # 馬5 と馬1 が同点1位。行順を馬番降順で書いても top1 は馬番昇順の馬1。
    snap = _snap(8, {5: 90.0, 1: 90.0, 3: 70.0, 7: 70.0})
    snap["index_compare"] = list(reversed(snap["index_compare"]))
    (pr / "tie-1.json").write_text(json.dumps(snap), encoding="utf-8")
    # 着順は全戦略外れ (的中脚の final_odds 欠落で no_odds skip にならないように)。
    (rs / "tie-1.json").write_text(json.dumps(_result_fo([8, 6, 4], 9999, {})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert (r["top1"], r["top2"], r["top3"]) == (1, 5, 3)   # 同点 90.0 は 1<5、70.0 は 3<7


def test_dead_heat_third_place_hits(dirs):
    """3着同着 (finish_positions が 4 頭): 同着側の的中を取りこぼさない (2026-07-04)。

    実例 (data/results/2026360622-622-7 型): 着順 9-7-{8,10}。finish_order は先勝ちで
    [9,7,8] だが、Claude top3=(9,7,10) の 3連単/3連複/ワイド/複勝 は 10 側でも的中。
    ワイドの 3着同着同士 (8-10) は不的中。
    """
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("dh-1", 10)), encoding="utf-8")
    # Claude 順位: top1=9, top2=7, top3=10, top4=8
    (pr / "dh-1.json").write_text(json.dumps(_snap(
        10, {9: 90.0, 7: 80.0, 10: 70.0, 8: 60.0, 1: 10.0})), encoding="utf-8")
    res = {
        "finish_order": [9, 7, 8],
        "finish_positions": {"9": 1, "7": 2, "8": 3, "10": 3},   # 3着同着
        "trifecta_payout": 4480,
        "final_odds": {"win:9": 2.0, "place:9": 1.2, "place:7": 1.5, "place:8": 1.6,
                       "place:10": 1.7, "wide:9-10": 4.0, "wide:7-10": 7.2, "wide:7-9": 2.0,
                       "quinella:7-9": 5.0, "exacta:9-7": 8.0,
                       "trio:7-8-9": 15.0, "trio:7-9-10": 17.3, "trifecta:9-7-10": 44.8},
        "recorded_at": "2026-06-28T17:00:00",
    }
    (rs / "dh-1.json").write_text(json.dumps(res), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert (r["top1"], r["top2"], r["top3"]) == (9, 7, 10)
    assert r["per"]["trifecta123"]["hit"] is True     # 9→7→10 は同着側の的中組
    assert r["per"]["trifecta123"]["payout"] == 4480  # trifecta:9-7-10=44.8 ×100
    assert r["per"]["trio123"]["hit"] is True and r["per"]["trio123"]["payout"] == 1730
    assert r["per"]["wide13"]["hit"] is True and r["per"]["wide13"]["payout"] == 400
    assert r["per"]["place3"]["hit"] is True and r["per"]["place3"]["payout"] == 170  # 10 は3着
    # BOX (top4 = 9,7,10,8): 的中組 (9,7,8) と (9,7,10) の両方を含む trio → 2 点的中
    box = r["per"]["trio1234box"]
    assert box["hits"] == 2      # trio{7,8,9} と trio{7,9,10} の両方が top3_pat [1,2,3] に一致
    assert box["payout"] == 1500 + 1730


def test_dead_heat_wide_third_pair_misses(dirs):
    """3着同着同士のワイド (rank 3,3) は不的中 (JRA/NAR 払戻ルール)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("dh-2", 10)), encoding="utf-8")
    # Claude top1=8, top2=10 (両方3着同着), top3=9
    (pr / "dh-2.json").write_text(json.dumps(_snap(
        10, {8: 90.0, 10: 80.0, 9: 70.0, 7: 60.0})), encoding="utf-8")
    res = {
        "finish_order": [9, 7, 8],
        "finish_positions": {"9": 1, "7": 2, "8": 3, "10": 3},
        "trifecta_payout": 4480,
        "final_odds": {"place:8": 1.6, "place:9": 1.2, "place:10": 1.7, "wide:8-9": 3.0,
                       "wide:9-10": 4.0, "trio:7-8-9": 15.0, "trio:7-9-10": 17.3},
        "recorded_at": "2026-06-28T17:00:00",
    }
    (rs / "dh-2.json").write_text(json.dumps(res), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["per"]["wide12"]["hit"] is False    # {8,10} = 3着同着同士 → 不的中
    assert r["per"]["quinella12"]["hit"] is False
    # 複勝は両方的中 (8 も 10 も 3着)
    assert r["per"]["place1"]["hit"] is True and r["per"]["place2"]["hit"] is True


def test_dead_heat_second_exacta_both_orders(dirs):
    """2着同着: 馬単は 1着→2着a / 1着→2着b の両順序が的中、2着a-2着b の馬連は不的中。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("dh-3", 8)), encoding="utf-8")
    # Claude top1=5, top2=6 (6 は2着同着の片方), top3=4
    (pr / "dh-3.json").write_text(json.dumps(_snap(
        8, {5: 90.0, 6: 80.0, 4: 70.0})), encoding="utf-8")
    res = {
        "finish_order": [5, 4, 6],
        "finish_positions": {"5": 1, "4": 2, "6": 2},   # 2着同着 (4 と 6)
        "trifecta_payout": 3510,
        "final_odds": {"win:5": 2.0, "place:5": 1.2, "place:4": 1.4, "place:6": 1.5,
                       "quinella:5-6": 5.7, "exacta:5-6": 10.2,
                       "wide:4-5": 1.8, "wide:4-6": 2.2, "wide:5-6": 1.9,
                       "trio:4-5-6": 8.0, "trifecta:5-6-4": 35.1},
        "recorded_at": "2026-06-28T17:00:00",
    }
    (rs / "dh-3.json").write_text(json.dumps(res), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["per"]["quinella12"]["hit"] is True     # {5,6} = 1着+2着同着片方 → 的中
    assert r["per"]["exacta12"]["hit"] is True       # 5→6 (rank 1→2) → 的中
    assert r["per"]["exacta12"]["payout"] == 1020
    # 3連単 5→6→4 は rank (1,2,2) 非減少 → 的中 (同着の両順序が払戻対象)
    assert r["per"]["trifecta123"]["hit"] is True and r["per"]["trifecta123"]["payout"] == 3510
