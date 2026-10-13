"""仮指数 (Claude の叩き台) と当日の馬場状態 (2026-10-10 レビュー指摘の修正)。

keiba.go.jp / JRA 公式経路に当日の馬場状態が入ったため、仮指数は馬場に依存するようになった。
凍結値の馬場と Claude に見せる馬場が食い違わないよう:
  - Claude が採点するとき、馬場が判明していて凍結時と違えば作り直す (過去走が半数以上ある時だけ)
  - その採点で渡した仮指数を .llm.json に残し、snapshot の仮指数 / prov_delta はそれを使う
"""
from __future__ import annotations

import json

import pytest

from src import analyze as az
from src.models import Horse, PastRun, Race, RaceData


def _pr(date, own, going="良", dist=1400):
    return PastRun(date=date, venue="大井", surface="ダート", distance=dist, going=going,
                   winner_time_sec=own, time_diff_sec=0.0, finish_pos=1 if own < 89 else None)


def _rd(weather_text="晴 / 良", with_past=True):
    hs = []
    for n, base in ((1, 87.0), (2, 88.5), (3, 90.0)):
        runs = [_pr("2026.09.20", base), _pr("2026.08.30", base + 0.4, "不", 1200 if n == 2 else 1400),
                _pr("2026.08.10", base + 0.8, "重")] if with_past else []
        hs.append(Horse(number=n, name=f"h{n}", past_runs=runs))
    return RaceData(race=Race(cup_id="c", schedule_index=1, race_number=1, venue_id=44, venue_name="大井",
                              race_class="C1", distance=1400, surface="ダート", weather_text=weather_text,
                              horses=hs), trifecta=[])


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(az, "ROOT", tmp_path)
    (tmp_path / "data" / "predictions").mkdir(parents=True)
    return tmp_path


def _write_snapshot(root, rid, prov, sev):
    (root / "data" / "predictions" / f"{rid}.json").write_text(
        json.dumps({"provisional_index": {str(k): v for k, v in prov.items()}, "provisional_going_sev": sev}),
        encoding="utf-8")


def test_claude_run_recomputes_when_going_changes(root):
    frozen = {1: 71.0, 2: 42.0, 3: 55.0}
    _write_snapshot(root, "R-1", frozen, 0)                       # 良で凍結
    vals, sev = az._resolve_provisional_with_sev("R-1", _rd("雨 / 不良"), for_claude_run=True)
    assert sev == 3 and vals != frozen and set(vals) == {1, 2, 3}
    # 同じ馬場なら凍結値のまま
    vals2, sev2 = az._resolve_provisional_with_sev("R-1", _rd("晴 / 良"), for_claude_run=True)
    assert vals2 == frozen and sev2 == 0


def test_claude_run_keeps_frozen_when_going_unknown_or_degraded(root):
    frozen = {1: 71.0, 2: 42.0, 3: 55.0}
    _write_snapshot(root, "R-2", frozen, 0)
    # 馬場不明 (取得失敗) は「変化」とみなさない
    assert az._resolve_provisional_with_sev("R-2", _rd(""), for_claude_run=True)[0] == frozen
    # 過去走が取れていない縮退 rd では作り直さない (全馬 50 で上書きしない)
    assert az._resolve_provisional_with_sev("R-2", _rd("雨 / 不良", with_past=False),
                                            for_claude_run=True)[0] == frozen


def test_snapshot_uses_anchor_claude_saw(root):
    anchor = {1: 66.0, 2: 48.0, 3: 51.0}
    parsed = {"scores": {1: 70, 2: 50, 3: 40}, "support": {}, "scale": "strength"}
    az._save_llm_scores("R-3", parsed, model="test", anchor=anchor, anchor_sev=2)
    saved = json.loads(az._llm_scores_path("R-3").read_text(encoding="utf-8"))
    assert saved["provisional"] == {"1": 66.0, "2": 48.0, "3": 51.0} and saved["provisional_going_sev"] == 2
    # snapshot 側 (for_claude_run=False) は凍結値ではなく Claude に渡した値を返す
    _write_snapshot(root, "R-3", {1: 71.0, 2: 42.0, 3: 55.0}, 0)
    vals, sev = az._resolve_provisional_with_sev("R-3", _rd("雨 / 重"))
    assert vals == anchor and sev == 2


def test_llm_scores_keep_official_enrich(root):
    """採点時の公式データ補完の件数を .llm.json に残す (地方の入力が補完済みだったかの記録, 2026-10-14)。"""
    parsed = {"scores": {1: 70, 2: 50}, "support": {}, "scale": "strength"}
    st = {"past_total": 10, "past_filled": 9, "past_mismatch": 0, "entries_filled": 2, "runners_no_jockey": 0}
    az._save_llm_scores("R-5", parsed, model="test", anchor={1: 60.0, 2: 40.0}, official_enrich=st)
    saved = json.loads(az._llm_scores_path("R-5").read_text(encoding="utf-8"))
    assert saved["official_enrich"] == st
    az._save_llm_scores("R-6", parsed, model="test")
    assert json.loads(az._llm_scores_path("R-6").read_text(encoding="utf-8"))["official_enrich"] is None


def test_legacy_wrapper_unchanged(root):
    """既存の _resolve_provisional (dict を返す凍結ロジック) は従来どおり。"""
    _write_snapshot(root, "R-4", {1: 71.0, 2: 42.0, 3: 55.0}, None)
    assert az._resolve_provisional("R-4", _rd("雨 / 不良")) == {1: 71.0, 2: 42.0, 3: 55.0}
