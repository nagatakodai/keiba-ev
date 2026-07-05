"""固定クエリ Tavily プリフェッチ (ARCH-B, 2026-07-05) のユニットテスト。

すべて OFFLINE: Tavily API は叩かず `research_prefetch._search` を差し替える。
claude も spawn しない (dispatcher テストは score_horses_prefetch / score_horses_stream を
モックして経路とフォールバックだけ検証)。
"""
from __future__ import annotations

import json

import pytest

from src import llm
from src import research_prefetch as rp
from src.models import Horse, Race, RaceData, Weather


def _mk_race(n_horses: int = 6, jockeys: list[str] | None = None) -> RaceData:
    race = Race(
        cup_id="X", schedule_index=1, race_number=11, venue_id=5,
        venue_name="東京", race_class="3勝クラス", distance=1600, surface="芝",
        weather=Weather(code=100, track_condition="良"),
        start_at=1783233900,
    )
    race.horses = [
        Horse(number=i, name=f"ホース{i}", sex_age="牡4",
              jockey_name=(jockeys[i - 1] if jockeys else f"騎手{i}"),
              body_weight=480, body_weight_diff=0, win_odds=float(i))
        for i in range(1, n_horses + 1)
    ]
    return RaceData(race=race, trifecta=[])


# ---------- build_queries ----------

def test_build_queries_templates_and_jockey_dedup():
    """レース級 3 本 (馬場/取消/予想) + ユニーク騎手 / 各馬 2 本 (パドック・近況)。"""
    rd = _mk_race(4, jockeys=["武豊", "武豊", "川田", "川田"])   # 2 ユニーク騎手
    race_level, per_horse = rp.build_queries(rd)
    kinds = [q["kind"] for q in race_level]
    assert kinds[:3] == ["track", "scratch", "preview"]
    assert kinds.count("jockey") == 2                      # 同騎手は 1 回だけ
    track_q = race_level[0]["query"]
    assert "東京" in track_q and "馬場状態" in track_q and "芝" in track_q
    assert "11R" in race_level[1]["query"]                 # 取消は R 指定
    assert set(per_horse) == {1, 2, 3, 4}
    pq, rq = per_horse[1][0], per_horse[1][1]
    assert pq["kind"] == "paddock" and '"ホース1"' in pq["query"] and "パドック" in pq["query"]
    assert rq["kind"] == "recent" and "3勝クラス" in rq["query"]


# ---------- fetch_dossier ----------

def _fake_search_factory(calls: list[str]):
    def _fake(query: str, *, max_results: int, depth: str, timeout: int = 15):
        calls.append(query)
        return [{"title": f"T:{query[:20]}", "url": "https://example.com/a/b",
                 "content": f"C:{query[:30]}"}]
    return _fake


def test_fetch_dossier_offline_builds_and_caches(monkeypatch, tmp_path):
    rd = _mk_race(3)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(rp, "CACHE_DIR", tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(rp, "_search", _fake_search_factory(calls))

    d = rp.fetch_dossier(rd, "X-1-11")
    assert d is not None
    assert d["race_id"] == "X-1-11"
    assert len(d["race_level"]) >= 3                      # 馬場/取消/予想 + 騎手
    assert set(d["horses"]) == {"1", "2", "3"}
    assert len(d["horses"]["1"]) == 2                     # paddock + recent
    assert len(d["queries"]) == len(calls)                # 発行クエリを全記録
    assert (tmp_path / "X-1-11.json").exists()            # キャッシュ書き込み

    # TTL 内の再呼び出しはキャッシュを返し _search を叩かない。
    n = len(calls)
    d2 = rp.fetch_dossier(rd, "X-1-11")
    assert d2 is not None and len(calls) == n


def test_fetch_dossier_none_when_no_key_or_all_fail(monkeypatch, tmp_path):
    rd = _mk_race(3)
    monkeypatch.setattr(rp, "CACHE_DIR", tmp_path)
    # API キー無し → None (agentic フォールバック)
    monkeypatch.setenv("TAVILY_API_KEY", "")
    assert rp.fetch_dossier(rd, "X-1-11") is None
    # 全クエリ失敗 (レート制限/障害) → None
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def _boom(query, *, max_results, depth, timeout=15):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(rp, "_search", _boom)
    assert rp.fetch_dossier(rd, "X-1-12") is None


def test_fetch_dossier_partial_failure_tolerated(monkeypatch, tmp_path):
    """一部クエリの失敗は呑み、取れた分だけの dossier を返す。"""
    rd = _mk_race(3)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(rp, "CACHE_DIR", tmp_path)
    state = {"n": 0}

    def _flaky(query, *, max_results, depth, timeout=15):
        state["n"] += 1
        if state["n"] % 2 == 0:
            raise RuntimeError("flaky")
        return [{"title": "t", "url": "https://x.jp/p", "content": "c"}]

    monkeypatch.setattr(rp, "_search", _flaky)
    d = rp.fetch_dossier(rd, "X-1-13")
    assert d is not None
    total_rows = len(d["race_level"]) + sum(len(v) for v in d["horses"].values())
    assert 0 < total_rows < len(d["queries"])             # 部分成功


# ---------- prompt ----------

def test_dossier_prompt_contains_material_and_rules(monkeypatch, tmp_path):
    rd = _mk_race(3)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(rp, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(rp, "_search", _fake_search_factory([]))
    d = rp.fetch_dossier(rd, "X-1-14")
    prompt = llm.build_horse_score_from_dossier_prompt(rd, d)
    assert "収集済み検索資料" in prompt
    assert "馬番 1 ホース1" in prompt
    assert "## 指数の付け方" in prompt                      # 採点ルール+schema は流用
    assert "検索 MCP の運用ルール" not in prompt             # 検索ルール節は差し替え済み
    assert "関連判定" in prompt                             # 生スニペットの読解指示


# ---------- dispatcher (score_horses) ----------

def test_dispatcher_prefetch_used_when_result(monkeypatch):
    rd = _mk_race(3)
    monkeypatch.setenv("KEIBA_SCORE_RESEARCH", "prefetch")

    def _fake_prefetch(rd_, **kw):
        yield ("tool_use", {"name": "tavily_prefetch", "input": {"query": "q1"}})
        yield ("result", json.dumps({"scores": {"1": 80}}))

    def _boom_stream(rd_, **kw):
        raise AssertionError("prefetch 成功時は stream を呼ばない")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "score_horses_prefetch", _fake_prefetch)
    monkeypatch.setattr(llm, "score_horses_stream", _boom_stream)
    evs = list(llm.score_horses(rd))
    assert ("result", json.dumps({"scores": {"1": 80}})) in evs
    assert llm.LAST_RESEARCH_MODE == "prefetch"


def test_dispatcher_prefetch_falls_back_to_agentic(monkeypatch):
    rd = _mk_race(3)
    monkeypatch.setenv("KEIBA_SCORE_RESEARCH", "prefetch")
    monkeypatch.delenv("KEIBA_SCORE_PARALLEL", raising=False)

    def _fail_prefetch(rd_, **kw):
        yield ("error", "prefetch: dossier 取得不可")

    def _ok_stream(rd_, **kw):
        yield ("result", "AGENTIC_RESULT")

    monkeypatch.setattr(llm, "score_horses_prefetch", _fail_prefetch)
    monkeypatch.setattr(llm, "score_horses_stream", _ok_stream)
    evs = list(llm.score_horses(rd))
    assert ("result", "AGENTIC_RESULT") in evs
    assert llm.LAST_RESEARCH_MODE == "agentic"             # 実績値は agentic とラベル


def test_dispatcher_default_unchanged(monkeypatch):
    """env 未設定 (既定) は prefetch を呼ばず従来どおり (挙動不変)。"""
    rd = _mk_race(3)
    monkeypatch.delenv("KEIBA_SCORE_RESEARCH", raising=False)
    monkeypatch.delenv("KEIBA_SCORE_PARALLEL", raising=False)

    def _boom_prefetch(rd_, **kw):
        raise AssertionError("既定では prefetch を呼ばない")
        yield  # pragma: no cover

    def _ok_stream(rd_, **kw):
        yield ("result", "OK")

    monkeypatch.setattr(llm, "score_horses_prefetch", _boom_prefetch)
    monkeypatch.setattr(llm, "score_horses_stream", _ok_stream)
    assert ("result", "OK") in list(llm.score_horses(rd))
