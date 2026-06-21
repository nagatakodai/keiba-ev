"""並列 score (Claude 指数) パイプラインのユニットテスト。

すべて OFFLINE: claude / Tavily は spawn せず、llm._spawn_claude を差し替えて
stream-json をモックする。実弾 score 段の挙動 (相対採点が単一段に閉じる / 失敗時は
単一セッションにフォールバック / 既定 OFF で挙動不変) を検証する。
"""
from __future__ import annotations

import io
import json

import pytest

from src import llm
from src.models import Horse, Race, RaceData, Weather


def _mk_race(n_horses: int = 8) -> RaceData:
    race = Race(
        cup_id="X", schedule_index=1, race_number=11, venue_id=5,
        venue_name="東京", race_class="3勝クラス", distance=1600, surface="芝",
        weather=Weather(code=100, track_condition="良"),
    )
    race.horses = [
        Horse(number=i, name=f"H{i}", sex_age="牡4", jockey_name="J",
              body_weight=480, body_weight_diff=0, win_odds=float(i))
        for i in range(1, n_horses + 1)
    ]
    return RaceData(race=race, trifecta=[])


# ---------- pure helpers ----------

def test_shard_numbers_covers_all_and_caps():
    assert llm._shard_numbers([1, 2, 3, 4, 5, 6, 7, 8], 4, 4) == [[1, 2, 3, 4], [5, 6, 7, 8]]
    shards = llm._shard_numbers(list(range(1, 19)), 4, 4)  # 18 頭, cap 4
    assert len(shards) == 4
    flat = [x for s in shards for x in s]
    assert sorted(flat) == list(range(1, 19))  # 全馬被覆
    assert llm._shard_numbers([], 4, 4) == []


def test_merge_research_union_and_robust():
    good1 = '```json\n{"facts": {"1": {"alerts": ["前走不利", ""], "support": "2", "digest": "x"}}}\n```'
    good2 = '```json\n{"facts": {"3": {"alerts": "取消", "support": -5, "digest": "y" }}}\n```'
    broken = '{"facts": "oops"}'          # facts が dict でない
    junk = "no json here"
    long_digest = '```json\n{"facts": {"5": {"digest": "' + "z" * 500 + '"}}}\n```'
    out = llm._merge_research([good1, good2, broken, junk, long_digest])
    assert set(out) == {1, 3, 5}
    assert out[1]["alerts"] == ["前走不利"]      # 空文字は除去
    assert out[1]["support"] == 2                 # 文字列 -> int
    assert out[3]["alerts"] == ["取消"]           # 単一文字列 -> list
    assert out[3]["support"] == 0                 # 負値は 0 clamp
    assert len(out[5]["digest"]) == 240           # 240 字に truncate
    assert llm._merge_research([]) == {}


def test_research_prompt_shape():
    rd = _mk_race(8)
    p = llm.build_horse_research_prompt(rd, [1, 3, 5], queries_per_horse=6)
    assert "リサーチ専用" in p
    assert "馬番 [1, 3, 5]" in p
    assert "~18 クエリ" in p           # 3 頭 × 6
    assert '"facts"' in p              # facts schema
    assert '"scores": {"7"' not in p   # 0-100 採点 schema は出さない (次段が行う)


def test_score_from_research_prompt_shape():
    rd = _mk_race(8)
    research = {1: {"alerts": ["取消"], "support": 2, "digest": "DIGEST_MARKER"}}
    p = llm.build_horse_score_from_research_prompt(rd, research)
    assert "収集済みリサーチ" in p
    assert "DIGEST_MARKER" in p              # digest が描画される
    assert "取消" in p
    assert "## 指数の付け方" in p            # 採点ルールは保持
    assert '"scores"' in p                   # 最終 JSON schema は保持
    assert "## 検索 MCP の運用ルール" not in p  # 検索ルールは差し替え済


# ---------- ClaudeGate ----------

def test_claude_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_CLAUDE_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setenv("KEIBA_LLM_MAX_CONCURRENT", "2")
    g1 = llm._ClaudeGate(3)
    assert g1.acquire() == 2          # cap=2 なので 3 要求でも 2 まで
    g2 = llm._ClaudeGate(2)
    assert g2.acquire() == 0          # 満杯
    g1.release()
    assert g2.acquire() == 2          # 解放後に取れる
    g2.release()


# ---------- mocked subprocess ----------

class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def _result_line(result_text: str) -> str:
    return json.dumps({"type": "result", "result": result_text}) + "\n"


_PHASE_B_SCORES = (
    "```json\n"
    '{"scores": {"1":95,"2":80,"3":70,"4":60,"5":50,"6":40,"7":30,"8":20},'
    ' "support": {}, "alerts": {}, "notes": {}, "summary": "ok", "confidence": "mid"}\n'
    "```"
)


def _fake_spawn_factory(research_result: str):
    def fake_spawn(cmd):
        prompt = cmd[2]
        if "リサーチ専用" in prompt:
            lines = [_result_line(research_result)]
        else:  # Phase B scoring
            lines = [_result_line(_PHASE_B_SCORES)]
        return _FakeProc(lines), io.StringIO()
    return fake_spawn


def test_parallel_success_full_field(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_CLAUDE_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setenv("KEIBA_SCORE_PARALLEL", "1")
    monkeypatch.setattr(llm, "is_available", lambda: True)
    research = '```json\n{"facts": {"1": {"alerts": ["前走不利"], "support": 2, "digest": "d"}}}\n```'
    monkeypatch.setattr(llm, "_spawn_claude", _fake_spawn_factory(research))

    rd = _mk_race(8)
    events = list(llm.score_horses(rd))
    results = [p for (t, p) in events if t == "result" and p]
    assert len(results) == 1                       # 採点 JSON は 1 回だけ (Phase B)
    parsed = llm.parse_horse_scores(results[0])
    assert set(parsed["scores"]) == set(range(1, 9))   # 全馬を被覆した単一 0-100 ベクトル
    assert parsed["scores"][1] == 95.0


def test_parallel_fallback_to_single_session(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_CLAUDE_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setenv("KEIBA_SCORE_PARALLEL", "1")
    monkeypatch.setattr(llm, "is_available", lambda: True)
    # research が facts を返さない → merge 空 → parallel は result 無しで終わる
    monkeypatch.setattr(llm, "_spawn_claude", _fake_spawn_factory('```json\n{"facts": {}}\n```'))

    def fake_single(rd, **kw):
        yield ("result", "SINGLE_SESSION_FALLBACK")
    monkeypatch.setattr(llm, "score_horses_stream", fake_single)

    rd = _mk_race(8)
    events = list(llm.score_horses(rd))
    assert ("result", "SINGLE_SESSION_FALLBACK") in events   # 単一セッションへ degrade


def test_dispatcher_default_off_uses_single_session(monkeypatch):
    monkeypatch.delenv("KEIBA_SCORE_PARALLEL", raising=False)

    called = {"n": 0}

    def fake_single(rd, **kw):
        called["n"] += 1
        yield ("result", "DEFAULT_SINGLE")
    monkeypatch.setattr(llm, "score_horses_stream", fake_single)
    # 並列パスに入ったら即失敗するようにして「呼ばれない」ことを保証
    monkeypatch.setattr(llm, "score_horses_parallel",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("並列に入ってはいけない")))

    rd = _mk_race(8)
    events = list(llm.score_horses(rd))
    assert called["n"] == 1
    assert ("result", "DEFAULT_SINGLE") in events


def test_dispatcher_off_when_too_few_horses(monkeypatch):
    monkeypatch.setenv("KEIBA_SCORE_PARALLEL", "1")
    monkeypatch.setenv("KEIBA_SCORE_MIN_HORSES_FOR_PARALLEL", "8")

    def fake_single(rd, **kw):
        yield ("result", "SINGLE_SMALL_FIELD")
    monkeypatch.setattr(llm, "score_horses_stream", fake_single)
    monkeypatch.setattr(llm, "score_horses_parallel",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("少頭数で並列に入ってはいけない")))

    rd = _mk_race(6)   # < 8 頭 → 単一セッション
    events = list(llm.score_horses(rd))
    assert ("result", "SINGLE_SMALL_FIELD") in events


def test_score_prompts_have_no_market_odds():
    """Claude 指数の score プロンプト (単一 + 並列 RESEARCH/SCORING) は単勝オッズ・人気を
    一切載せない = 市場非依存 (2026-06-21)。適性総合は過去走由来でオッズ非依存なので残す。"""
    from types import SimpleNamespace
    rd = _mk_race(6)
    rd.race.horses[0].win_odds = 77.7          # 距離/馬体重と衝突しない distinctive 値
    apt = {1: SimpleNamespace(total=88.0)}
    prompts = {
        "score": llm.build_horse_score_prompt(rd, aptitudes=apt),
        "research": llm.build_horse_research_prompt(rd, [1, 2], aptitudes=apt),
        "from_research": llm.build_horse_score_from_research_prompt(
            rd, {1: {"alerts": [], "support": 1, "digest": "d"}}, aptitudes=apt),
    }
    for label, p in prompts.items():
        assert "| 単勝 |" not in p, f"{label}: 単勝オッズ列が残存"
        assert "単勝オッズ)" not in p, f"{label}: 表ヘッダに単勝オッズ"
        assert "77.7" not in p, f"{label}: 単勝オッズ値が漏洩"
        assert "| 88 |" in p, f"{label}: 適性総合が落ちている"
    # オッズ・人気アンカー指示は除去、意図的非提示の注記は明記。
    assert "オッズの常識" not in prompts["score"]
    assert "人気・評価が" not in prompts["score"]
    assert "意図的に与えていません" in prompts["score"]
