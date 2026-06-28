"""src/shobu.py (今日の勝負レース スキャン) のロジック test。

ネットワークには出ない: discover_today_races / 最新オッズ fetch / snapshot 読込を monkeypatch する。
"""
from __future__ import annotations

import time

import src.shobu as shobu


# ---------------------------------------------------------------- pure 関数 --
# 判定は基準B (市場との順位乖離) 単独 (ユーザ指示 2026-06-28: 基準A=強弱は廃止)。

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

    res_all = shobu.scan(race_type="all", claude_eval=0, log=lambda *_: None)
    assert res_all["summary"]["by_type"] == {"jra": 0, "nar": 1, "banei": 1}

    res_nar = shobu.scan(race_type="nar", claude_eval=0, log=lambda *_: None)
    assert [r["race_type"] for r in res_nar["races"]] == ["nar"]

    res_banei = shobu.scan(race_type="banei", claude_eval=0, log=lambda *_: None)
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


def _setup(monkeypatch, *, snap_a, snap_b):
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: _fake_discovery(now))

    nar_internal = shobu._internal_id("202632060101")
    jra_internal = shobu._internal_id("202605010111")

    def fake_snap(internal):
        return {nar_internal: snap_a, jra_internal: snap_b}.get(internal)

    monkeypatch.setattr(shobu, "_load_snapshot", fake_snap)
    return nar_internal, jra_internal


# snapshot: 市場2番人気を Claude が本命視 (順位乖離) → 基準B 合格。
_SNAP_DIVERGENCE = {"index_compare": [
    {"number": 1, "claude_index": 60, "market_index": 90},   # 市場1位 Claude2位
    {"number": 2, "claude_index": 85, "market_index": 70},   # 市場2位 Claude1位
    {"number": 3, "claude_index": 40, "market_index": 50},
]}
# snapshot: 市場と Claude の順位が一致 (乖離なし) → 基準B 不合格。
_SNAP_ALIGNED = {"index_compare": [
    {"number": 1, "claude_index": 90, "market_index": 90},
    {"number": 2, "claude_index": 70, "market_index": 70},
    {"number": 3, "claude_index": 50, "market_index": 50},
]}


def test_scan_recommends_on_divergence(monkeypatch):
    """基準B 単独: 順位乖離のある snapshot だけ推奨、一致 snapshot は非推奨。"""
    _setup(monkeypatch, snap_a=_SNAP_ALIGNED, snap_b=_SNAP_DIVERGENCE)
    res = shobu.scan(edge_margin=3.0, edge_threshold=20.0, claude_eval=0, log=lambda *_: None)
    by_venue = {r["venue"]: r for r in res["races"]}
    assert by_venue["東京"]["recommended"] is True          # 順位乖離あり
    assert "claude" in by_venue["東京"]["matched"]
    assert by_venue["東京"]["claude"]["top_rank_gap"] == 1
    assert by_venue["佐賀"]["recommended"] is False         # 乖離なし
    assert res["summary"]["recommended"] == 1


def test_scan_no_snapshot_not_recommended(monkeypatch):
    """snapshot (Claude 指数) が無いレースは基準B 評価不能 = 非推奨だが evaluated には数える。"""
    _setup(monkeypatch, snap_a=None, snap_b=None)
    res = shobu.scan(edge_margin=3.0, edge_threshold=20.0, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["evaluated"] == 2
    assert res["summary"]["recommended"] == 0
    assert all(not r["recommended"] for r in res["races"])


def test_scan_race_type_filter(monkeypatch):
    """race_type=nar は NAR だけ評価する。"""
    _setup(monkeypatch, snap_a=None, snap_b=None)
    res = shobu.scan(race_type="nar", claude_eval=0, log=lambda *_: None)
    assert res["summary"]["evaluated"] == 1
    assert res["races"][0]["race_type"] == "nar"


def test_scan_carries_past_recommended(tmp_path, monkeypatch):
    """再スキャンで発走済になった推奨レースを file に維持する (ユーザ指摘 2026-06-28)。

    upcoming_only=True で discovery から落ちても、前回 file の発走済レースを carry-forward して
    再採点 → file に残す。これでダッシュボード仮想収支が発走後も勝負レースを数え続けられる。
    """
    import json
    now = int(time.time())
    nar = shobu._internal_id("202632060101")   # A
    jra = shobu._internal_id("202605010111")   # B
    monkeypatch.setattr(shobu, "_load_snapshot", lambda i: _SNAP_DIVERGENCE)

    # 1回目: A, B とも発走前 → 両方 file に推奨で入る。
    calls = {"n": 0}

    def disc(_d):
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                {"race_id": "202632060101", "url": "u", "start_at": now + 3600,
                 "venue": "佐賀", "race_no": 1, "source": "oddspark"},
                {"race_id": "202605010111", "url": "u", "start_at": now + 7200,
                 "venue": "東京", "race_no": 11, "source": "keibabook"},
            ]
        # 2回目: A は発走済で discovery から消えた (B のみ返る)。
        return [
            {"race_id": "202605010111", "url": "u", "start_at": now + 7200,
             "venue": "東京", "race_no": 11, "source": "keibabook"},
        ]

    monkeypatch.setattr("src.auto_watch.discover_today_races", disc)
    out = tmp_path / "20260628.json"
    r1 = shobu.scan(edge_threshold=20.0, claude_eval=0, out=out, log=lambda *_: None)
    assert {r["race_id"] for r in r1["races"]} == {nar, jra}
    assert r1["summary"]["recommended"] == 2

    # 2回目: A は discovery に無いが、前回 file から carry されて残る。
    r2 = shobu.scan(edge_threshold=20.0, claude_eval=0, out=out, log=lambda *_: None)
    ids = {r["race_id"] for r in r2["races"]}
    assert nar in ids and jra in ids                    # A が消えずに残る
    a = next(r for r in r2["races"] if r["race_id"] == nar)
    assert a["recommended"] is True                      # 再採点され推奨のまま
    assert r2["summary"]["recommended"] == 2             # dashboard が数える母集団は維持


def test_select_claude_targets():
    now = 1000

    def race(rid, claude, start_at, future):
        return {
            "race_id": rid, "netkeiba_race_id": "x", "venue": "V", "race_no": 1,
            "race_type": "nar", "start_at": start_at,
            "close_at": now + 100 if future else now - 100,
            "claude": ({"available": True} if claude else None),
        }

    results = [
        race("a", False, 2000, True),   # 未スコア・発走前・発走 2000 (遅い)
        race("b", False, 1500, True),   # 未スコア・発走前・発走 1500 (近い)
        race("c", True, 1400, True),    # スコア済 → 対象外
        race("d", False, 900, False),   # 締切済 → upcoming_only で対象外
    ]
    # 全件モード: 未スコア発走前を発走が近い順で全部。
    t_all = shobu._select_claude_targets(results, claude_all=True, claude_eval=0,
                                         upcoming_only=True, now=now)
    assert [r["race_id"] for r in t_all] == ["b", "a"]   # 発走時刻 1500 < 2000
    # 上位 N モード (発走が近い順)。
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


class _FakePopen:
    """_run_claude_eval._one が使う Popen 互換ダミー。stdout 行を yield し env/cmd を capture。"""

    def __init__(self, cmd, *, env=None, capture=None, returncode=0, lines=None):
        self.returncode = returncode
        self.stdout = iter(list(lines or []))
        if capture is not None:
            capture["env"] = env
            capture["cmd"] = cmd

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _fake_popen_factory(capture, *, returncode=0, lines=None):
    def _factory(cmd, **kw):
        return _FakePopen(cmd, env=kw.get("env"), capture=capture,
                          returncode=returncode, lines=lines)
    return _factory


def test_run_claude_eval_sets_search_env(monkeypatch):
    """_run_claude_eval が score subprocess へ 10クエリ/頭・並列飽和・timeout余裕の env を渡す。

    頭数×10 クエリ (KEIBA_SCORE_QUERIES_PER_HORSE=10 を全シャード被覆) + across-race=4 に対し
    シャード/レース = 20//4 = 5 (per_shard=3) で claude 同時実行を飽和 + 外側 kill 手前の
    KEIBA_SCORE_TIMEOUT。
    """
    for k in ("KEIBA_SCORE_HORSES_PER_SHARD", "KEIBA_SCORE_MAX_SHARDS", "KEIBA_SCORE_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    captured: dict = {}
    monkeypatch.setattr(shobu.subprocess, "Popen", _fake_popen_factory(captured))
    targets = [{"netkeiba_race_id": "202632060101", "race_type": "nar",
                "start_at": 1000, "venue": "佐賀", "race_no": 1}]
    done = shobu._run_claude_eval(
        targets, timeout=900, parallel=4, log=lambda m: None,
        score_parallel=True, score_queries_per_horse=10, llm_max_concurrent=20)
    assert done == 1
    env = captured["env"]
    assert env["KEIBA_SCORE_PARALLEL"] == "1"
    assert env["KEIBA_SCORE_QUERIES_PER_HORSE"] == "10"
    assert env["KEIBA_LLM_MAX_CONCURRENT"] == "20"
    assert env["KEIBA_SCORE_HORSES_PER_SHARD"] == "3"
    assert env["KEIBA_SCORE_MAX_SHARDS"] == "5"          # 20 // 4
    assert int(env["KEIBA_SCORE_TIMEOUT"]) == 810        # 900 - 90 (外側 kill の手前)
    # score subprocess コマンドも phase=score。
    assert "--phase=score" in captured["cmd"]


def test_run_claude_eval_no_parallel_omits_shard_env(monkeypatch):
    """score_parallel=False ではシャード env を入れず KEIBA_SCORE_PARALLEL を打ち消す。"""
    for k in ("KEIBA_SCORE_HORSES_PER_SHARD", "KEIBA_SCORE_MAX_SHARDS", "KEIBA_SCORE_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    captured: dict = {}
    monkeypatch.setattr(shobu.subprocess, "Popen", _fake_popen_factory(captured))
    targets = [{"netkeiba_race_id": "x", "race_type": "nar",
                "start_at": 0, "venue": "V", "race_no": 1}]
    shobu._run_claude_eval(targets, timeout=900, parallel=4, log=lambda m: None,
                           score_parallel=False, score_queries_per_horse=10,
                           llm_max_concurrent=20)
    env = captured["env"]
    assert env["KEIBA_SCORE_PARALLEL"] == ""
    assert env["KEIBA_SCORE_QUERIES_PER_HORSE"] == "10"  # クエリ数は並列に依らず伝える
    assert "KEIBA_SCORE_MAX_SHARDS" not in env
    assert "KEIBA_SCORE_HORSES_PER_SHARD" not in env


def test_run_claude_eval_inner_timeout_below_outer(monkeypatch):
    """KEIBA_SCORE_TIMEOUT は常に外側 subprocess timeout より小さい (反転防止)。

    外側が先に発火すると score 結果が丸ごと失われるため、小さい timeout でも内側<外側を保証。
    """
    for k in ("KEIBA_SCORE_HORSES_PER_SHARD", "KEIBA_SCORE_MAX_SHARDS", "KEIBA_SCORE_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    captured: dict = {}
    monkeypatch.setattr(shobu.subprocess, "Popen", _fake_popen_factory(captured))
    targets = [{"netkeiba_race_id": "x", "race_type": "nar",
                "start_at": 0, "venue": "V", "race_no": 1}]
    for outer in (900, 300, 210, 150, 130):
        shobu._run_claude_eval(targets, timeout=outer, parallel=4, log=lambda m: None,
                               score_parallel=True, score_queries_per_horse=10,
                               llm_max_concurrent=20)
        inner = int(captured["env"]["KEIBA_SCORE_TIMEOUT"])
        assert 0 < inner < outer, f"timeout={outer}: 内側 {inner} が外側を超えた (反転)"
    # 通常運用 (900) は 810s。
    shobu._run_claude_eval(targets, timeout=900, parallel=4, log=lambda m: None,
                           score_parallel=True, score_queries_per_horse=10, llm_max_concurrent=20)
    assert int(captured["env"]["KEIBA_SCORE_TIMEOUT"]) == 810


def test_cli_clamps_tiny_timeout(monkeypatch, capsys):
    """CLI --claude-eval-timeout が小さすぎる値を 210s にクランプする。"""
    seen: dict = {}
    monkeypatch.setattr(shobu, "scan",
                        lambda **k: seen.update(k) or {"races": [],
                            "summary": {"recommended": 0, "evaluated": 0,
                                        "with_snapshot": 0, "with_claude": 0}})
    shobu.main(["--claude-eval-timeout", "10"])
    assert seen["claude_eval_timeout"] == 210


def test_run_claude_eval_shards_scale_with_concurrency(monkeypatch):
    """シャード/レース = llm_max_concurrent // across-race (飽和度の自動スケール)。"""
    for k in ("KEIBA_SCORE_HORSES_PER_SHARD", "KEIBA_SCORE_MAX_SHARDS", "KEIBA_SCORE_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    captured: dict = {}
    monkeypatch.setattr(shobu.subprocess, "Popen", _fake_popen_factory(captured))
    targets = [{"netkeiba_race_id": "x", "race_type": "jra",
                "start_at": 0, "venue": "V", "race_no": 1}]
    shobu._run_claude_eval(targets, timeout=900, parallel=2, log=lambda m: None,
                           score_parallel=True, score_queries_per_horse=10,
                           llm_max_concurrent=20)
    assert captured["env"]["KEIBA_SCORE_MAX_SHARDS"] == "10"   # 20 // 2


def test_run_claude_eval_forwards_query_lines(monkeypatch):
    """score subprocess の検索クエリ行 (🔍) を scan ログへレース名付きで転送する。

    ユーザ指示「クエリもログに出す」。⚙ (非検索ツール) や冗長な LLM 出力は転送しない。
    """
    lines = [
        "Claude 考察の冗長な本文 ...\n",
        "  🔍 tavily/tavily_search: サンプル馬 前走 不利 OR 出遅れ\n",
        "  ⚙ Read\n",
    ]
    captured: dict = {}
    monkeypatch.setattr(shobu.subprocess, "Popen",
                        _fake_popen_factory(captured, lines=lines))
    logs: list[str] = []
    targets = [{"netkeiba_race_id": "x", "race_type": "nar",
                "start_at": 0, "venue": "佐賀", "race_no": 11}]
    done = shobu._run_claude_eval(targets, timeout=900, parallel=1, log=logs.append,
                                  score_parallel=False, score_queries_per_horse=10,
                                  llm_max_concurrent=20)
    assert done == 1
    qlogs = [m for m in logs if "🔍" in m]
    assert qlogs == ["[query] 佐賀11R 🔍 tavily/tavily_search: サンプル馬 前走 不利 OR 出遅れ"]
    assert not any("冗長" in m for m in logs)   # 本文は転送しない
    assert not any("⚙" in m for m in logs)       # 非検索ツールは転送しない


def test_run_claude_eval_real_subprocess_timeout(monkeypatch):
    """実プロセスで timeout→killpg→stdout EOF→(done=0, note=timeout) と 🔍 ライブ転送を検証。

    _FakePopen では threading.Timer/kill 経路が no-op なので、ここだけ本物の子プロセスを使う。
    子は 🔍 を 1 行出してから 30s sleep。timeout=1 で kill されることを確認 (sleep を待たない)。
    """
    import sys
    child = ("import sys, time\n"
             "sys.stdout.write('  \U0001f50d t: REALQ\\n'); sys.stdout.flush()\n"
             "time.sleep(30)\n")
    monkeypatch.setattr(shobu, "_score_stage_cmd",
                        lambda rid, rtype, start_at: [sys.executable, "-c", child])
    logs: list[str] = []
    targets = [{"netkeiba_race_id": "x", "race_type": "nar",
                "start_at": 0, "venue": "佐賀", "race_no": 11}]
    t0 = time.time()
    done = shobu._run_claude_eval(targets, timeout=1, parallel=1, log=logs.append,
                                  score_parallel=False, score_queries_per_horse=10,
                                  llm_max_concurrent=20)
    elapsed = time.time() - t0
    assert done == 0                                       # timeout は生成失敗
    assert any("timeout" in m for m in logs)               # 完了行に timeout note
    assert any("🔍 t: REALQ" in m for m in logs)           # kill 前に query が live 転送された
    assert elapsed < 20, f"30s sleep を待たず kill されるはず (elapsed={elapsed:.1f}s)"


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
    res = shobu.scan(claude_all=True, edge_margin=3.0, edge_threshold=20.0,
                     log=lambda *_: None)
    assert res["summary"]["with_claude"] == 2          # 2 レースとも生成された
    assert res["summary"]["recommended"] == 2          # 順位乖離 → 勝負 (claude のみ)
    assert all(r["claude"]["top_rank_gap"] == 1 for r in res["races"])


def test_scan_provisional_then_final_emits(tmp_path, monkeypatch):
    """out 指定時: 生成前に暫定 (generating=True/gen_done=0) → 生成完了ごとに live 更新 →
    最終 (generating=False/gen_done==gen_total) を書き出し、基準B が確定する。"""
    import json
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

    # 各 _atomic_write_json の (generating, gen_total, gen_done, with_claude) を記録。
    emits: list[tuple] = []
    real_write = shobu._atomic_write_json

    def rec_write(path, doc):
        emits.append((doc.get("generating"), doc.get("gen_total"), doc.get("gen_done"),
                      doc["summary"]["with_claude"]))
        real_write(path, doc)

    monkeypatch.setattr(shobu, "_atomic_write_json", rec_write)

    # 生成 stub: 各 target の snapshot に順位乖離を install して on_progress を呼ぶ (live)。
    def fake_gen(targets, *, on_progress=None, **kw):
        for i, t in enumerate(targets, 1):
            snaps[t["race_id"]] = {"index_compare": [
                {"number": 1, "claude_index": 60, "market_index": 90},
                {"number": 2, "claude_index": 85, "market_index": 70},
                {"number": 3, "claude_index": 40, "market_index": 50},
            ]}
            if on_progress:
                on_progress(t, i, len(targets))
        return len(targets)

    monkeypatch.setattr(shobu, "_run_claude_eval", fake_gen)

    out = tmp_path / "20260101.json"
    res = shobu.scan(claude_all=True, edge_margin=3.0, edge_threshold=20.0,
                     out=out, log=lambda *_: None)

    # 暫定 (生成前) = 最初の書き出し: generating=True / gen_done=0 / Claude まだ 0。
    assert emits[0][0] is True and emits[0][2] == 0 and emits[0][3] == 0
    # generating=True の各 emit で gen_done が 0→1→2 と進み Claude が付いていく。
    assert [e[2] for e in emits if e[0] is True] == [0, 1, 2]
    # 最終 = generating=False / gen_done==gen_total==2 / Claude 2。
    assert emits[-1][0] is False and emits[-1][1] == 2 and emits[-1][2] == 2 and emits[-1][3] == 2
    # 返り値・ファイルとも最終状態 (基準B 確定)。
    assert res["generating"] is False and res["summary"]["with_claude"] == 2
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["generating"] is False and written["gen_total"] == 2 and written["gen_done"] == 2


def test_scan_no_generation_single_final_emit(tmp_path, monkeypatch):
    """生成対象が無い (claude_eval=0) なら暫定を出さず最終のみ (generating=False)。"""
    import json
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: [
        {"race_id": "202632060101", "url": "u", "start_at": now + 3600,
         "venue": "佐賀", "race_no": 1, "source": "oddspark"},
    ])
    monkeypatch.setattr(shobu, "_load_snapshot", lambda i: None)
    out = tmp_path / "20260101.json"
    res = shobu.scan(claude_all=False, claude_eval=0, out=out, log=lambda *_: None)
    assert res["generating"] is False and res["gen_total"] == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["generating"] is False and written["gen_total"] == 0


def test_scan_upcoming_only_excludes_past(monkeypatch):
    """発走前のみ: 締切済 (start_at が過去) は除外。"""
    now = int(time.time())
    monkeypatch.setattr("src.auto_watch.discover_today_races", lambda d: [
        {"race_id": "202632060101", "url": "u", "start_at": now - 3600,
         "venue": "佐賀", "race_no": 1, "source": "oddspark"},
    ])
    monkeypatch.setattr(shobu, "_load_snapshot", lambda i: None)
    res = shobu.scan(upcoming_only=True, claude_eval=0, log=lambda *_: None)
    assert res["summary"]["evaluated"] == 0
    res2 = shobu.scan(upcoming_only=False, claude_eval=0, log=lambda *_: None)
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
        "options": {"edge_margin": 3.0, "edge_threshold": 10.0},
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
    # 最新オッズ: 馬2 が強い1番人気 (1.2倍) になり市場が Claude 本命に追いつく → 市場乖離が縮小し
    # 勝負スコア (基準B) 低下。edge_threshold=10 なので低下後も推奨は維持される。
    monkeypatch.setattr(shobu, "_fetch_fresh_win",
                        lambda rid, rtype: {"odds": {1: 8.0, 2: 1.2, 3: 12.0},
                                            "names": {1: "A", 2: "B", 3: "C"}})

    doc = shobu.refresh_recommended(date)
    assert doc is not None
    rec = next(r for r in doc["races"] if r["race_id"] == "1-1-1")
    assert rec["data_source"] == "fresh"               # 最新オッズで再採点された
    assert rec["recommended"] is True                   # 市場乖離が残り推奨維持 (threshold=10)
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
