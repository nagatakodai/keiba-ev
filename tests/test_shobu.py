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


def test_claude_edge_from_index_compare():
    snap = {"index_compare": [
        {"number": 3, "name": "X", "claude_index": 80, "market_index": 55, "diff": 25, "support": 2, "alerts": []},
        {"number": 1, "name": "Y", "claude_index": 70, "market_index": 62, "diff": 8, "support": 1, "alerts": []},
        {"number": 5, "name": "Z", "claude_index": 40, "market_index": 50, "diff": -10},
    ]}
    e = shobu._claude_edge(snap, margin=8.0)
    assert e["edge_count"] == 2          # diff 25, 8 が margin 以上
    assert e["max_diff"] == 25
    assert e["score"] == 33              # 25 + 8 (cap 100)
    assert e["edge_horses"][0]["number"] == 3   # diff 降順


def test_claude_edge_fallback_from_index_dicts():
    """index_compare が無くても llm_win_index / market_win_index から diff を出せる。"""
    snap = {"llm_win_index": {"1": 75, "2": 40}, "market_win_index": {"1": 50, "2": 45}}
    e = shobu._claude_edge(snap, margin=8.0)
    assert e["edge_count"] == 1          # 1: 75-50=25 ≥8 / 2: 40-45=-5 <8
    assert e["max_diff"] == 25.0


def test_claude_edge_none_without_claude():
    assert shobu._claude_edge({"market_win_index": {"1": 50}}, margin=8.0) is None
    assert shobu._claude_edge({}, margin=8.0) is None


def test_race_type_and_internal_id():
    assert shobu._race_type("202605010102", "keibabook") == "jra"
    assert shobu._race_type("202632060101", "oddspark") == "nar"
    # source 不明でも netkeiba 場コードで判定。
    assert shobu._race_type("202605010102", "") == "jra"
    assert shobu._race_type("202632060101", "") == "nar"
    assert shobu._internal_id("202605010102") == "20260501-1-2"


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


def test_scan_or_combine(monkeypatch):
    """OR: A は強弱で勝負、B は Claude 乖離で勝負。"""
    # A = 集中フィールド (sep 高) / snapshot なし
    fresh_a = {"odds": {1: 1.3, 2: 8.0, 3: 14.0, 4: 22.0, 5: 33.0},
               "names": {1: "AA", 2: "BB", 3: "CC", 4: "DD", 5: "EE"}}
    # B = 一様フィールド (sep 低) だが snapshot に Claude 乖離 2 頭
    fresh_b = {"odds": {n: 5.0 for n in range(1, 9)}, "names": {}}
    snap_b = {"index_compare": [
        {"number": 1, "claude_index": 80, "market_index": 55, "diff": 25},
        {"number": 2, "claude_index": 70, "market_index": 58, "diff": 12},
    ]}
    _setup(monkeypatch, fresh_a=fresh_a, fresh_b=fresh_b, snap_a=None, snap_b=snap_b)

    res = shobu.scan(fetch_odds=True, combine="or", sep_threshold=25.0,
                     edge_margin=8.0, edge_min_count=2, claude_eval=0, log=lambda *_: None)
    by_venue = {r["venue"]: r for r in res["races"]}
    assert by_venue["佐賀"]["recommended"] is True
    assert "sep" in by_venue["佐賀"]["matched"]
    assert by_venue["東京"]["recommended"] is True
    assert "claude" in by_venue["東京"]["matched"]
    assert res["summary"]["recommended"] == 2


def test_scan_and_combine(monkeypatch):
    """AND: A は Claude 不在で不可、B は強弱不足で不可 → 推奨ゼロ。"""
    fresh_a = {"odds": {1: 1.3, 2: 8.0, 3: 14.0, 4: 22.0, 5: 33.0}, "names": {}}
    fresh_b = {"odds": {n: 5.0 for n in range(1, 9)}, "names": {}}
    snap_b = {"index_compare": [
        {"number": 1, "claude_index": 80, "market_index": 55, "diff": 25},
        {"number": 2, "claude_index": 70, "market_index": 58, "diff": 12},
    ]}
    _setup(monkeypatch, fresh_a=fresh_a, fresh_b=fresh_b, snap_a=None, snap_b=snap_b)

    res = shobu.scan(fetch_odds=True, combine="and", sep_threshold=25.0,
                     edge_margin=8.0, edge_min_count=2, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["recommended"] == 0


def test_scan_race_type_filter(monkeypatch):
    """race_type=nar は NAR だけ評価する。"""
    fresh_a = {"odds": {1: 1.3, 2: 8.0, 3: 14.0}, "names": {}}
    _setup(monkeypatch, fresh_a=fresh_a, fresh_b=None, snap_a=None, snap_b=None)
    res = shobu.scan(race_type="nar", fetch_odds=True, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["evaluated"] == 1
    assert res["races"][0]["race_type"] == "nar"


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
