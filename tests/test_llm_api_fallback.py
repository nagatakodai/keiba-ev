"""claude -p usage limit → Anthropic API フォールバック (src/llm.py) のユニットテスト。

すべて OFFLINE: claude CLI も Anthropic API も呼ばない。llm._spawn_claude /
llm._api_stream を差し替えて「limit で結果が出ない → API へフォールバック」と
「limit 以外の失敗ではフォールバックしない (従来挙動)」を検証する。
"""
from __future__ import annotations

import io
import json

from src import llm
from src.models import Horse, Race, RaceData, Weather


def _mk_race(n_horses: int = 4) -> RaceData:
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


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def _fake_api_stream(prompt, *, use_search, timeout, effort="high", max_tokens=32_000):
    yield ("text", "[api]")
    yield ("result", '{"scores": {"1": 80}}')


# ---------- _looks_rate_limited ----------

def test_looks_rate_limited_patterns():
    assert llm._looks_rate_limited("Claude AI usage limit reached|1751600000")
    assert llm._looks_rate_limited("claude exit 1: rate_limit_error ...")
    assert llm._looks_rate_limited("5-hour limit reached ∙ resets 3am")
    assert not llm._looks_rate_limited("claude timeout")          # 時間切れは limit ではない
    assert not llm._looks_rate_limited("stream parse error: x")
    assert not llm._looks_rate_limited("")


# ---------- score_horses_stream ----------

def test_score_falls_back_to_api_on_limit(monkeypatch):
    """rc=1 + stderr に usage limit → _api_stream の result が流れる。"""
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(
        llm, "_spawn_claude",
        lambda cmd: (_FakeProc([], returncode=1),
                     io.StringIO("Claude AI usage limit reached|1751600000")))
    monkeypatch.setattr(llm, "_api_stream", _fake_api_stream)
    events = list(llm.score_horses_stream(_mk_race()))
    results = [p for t, p in events if t == "result" and p]
    assert results == ['{"scores": {"1": 80}}']
    assert any("フォールバック" in p for t, p in events if t == "text")


def test_score_limit_in_result_event_triggers_fallback(monkeypatch):
    """result イベント自体が limit メッセージ → 採点と誤認せず error 化してフォールバック。"""
    line = json.dumps({"type": "result", "result": "Claude AI usage limit reached|123"})
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(
        llm, "_spawn_claude", lambda cmd: (_FakeProc([line + "\n"]), io.StringIO()))
    monkeypatch.setattr(llm, "_api_stream", _fake_api_stream)
    events = list(llm.score_horses_stream(_mk_race()))
    results = [p for t, p in events if t == "result" and p]
    assert results == ['{"scores": {"1": 80}}']   # limit メッセージは result に残らない
    assert any("claude -p limit" in str(p) for t, p in events if t == "error")


def test_score_no_fallback_on_non_limit_failure(monkeypatch):
    """limit 以外の失敗 (rc=1 の一般エラー) ではフォールバックしない (従来挙動)。"""
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(
        llm, "_spawn_claude",
        lambda cmd: (_FakeProc([], returncode=1), io.StringIO("some other failure")))
    called = []
    monkeypatch.setattr(llm, "_api_stream",
                        lambda *a, **k: called.append(1) or iter(()))
    events = list(llm.score_horses_stream(_mk_race()))
    assert not called
    assert not [p for t, p in events if t == "result" and p]


def test_score_no_fallback_when_disabled(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setenv("KEIBA_API_FALLBACK", "0")
    monkeypatch.setattr(
        llm, "_spawn_claude",
        lambda cmd: (_FakeProc([], returncode=1),
                     io.StringIO("Claude AI usage limit reached|1")))
    called = []
    monkeypatch.setattr(llm, "_api_stream",
                        lambda *a, **k: called.append(1) or iter(()))
    list(llm.score_horses_stream(_mk_race()))
    assert not called


def test_score_normal_result_does_not_fallback(monkeypatch):
    """正常 result が出たらフォールバックしない。"""
    line = json.dumps({"type": "result", "result": '{"scores": {"1": 70}}'})
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(
        llm, "_spawn_claude", lambda cmd: (_FakeProc([line + "\n"]), io.StringIO()))
    called = []
    monkeypatch.setattr(llm, "_api_stream",
                        lambda *a, **k: called.append(1) or iter(()))
    events = list(llm.score_horses_stream(_mk_race()))
    assert [p for t, p in events if t == "result"] == ['{"scores": {"1": 70}}']
    assert not called


# ---------- select_trifecta_stream ----------

def test_trifecta_select_falls_back_without_search(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(
        llm, "_spawn_claude",
        lambda cmd: (_FakeProc([], returncode=1),
                     io.StringIO("rate limit reached")))
    seen_kwargs = {}

    def fake_api(prompt, *, use_search, timeout, effort="high", max_tokens=32_000):
        seen_kwargs.update(use_search=use_search, max_tokens=max_tokens)
        yield ("result", '{"keys": [[1,2,3]]}')

    monkeypatch.setattr(llm, "_api_stream", fake_api)
    events = list(llm.select_trifecta_stream(_mk_race(), llm_index={1: 90.0, 2: 80.0, 3: 70.0}))
    assert [p for t, p in events if t == "result" and p] == ['{"keys": [[1,2,3]]}']
    assert seen_kwargs["use_search"] is False        # 締切直前選定は検索なし
    assert seen_kwargs["max_tokens"] == 8_000


# ---------- helpers ----------

def test_api_stream_requires_key(monkeypatch):
    monkeypatch.setattr(llm, "_api_key", lambda: "")
    events = list(llm._api_stream("p", use_search=False, timeout=60))
    assert events and events[0][0] == "error" and "ANTHROPIC_API_KEY" in events[0][1]


def test_api_fallback_enabled_env(monkeypatch):
    monkeypatch.delenv("KEIBA_API_FALLBACK", raising=False)
    assert llm._api_fallback_enabled()
    monkeypatch.setenv("KEIBA_API_FALLBACK", "0")
    assert not llm._api_fallback_enabled()
    monkeypatch.setenv("KEIBA_API_FALLBACK", "false")
    assert not llm._api_fallback_enabled()
