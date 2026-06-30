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
          place_odds: dict[int, float] | None = None) -> dict:
    """全馬に Claude 指数を付けた snapshot (index_compare 形式)。

    win_odds/place_odds を渡すと bet_tables を作る (単勝/複勝 ≤1.1・単複 合成<1 フィルタの
    最終オッズ源)。未指定なら bet_tables 無し = フィルタ no-op (= オッズ不明なら買う)。
    """
    snap = {
        "n_runners": n_runners,
        "saved_at": "2026-06-28T11:00:00",
        "index_compare": [
            {"number": k, "claude_index": v, "market_index": 50.0} for k, v in idx.items()
        ],
    }
    bt: dict = {}
    if win_odds is not None:
        bt["win"] = [{"key": [n], "odds": o} for n, o in win_odds.items()]
    if place_odds is not None:
        bt["place"] = [{"key": [n], "odds": o} for n, o in place_odds.items()]
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
    # winplace = 1位単勝 + 1位複勝 (bet_tables 無し→合成フィルタ no-op)。300(win) + 110(place#1) = 410。
    assert r["per"]["winplace"]["payout"] == 410 and r["per"]["winplace"]["stake"] == 200
    # 戦略集計 (1レース)
    assert _strat(d, "win1")["roi"] == 3.0 and _strat(d, "win1")["net"] == 200
    assert _strat(d, "place2")["payout"] == 150 and _strat(d, "place3")["payout"] == 200
    assert _strat(d, "quinella12")["payout"] == 400 and _strat(d, "quinella12")["bets"] == 1
    assert _strat(d, "winplace")["stake"] == 200 and _strat(d, "winplace")["payout"] == 410


def test_strategies_quinella_needs_both_in_top2(dirs):
    """馬連#1-2 は #1,#2 が両方とも上位2着に入って初めて的中 (#1だけ勝ちでは外れ)。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    # 着順 1-5-2 → #1は1着だが #2(=馬番2)は3着 → 馬連{1,2}は上位2着{1,5}に収まらない=外れ。
    # 馬単1→2 も2着が馬番5なので外れ。#1 は1着で複勝圏 → place1 的中 (place:1 が要る)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 5, 2], 8000, {"win:1": 2.0, "place:1": 1.2, "place:2": 1.4, "place:3": 9.9})),
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
        {"win:1": 5.0, "place:2": 1.8, "place:3": 2.5, "trio:2-3-4": 30.0})), encoding="utf-8")
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
         "trifecta:1-2-3": 50.0, "trio:1-2-3": 10.0})),
        encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    r = d["races_detail"][0]
    assert r["place_cutoff"] == 0
    # 複勝は発売なし → place1/place2/place3 とも 0 脚
    assert r["per"]["place1"]["bets"] == 0
    assert r["per"]["place2"]["bets"] == 0 and r["per"]["place3"]["bets"] == 0
    assert r["per"]["winplace"]["stake"] == 100               # 単勝のみ (複勝なし)
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
    # winplace = 1位単勝+1位複勝 = 2脚 ¥200 (全外れ)。
    assert _strat(d, "winplace")["payout"] == 0 and _strat(d, "winplace")["stake"] == 200
    assert _strat(d, "win1")["hits"] == 0 and _strat(d, "quinella12")["hits"] == 0


def test_strategies_trifecta_trio_box_full_hit(dirs):
    """8頭: 着順 1-2-3 完全一致 → 3連単/3連複/3連複BOX すべて的中・配当を検算。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 9999,
        {"win:1": 2.0, "place:1": 1.1, "place:2": 1.2, "place:3": 1.3,
         "quinella:1-2": 3.0, "exacta:1-2": 5.0,
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
        [4, 2, 1], 9999, {"place:1": 2.2, "place:2": 1.4, "trio:1-2-4": 15.0})), encoding="utf-8")
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


def test_strategies_winplace_synth_filter(dirs):
    """単複=1位単勝+1位複勝。合成オッズ(=1位複勝/2)<1 なら買わない。ユーザ指示 2026-06-30。"""
    sh, pr, rs = dirs
    # 着順 1-5-6: #1 勝ち (単複の win/place 両的中) だが 馬連/馬単/3連系は全外れ (余計なオッズ不要)。
    # ケースA: #1 複勝1.8 → 合成0.9<1 → 買わない。
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(
        8, _IDX8, place_odds={1: 1.8})), encoding="utf-8")
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 5, 6], 9999, {"win:1": 3.0, "place:1": 1.8})), encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    assert _strat(d, "winplace")["races"] == 0    # 合成0.9<1 → 見送り

    # ケースB: #1 複勝2.4 → 合成1.2≥1 → 買う。win#1(3.0→300)+place#1(2.4→240)=540, stake200。
    (pr / "rec-1.json").write_text(json.dumps(_snap(
        8, _IDX8, place_odds={1: 2.4})), encoding="utf-8")
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 5, 6], 9999, {"win:1": 3.0, "place:1": 2.4})), encoding="utf-8")
    d2 = store.compute_shobu_strategies_pnl(point_cost=100)
    wp = next(r["per"]["winplace"] for r in d2["races_detail"])
    assert wp["bets"] == 2 and wp["stake"] == 200 and wp["payout"] == 540
    assert _strat(d2, "winplace")["races"] == 1 and _strat(d2, "winplace")["races_hit"] == 1


def test_strategies_hit_rate_denominator_is_races(dirs):
    """単複・3連複BOX の的中率の母数は **レース数** (races_hit/races)。ユーザ指示 2026-06-30。"""
    sh, pr, rs = dirs
    (sh / "20260628.json").write_text(json.dumps(_shobu_doc("rec-1", 8)), encoding="utf-8")
    (pr / "rec-1.json").write_text(json.dumps(_snap(8, _IDX8, place_odds={1: 3.0})), encoding="utf-8")
    # 着順 1-2-3 完全的中 (BOX 4脚中1脚的中, 単複 win+place とも的中)。
    (rs / "rec-1.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 9999,
        {"win:1": 2.0, "place:1": 3.0, "place:2": 1.5, "place:3": 2.0,
         "quinella:1-2": 3.0, "exacta:1-2": 4.0, "trifecta:1-2-3": 50.0, "trio:1-2-3": 10.0})),
        encoding="utf-8")
    d = store.compute_shobu_strategies_pnl(point_cost=100)
    box = _strat(d, "trio1234box")
    # BOX: 4脚/1レース、1脚的中。hit_rate は 1/1 (レース母数) であって 1/4 (脚母数) ではない。
    assert box["bets"] == 4 and box["races"] == 1 and box["races_hit"] == 1
    assert box["hit_rate"] == 1.0
    wp = _strat(d, "winplace")
    assert wp["bets"] == 2 and wp["races"] == 1 and wp["hit_rate"] == 1.0


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
         "trifecta:1-2-3": 60.0, "trio:1-2-3": 12.0})), encoding="utf-8")
    # archive: shobu に無い → 含めない
    (pr / "arch.json").write_text(json.dumps(_snap(8, _IDX8)), encoding="utf-8")
    (rs / "arch.json").write_text(json.dumps(_result_fo(
        [1, 2, 3], 5000,
        {"win:1": 2.0, "place:1": 1.1, "place:2": 1.2, "place:3": 1.3,
         "quinella:1-2": 3.0, "exacta:1-2": 4.0,
         "trifecta:1-2-3": 60.0, "trio:1-2-3": 12.0})), encoding="utf-8")
    rec = store.compute_shobu_strategies_pnl()
    allr = store.compute_indexed_strategies_pnl()
    assert {r["race_id"] for r in rec["races_detail"]} == {"rec-1"}
    assert {r["race_id"] for r in allr["races_detail"]} == {"rec-1", "non-1"}
    assert allr["recommended_total"] == 2
    # winplace = win1 + place1 (1位単勝+1位複勝) の整合 (bet_tables 無し=フィルタ no-op なので一致)
    wp = _strat(allr, "winplace")
    w1, p1 = _strat(allr, "win1"), _strat(allr, "place1")
    assert wp["bets"] == w1["bets"] + p1["bets"]
    assert wp["stake"] == w1["stake"] + p1["stake"]
    assert wp["payout"] == w1["payout"] + p1["payout"]
