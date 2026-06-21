"""src/shobu.py (今日の勝負レース スキャン) のロジック test。

ネットワークには出ない: discover_today_races / 最新オッズ fetch / snapshot 読込を monkeypatch する。
"""
from __future__ import annotations

import time

import src.shobu as shobu


# ---------------------------------------------------------------- pure 関数 --

def test_separation_concentrated_vs_uniform():
    """集中したフィールドは高 sep、一様フィールドは ~0。"""
    conc = shobu._implied_from_win_odds({1: 1.3, 2: 8.0, 3: 14.0, 4: 22.0, 5: 33.0})
    unif = shobu._implied_from_win_odds({1: 5.0, 2: 5.1, 3: 4.9, 4: 5.2, 5: 4.8})
    s_conc = shobu._separation(conc)
    s_unif = shobu._separation(unif)
    assert s_conc["score"] > 25
    assert s_unif["score"] < 5
    assert s_conc["score"] > s_unif["score"]
    # favorites は prob 降順の top3。
    assert [f["number"] for f in s_conc["favorites"]] == [1, 2, 3]


def test_separation_needs_two_horses():
    assert shobu._separation({1: 1.0}) is None
    assert shobu._separation({}) is None


def test_implied_from_market_index_roundtrip():
    """market_win_index (=100·p^(1/1.5)) から implied 勝率を復元できる。"""
    # 単勝 1.5 / 8 / 12 倍相当の指数。
    mwi = {
        "1": 100.0 * (1 / 1.5) ** (1 / 1.5),
        "2": 100.0 * (1 / 8.0) ** (1 / 1.5),
        "3": 100.0 * (1 / 12.0) ** (1 / 1.5),
    }
    implied = shobu._implied_from_market_index(mwi)
    assert abs(sum(implied.values()) - 1.0) < 1e-6
    # 1.5 倍が最大の implied を持つ。
    assert max(implied, key=implied.get) == 1


def test_claude_edge_rank_divergence():
    """市場2番人気を Claude が本命視 = 順位乖離。top_rank_gap と edge を正しく出す。"""
    # 市場: 1番(90)>2番(70)>3番(50) / Claude: 2番(85)>1番(60)>3番(40)
    snap = {"index_compare": [
        {"number": 1, "name": "Y", "claude_index": 60, "market_index": 90},  # 市場1位 Claude2位
        {"number": 2, "name": "X", "claude_index": 85, "market_index": 70},  # 市場2位 Claude1位
        {"number": 3, "name": "Z", "claude_index": 40, "market_index": 50},
    ]}
    e = shobu._claude_edge(snap, value_floor=3.0)
    assert e["top_pick"]["number"] == 2          # Claude 本命
    assert e["top_pick"]["market_rank"] == 2     # 市場2番人気
    assert e["top_rank_gap"] == 1                # 「市場2位なのに Claude1位」
    nums = [h["number"] for h in e["edge_horses"]]
    assert 2 in nums                             # rank_gap=1, diff=15 ≥ floor
    assert 1 not in nums                          # rank_gap=-1 (市場が上)
    # score = top_rank_gap*20 + (rank_gap*5 + diff*0.4) = 20 + (5 + 6) = 31
    assert e["score"] == 31.0


def test_claude_edge_value_floor_blocks_weak_diff():
    """順位は乖離しても指数差が小さければ edge にしない (top_rank_gap は score に残る)。"""
    snap = {"index_compare": [
        {"number": 1, "claude_index": 70, "market_index": 80},  # 市場1位 Claude2位
        {"number": 6, "claude_index": 78, "market_index": 75},  # 市場2位 Claude1位 diff=3
        {"number": 3, "claude_index": 40, "market_index": 50},
    ]}
    e = shobu._claude_edge(snap, value_floor=10.0)   # diff 3 < 10
    assert e["edge_count"] == 0
    assert e["top_rank_gap"] == 1
    assert e["score"] == 20.0                         # top_rank_gap*20 のみ


def test_claude_edge_fallback_ranks_from_dicts():
    """index_compare 無しでも llm/market 指数の両方から順位乖離を出す。"""
    snap = {"llm_win_index": {"1": 60, "2": 85, "3": 40},
            "market_win_index": {"1": 90, "2": 70, "3": 50}}
    e = shobu._claude_edge(snap, value_floor=3.0)
    assert e["top_pick"]["number"] == 2
    assert e["top_rank_gap"] == 1


def test_claude_edge_none_when_insufficient():
    # claude 指数なし
    assert shobu._claude_edge({"market_win_index": {"1": 50}}, value_floor=3.0) is None
    assert shobu._claude_edge({}, value_floor=3.0) is None
    # 両指数が揃う馬が 2 頭未満 → ランク比較不能
    snap = {"index_compare": [{"number": 1, "claude_index": 70, "market_index": 80}]}
    assert shobu._claude_edge(snap, value_floor=3.0) is None


def test_race_type_and_internal_id():
    assert shobu._race_type("202605010102", "keibabook") == "jra"
    assert shobu._race_type("202632060101", "oddspark") == "nar"
    # source 不明でも netkeiba 場コードで判定。
    assert shobu._race_type("202605010102", "") == "jra"
    assert shobu._race_type("202632060101", "") == "nar"
    assert shobu._internal_id("202605010102") == "20260501-1-2"


def test_race_type_banei_separated():
    """帯広ばんえい (場コード 65) は source に関わらず banei に分離 (ev.segment_of_rd と同じ)。"""
    assert shobu._race_type("202665062001", "oddspark") == "banei"
    assert shobu._race_type("202665062001", "") == "banei"
    # nar フィルタは banei を含まない (rtype が異なるため)。
    assert shobu._race_type("202665062001", "oddspark") != "nar"


def test_scan_banei_filter(monkeypatch):
    """race_type=banei は帯広だけ / nar は帯広を除外。by_type も分離して数える。"""
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: [
        {"race_id": "202632060101", "url": "u", "start_at": now + 3600,
         "venue": "佐賀", "race_no": 1, "source": "oddspark"},      # 平地NAR
        {"race_id": "202665062001", "url": "u", "start_at": now + 3600,
         "venue": "帯広", "race_no": 1, "source": "oddspark"},      # ばんえい
    ])
    monkeypatch.setattr(shobu, "_load_snapshot", lambda i: None)
    monkeypatch.setattr(shobu, "_fetch_fresh_win", lambda r, t: None)

    res_all = shobu.scan(race_type="all", fetch_odds=False, claude_eval=0, log=lambda *_: None)
    assert res_all["summary"]["by_type"] == {"jra": 0, "nar": 1, "banei": 1}

    res_nar = shobu.scan(race_type="nar", fetch_odds=False, claude_eval=0, log=lambda *_: None)
    assert [r["race_type"] for r in res_nar["races"]] == ["nar"]

    res_banei = shobu.scan(race_type="banei", fetch_odds=False, claude_eval=0, log=lambda *_: None)
    assert [r["race_type"] for r in res_banei["races"]] == ["banei"]
    assert res_banei["races"][0]["venue"] == "帯広"


# ---------------------------------------------------------------- scan() ----

def _fake_discovery(now: int):
    """NAR 1 + JRA 1 の未来開催 2 件 (discover_today_races の戻り値形)。"""
    return [
        {"race_id": "202632060101", "url": "u1", "start_at": now + 3600,
         "venue": "佐賀", "race_no": 1, "source": "oddspark"},     # NAR
        {"race_id": "202605010111", "url": "u2", "start_at": now + 7200,
         "venue": "東京", "race_no": 11, "source": "keibabook"},   # JRA
    ]


def _setup(monkeypatch, *, fresh_a, fresh_b, snap_a, snap_b):
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: _fake_discovery(now))

    nar_internal = shobu._internal_id("202632060101")
    jra_internal = shobu._internal_id("202605010111")

    def fake_snap(internal):
        return {nar_internal: snap_a, jra_internal: snap_b}.get(internal)

    def fake_fresh(rid, rtype):
        return {"202632060101": fresh_a, "202605010111": fresh_b}.get(rid)

    monkeypatch.setattr(shobu, "_load_snapshot", fake_snap)
    monkeypatch.setattr(shobu, "_fetch_fresh_win", fake_fresh)
    return nar_internal, jra_internal


# B race の snapshot: 市場2番人気を Claude が本命視 (順位乖離) → 基準B 合格。
_SNAP_DIVERGENCE = {"index_compare": [
    {"number": 1, "claude_index": 60, "market_index": 90},   # 市場1位 Claude2位
    {"number": 2, "claude_index": 85, "market_index": 70},   # 市場2位 Claude1位
    {"number": 3, "claude_index": 40, "market_index": 50},
]}


def test_scan_or_combine(monkeypatch):
    """OR: A は強弱で勝負、B は市場との順位乖離で勝負。"""
    # A = 集中フィールド (sep 高) / snapshot なし
    fresh_a = {"odds": {1: 1.3, 2: 8.0, 3: 14.0, 4: 22.0, 5: 33.0},
               "names": {1: "AA", 2: "BB", 3: "CC", 4: "DD", 5: "EE"}}
    # B = 一様フィールド (sep 低) だが snapshot に順位乖離 (市場2位→Claude1位)
    fresh_b = {"odds": {n: 5.0 for n in range(1, 9)}, "names": {}}
    _setup(monkeypatch, fresh_a=fresh_a, fresh_b=fresh_b, snap_a=None, snap_b=_SNAP_DIVERGENCE)

    res = shobu.scan(fetch_odds=True, combine="or", sep_threshold=25.0,
                     edge_margin=3.0, edge_threshold=20.0, claude_eval=0, log=lambda *_: None)
    by_venue = {r["venue"]: r for r in res["races"]}
    assert by_venue["佐賀"]["recommended"] is True
    assert "sep" in by_venue["佐賀"]["matched"]
    assert by_venue["東京"]["recommended"] is True
    assert "claude" in by_venue["東京"]["matched"]
    assert by_venue["東京"]["claude"]["top_rank_gap"] == 1
    assert res["summary"]["recommended"] == 2


def test_scan_and_combine(monkeypatch):
    """AND: A は Claude 不在で不可、B は強弱不足で不可 → 推奨ゼロ。"""
    fresh_a = {"odds": {1: 1.3, 2: 8.0, 3: 14.0, 4: 22.0, 5: 33.0}, "names": {}}
    fresh_b = {"odds": {n: 5.0 for n in range(1, 9)}, "names": {}}
    _setup(monkeypatch, fresh_a=fresh_a, fresh_b=fresh_b, snap_a=None, snap_b=_SNAP_DIVERGENCE)

    res = shobu.scan(fetch_odds=True, combine="and", sep_threshold=25.0,
                     edge_margin=3.0, edge_threshold=20.0, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["recommended"] == 0


def test_scan_race_type_filter(monkeypatch):
    """race_type=nar は NAR だけ評価する。"""
    fresh_a = {"odds": {1: 1.3, 2: 8.0, 3: 14.0}, "names": {}}
    _setup(monkeypatch, fresh_a=fresh_a, fresh_b=None, snap_a=None, snap_b=None)
    res = shobu.scan(race_type="nar", fetch_odds=True, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["evaluated"] == 1
    assert res["races"][0]["race_type"] == "nar"


def test_select_claude_targets():
    now = 1000

    def race(rid, claude, sep, future):
        return {
            "race_id": rid, "netkeiba_race_id": "x", "venue": "V", "race_no": 1,
            "race_type": "nar", "start_at": 0,
            "close_at": now + 100 if future else now - 100,
            "claude": ({"available": True} if claude else None),
            "separation": ({"score": sep} if sep is not None else None),
        }

    results = [
        race("a", False, 50, True),    # 未スコア・発走前・sep 50
        race("b", False, 80, True),    # 未スコア・発走前・sep 80
        race("c", True, 90, True),     # スコア済 → 対象外
        race("d", False, 10, False),   # 締切済 → upcoming_only で対象外
    ]
    # 全件モード: 未スコア発走前を sep 降順で全部。
    t_all = shobu._select_claude_targets(results, claude_all=True, claude_eval=0,
                                         upcoming_only=True, now=now)
    assert [r["race_id"] for r in t_all] == ["b", "a"]
    # 上位 N モード。
    t_top = shobu._select_claude_targets(results, claude_all=False, claude_eval=1,
                                         upcoming_only=True, now=now)
    assert [r["race_id"] for r in t_top] == ["b"]
    # 締切済も含める。
    t_past = shobu._select_claude_targets(results, claude_all=True, claude_eval=0,
                                          upcoming_only=False, now=now)
    assert {r["race_id"] for r in t_past} == {"a", "b", "d"}
    # どちらも無効 → 空。
    assert shobu._select_claude_targets(results, claude_all=False, claude_eval=0,
                                        upcoming_only=True, now=now) == []


def test_scan_claude_all_generates_for_all(monkeypatch):
    """claude_all: Claude 指数なしの全レースに生成 → 全レースが Claude 乖離を持つ。"""
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: [
        {"race_id": "202632060101", "url": "u", "start_at": now + 3600,
         "venue": "佐賀", "race_no": 1, "source": "oddspark"},
        {"race_id": "202632060102", "url": "u", "start_at": now + 3600,
         "venue": "佐賀", "race_no": 2, "source": "oddspark"},
    ])
    snaps: dict = {}
    monkeypatch.setattr(shobu, "_load_snapshot", lambda i: snaps.get(i))
    monkeypatch.setattr(shobu, "_fetch_fresh_win",
                        lambda r, t: {"odds": {1: 5.0, 2: 5.1, 3: 4.9}, "names": {}})

    # 生成 stub: 対象 race の snapshot に順位乖離 (市場2位→Claude1位) を「インストール」。
    def fake_gen(targets, **kw):
        for t in targets:
            snaps[t["race_id"]] = {
                "index_compare": [
                    {"number": 1, "claude_index": 60, "market_index": 90},
                    {"number": 2, "claude_index": 85, "market_index": 70},
                    {"number": 3, "claude_index": 40, "market_index": 50},
                ],
            }
        return len(targets)

    monkeypatch.setattr(shobu, "_run_claude_eval", fake_gen)
    res = shobu.scan(claude_all=True, use_separation=False, use_claude_edge=True,
                     sep_threshold=101, edge_margin=3.0, edge_threshold=20.0,
                     fetch_odds=True, log=lambda *_: None)
    assert res["summary"]["with_claude"] == 2          # 2 レースとも生成された
    assert res["summary"]["recommended"] == 2          # 順位乖離 → 勝負 (claude のみ)
    assert all(r["claude"]["top_rank_gap"] == 1 for r in res["races"])


def test_scan_upcoming_only_excludes_past(monkeypatch):
    """発走前のみ: 締切済 (start_at が過去) は除外。"""
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: [
        {"race_id": "202632060101", "url": "u", "start_at": now - 3600,
         "venue": "佐賀", "race_no": 1, "source": "oddspark"},
    ])
    monkeypatch.setattr(shobu, "_load_snapshot", lambda i: None)
    monkeypatch.setattr(shobu, "_fetch_fresh_win", lambda r, t: None)
    res = shobu.scan(upcoming_only=True, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["evaluated"] == 0
    res2 = shobu.scan(upcoming_only=False, claude_eval=0, fetch_odds=False, log=lambda *_: None)
    assert res2["summary"]["evaluated"] == 1


# ------------------------------------------------ refresh (2分毎・推奨のみ) --

def test_refresh_recommended_rescore_and_history(tmp_path, monkeypatch):
    """refresh_recommended が推奨レースを最新オッズで再採点し、score_delta/履歴を付け、
    サイドカーに履歴を書き、推奨外レースは据え置く。Claude (snapshot) は呼ばない。"""
    import json
    monkeypatch.setattr(shobu, "SHOBU_DIR", tmp_path)
    date = "20260101"
    result = {
        "date": date,
        "generated_at": "2026-01-01T10:00:00+09:00",
        "options": {"use_separation": True, "use_claude_edge": True, "combine": "or",
                    "sep_threshold": 35.0, "edge_margin": 3.0, "edge_threshold": 25.0},
        "summary": {"total_discovered": 5},
        "races": [
            {"netkeiba_race_id": "202601010101", "race_id": "1-1-1", "venue": "X",
             "race_no": 1, "race_type": "nar", "start_at": 0, "close_at": 0,
             "n_runners": 3, "data_source": "snapshot", "has_snapshot": True,
             "recommended": True, "matched": ["claude"], "shobu_score": 58.0, "reasons": []},
            {"netkeiba_race_id": "202601010102", "race_id": "1-1-2", "venue": "X",
             "race_no": 2, "race_type": "nar", "start_at": 0, "close_at": 0,
             "n_runners": 3, "data_source": "snapshot", "has_snapshot": True,
             "recommended": False, "matched": [], "shobu_score": 5.0, "reasons": []},
        ],
    }
    (tmp_path / f"{date}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    snap = {"index_compare": [
        {"number": 1, "name": "A", "claude_index": 40.0, "market_index": 80.0},
        {"number": 2, "name": "B", "claude_index": 90.0, "market_index": 50.0},
        {"number": 3, "name": "C", "claude_index": 60.0, "market_index": 30.0},
    ], "n_runners": 3, "stage": "bet"}
    monkeypatch.setattr(shobu, "_load_snapshot", lambda rid: snap)
    # 最新オッズ: 強い1番人気 (馬2 1.2倍) → 強弱で推奨維持、ただし市場乖離は縮小 → 総合スコア低下。
    monkeypatch.setattr(shobu, "_fetch_fresh_win",
                        lambda rid, rtype: {"odds": {1: 8.0, 2: 1.2, 3: 12.0},
                                            "names": {1: "A", 2: "B", 3: "C"}})

    doc = shobu.refresh_recommended(date)
    assert doc is not None
    rec = next(r for r in doc["races"] if r["race_id"] == "1-1-1")
    assert rec["data_source"] == "fresh"               # 最新オッズで再採点された
    assert rec["recommended"] is True                   # 強弱で推奨維持
    assert rec["score_prev"] == 58.0                    # シード = スキャン時スコア
    assert rec["shobu_score"] < 58.0                    # 市場が Claude に追いつき低下
    assert rec["score_delta"] < 0
    assert len(rec["score_history"]) == 2               # シード + 今回
    other = next(r for r in doc["races"] if r["race_id"] == "1-1-2")
    assert "score_history" not in other                 # 推奨外は据え置き
    assert (tmp_path / f"{date}.scores.json").exists()  # サイドカー
    assert doc.get("refreshed_at")

    # 2回目: 履歴が伸び、score_prev は前回の新スコア。
    doc2 = shobu.refresh_recommended(date)
    rec2 = next(r for r in doc2["races"] if r["race_id"] == "1-1-1")
    assert len(rec2["score_history"]) == 3
    assert rec2["score_prev"] == rec["shobu_score"]


def test_refresh_recommended_missing_result(tmp_path, monkeypatch):
    """スキャン結果ファイルが無ければ None (404 元)。"""
    monkeypatch.setattr(shobu, "SHOBU_DIR", tmp_path)
    assert shobu.refresh_recommended("20260102") is None
