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


def test_score_prompt_query_budget_configurable():
    """build_horse_score_prompt の検索予算は queries_per_horse で可変 (既定 2 / shobu は 10)。"""
    rd = _mk_race(6)
    p2 = llm.build_horse_score_prompt(rd)                 # 既定 2/頭
    assert "1 頭あたり約 2 クエリ" in p2
    assert "~12 クエリ" in p2                             # 6 頭 × 2
    p10 = llm.build_horse_score_prompt(rd, queries_per_horse=10)
    assert "1 頭あたり約 10 クエリ" in p10
    assert "~60 クエリ" in p10                            # 6 頭 × 10


def test_single_session_respects_query_env(monkeypatch):
    """単一セッション (score_horses_stream) も KEIBA_SCORE_QUERIES_PER_HORSE を尊重する。

    並列しきい値未満の少頭数でも、shobu 等が env を立てれば「頭数 × N」クエリが流れる。
    """
    monkeypatch.setenv("KEIBA_SCORE_QUERIES_PER_HORSE", "10")
    monkeypatch.setattr(llm, "is_available", lambda: True)
    captured: dict[str, str] = {}

    def fake_spawn(cmd):
        captured["prompt"] = cmd[2]
        return _FakeProc([_result_line(_PHASE_B_SCORES)]), io.StringIO()

    monkeypatch.setattr(llm, "_spawn_claude", fake_spawn)
    rd = _mk_race(6)                                      # < 8 頭 → 単一セッション経路
    list(llm.score_horses_stream(rd))
    assert "1 頭あたり約 10 クエリ" in captured["prompt"]
    assert "~60 クエリ" in captured["prompt"]             # 6 頭 × 10


def test_single_session_default_query_budget(monkeypatch):
    """env 未設定なら単一セッションは従来どおり 2/頭 (挙動不変)。"""
    monkeypatch.delenv("KEIBA_SCORE_QUERIES_PER_HORSE", raising=False)
    monkeypatch.setattr(llm, "is_available", lambda: True)
    captured: dict[str, str] = {}

    def fake_spawn(cmd):
        captured["prompt"] = cmd[2]
        return _FakeProc([_result_line(_PHASE_B_SCORES)]), io.StringIO()

    monkeypatch.setattr(llm, "_spawn_claude", fake_spawn)
    rd = _mk_race(6)
    list(llm.score_horses_stream(rd))
    assert "1 頭あたり約 2 クエリ" in captured["prompt"]


def test_research_prompt_full_field_budget():
    """並列 RESEARCH は担当頭数 × queries_per_horse 分の予算を提示 (全シャード合算で頭数×N)。"""
    rd = _mk_race(16)
    # per_shard=3 で 16 頭を分割 → 各シャードの予算が 担当頭数 × 10 になることを確認。
    shards = llm._shard_numbers(list(range(1, 17)), 3, 5)
    assert sum(len(s) for s in shards) == 16             # 全馬被覆 = 合計 16×10=160 クエリ
    total_budget = 0
    for s in shards:
        p = llm.build_horse_research_prompt(rd, s, queries_per_horse=10)
        assert f"~{len(s) * 10} クエリ" in p              # 担当 len(s) 頭 × 10
        total_budget += len(s) * 10
    assert total_budget == 160                            # 頭数 × 10


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


def test_normalize_evidence_keeps_all_items():
    """_normalize_evidence は 3 件で打ち切らず全件 (上限なし) を保持し、空/壊れを除去する。"""
    raw = {"7": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "", None],  # 11 valid
           "3": "単一文字列も配列化",
           "x": ["数値化できないキーは無視"]}
    out = llm._normalize_evidence(raw)
    assert set(out) == {7, 3}                      # 無効キー "x" は落ちる
    assert len(out[7]) == 11                        # 空/None 除去後の全件 (>3 = cap なし)
    assert out[3] == ["単一文字列も配列化"]
    # 各要素は max_len でクランプ、件数も緩い上限まで保持。
    big = {"1": ["z" * 500] + [f"e{i}" for i in range(50)]}
    out2 = llm._normalize_evidence(big)
    assert len(out2[1][0]) == 300                   # 1 要素 300 字クランプ
    assert len(out2[1]) == 40                        # max_items 既定 40 (3 ではない)


def test_parse_horse_scores_evidence_and_support_backfill():
    """evidence を解析し、support 未指定/過小でも evidence 件数で補完する。"""
    text = ('```json\n{"scores": {"7": 80, "2": 50},'
            ' "support": {"2": 1},'
            ' "evidence": {"7": ["根拠1", "根拠2", "根拠3", "根拠4", "根拠5"], "2": ["x"]},'
            ' "summary": "s", "confidence": "high"}\n```')
    parsed = llm.parse_horse_scores(text)
    assert parsed["evidence"][7] == ["根拠1", "根拠2", "根拠3", "根拠4", "根拠5"]
    assert parsed["support"][7] == 5                # support 未指定 → evidence 件数で補完
    assert parsed["support"][2] == 1                # support(1) >= len(evidence)=1 はそのまま
    # evidence 無しの壊れ出力でも raise せず空で返す。
    assert llm.parse_horse_scores("no json")["evidence"] == {}


def test_merge_research_carries_evidence():
    """_merge_research が facts.evidence を馬番キーへ union し support を件数で補完する。"""
    txt = ('```json\n{"facts": {"5": {"alerts": ["前走不利"],'
           ' "evidence": ["e1", "e2", "e3"], "support": 1, "digest": "d"}}}\n```')
    out = llm._merge_research([txt])
    assert out[5]["evidence"] == ["e1", "e2", "e3"]
    assert out[5]["support"] == 3                   # support(1) < len(evidence)=3 → 3 に補完


def test_score_prompts_advertise_unbounded_evidence():
    """score / research / scoring プロンプトが「上限なし・10件以上可」+ evidence schema を明記。"""
    rd = _mk_race(6)
    score = llm.build_horse_score_prompt(rd)
    research = llm.build_horse_research_prompt(rd, [1, 2, 3])
    scoring = llm.build_horse_score_from_research_prompt(
        rd, {1: {"alerts": [], "evidence": ["EV_MARK"], "support": 1, "digest": "d"}})
    for label, p in (("score", score), ("research", research), ("scoring", scoring)):
        assert "evidence" in p, f"{label}: evidence 指示が無い"
        assert "10 件以上" in p, f"{label}: 上限なし (10件以上) の明記が無い"
    assert '"evidence"' in score                    # JSON schema 例に evidence
    assert "EV_MARK" in scoring                      # 収集済み evidence を採点段へ渡す


def test_normalize_paddock_dict_and_string_forms():
    """_normalize_paddock が dict 形 / 文字列形 (先頭記号を rating に分離) 両方を正規化する。"""
    out = llm._normalize_paddock({
        "7": {"rating": "◎", "note": "トモ充実・毛艶◎"},
        "2": "△ 発汗やや多・イレ込み気味",       # 文字列 → 先頭記号を rating へ
        "3": "気配は平凡",                        # 記号なし → note のみ
        "9": {"rating": "", "note": ""},          # 空は落とす
        "x": {"rating": "○", "note": "n"},        # 非数値キーは無視
    })
    assert out[7] == {"rating": "◎", "note": "トモ充実・毛艶◎"}
    assert out[2] == {"rating": "△", "note": "発汗やや多・イレ込み気味"}
    assert out[3] == {"rating": "", "note": "気配は平凡"}
    assert 9 not in out and "x" not in out and 0 not in out
    assert llm._normalize_paddock(None) == {} and llm._normalize_paddock("nope") == {}


def test_parse_horse_scores_carries_paddock():
    """parse_horse_scores が paddock フィールドを正規化して返す (壊れ出力は空 dict)。"""
    text = ('```json\n{"scores": {"7": 80},'
            ' "paddock": {"7": {"rating": "◎", "note": "気配良・チャカつき無"}},'
            ' "summary": "s", "confidence": "high"}\n```')
    parsed = llm.parse_horse_scores(text)
    assert parsed["paddock"][7] == {"rating": "◎", "note": "気配良・チャカつき無"}
    assert llm.parse_horse_scores("no json")["paddock"] == {}


def test_merge_research_carries_paddock():
    """_merge_research が facts.paddock を馬番キーへ union する (並列リサーチ→採点段の橋渡し)。"""
    txt = ('```json\n{"facts": {"5": {"alerts": [], "evidence": ["e1"], "support": 1,'
           ' "paddock": {"rating": "○", "note": "トモの張り良"}, "digest": "d"}}}\n```')
    out = llm._merge_research([txt])
    assert out[5]["paddock"] == {"rating": "○", "note": "トモの張り良"}
    # scoring プロンプトが収集済みパドックを採点段へ渡し、paddock schema も明記する。
    scoring = llm.build_horse_score_from_research_prompt(_mk_race(6), out)
    assert "トモの張り良" in scoring and "paddock" in scoring


def test_score_prompts_advertise_paddock():
    """score / research プロンプトがパドック評価 (専用クエリ + paddock schema) を明記。"""
    rd = _mk_race(6)
    for label, p in (("score", llm.build_horse_score_prompt(rd)),
                     ("research", llm.build_horse_research_prompt(rd, [1, 2, 3]))):
        assert "パドック" in p, f"{label}: パドック指示が無い"
        assert "paddock" in p, f"{label}: paddock フィールドの明記が無い"


def test_provisional_threads_into_all_score_prompts():
    """仮指数 (provisional) が 出走馬表列 + anchor→±調整 の framing として3プロンプトに伝播する。"""
    rd = _mk_race(8)
    prov = {i: 55.0 + i for i in range(1, 9)}   # 馬8 = 63
    score = llm.build_horse_score_prompt(rd, provisional=prov)
    research = llm.build_horse_research_prompt(rd, [1, 2, 3], provisional=prov)
    scoring = llm.build_horse_score_from_research_prompt(
        rd, {1: {"evidence": ["e"], "support": 1}}, provisional=prov)
    for label, p in (("score", score), ("research", research), ("scoring", scoring)):
        assert "| 仮指数 |" in p, f"{label}: 仮指数列が無い"
    assert "## 仮指数の扱い (anchor → ± 調整)" in score
    assert "| 63 |" in score                       # 仮指数値がテーブルに載る
    assert "| 適性 |" not in score                 # 適性列は仮指数に置換
    # provisional 無しでも壊れない (適性 or '-' でフォールバック)。
    assert "| 仮指数 |" in llm.build_horse_score_prompt(rd)


def test_paddock_persists_to_llm_json_and_index_compare(tmp_path, monkeypatch):
    """paddock が .llm.json → _load_llm_scores(7-tuple) → index_compare[].paddock まで伝わる。"""
    from src import analyze as az
    monkeypatch.setattr(az, "ROOT", tmp_path)
    (tmp_path / "data" / "predictions").mkdir(parents=True)
    rid = "T-1-11"
    parsed = llm.parse_horse_scores(
        '```json\n{"scores": {"1": 90, "2": 60}, "support": {"1": 1},'
        ' "evidence": {"1": ["根拠A"]},'
        ' "paddock": {"1": {"rating": "◎", "note": "気配良・トモ充実"},'
        ' "2": {"rating": "△", "note": "イレ込み気味"}}}\n```')
    az._save_llm_scores(rid, parsed, model="test")
    # 7-tuple の末尾に paddock。
    loaded = az._load_llm_scores(rid, max_age_sec=10**9)
    assert len(loaded) == 7
    scores, support, scale, at, alerts, evidence, paddock = loaded
    assert paddock[1] == {"rating": "◎", "note": "気配良・トモ充実"}
    # index_compare 行に paddock が乗る (馬1=◎、馬2=△)。
    rd = _mk_race(2)
    rd.race.horses[0].number, rd.race.horses[1].number = 1, 2
    ic = az._build_index_compare(rd, scores, support, alerts, evidence, paddock)
    by_num = {r["number"]: r for r in ic}
    assert by_num[1]["paddock"] == {"rating": "◎", "note": "気配良・トモ充実"}
    assert by_num[2]["paddock"] == {"rating": "△", "note": "イレ込み気味"}


def test_resolve_provisional_freezes_after_first_save(tmp_path, monkeypatch):
    """仮指数は既存 snapshot にあれば再利用して凍結 (オッズ更新/再score で揺れない)。"""
    from src import analyze as az
    monkeypatch.setattr(az, "ROOT", tmp_path)
    (tmp_path / "data" / "predictions").mkdir(parents=True)
    rid = "T-9-9"
    rd = _mk_race(3)
    # snapshot 未保存 → rd から計算 (dict を返す・raise しない)。
    assert isinstance(az._resolve_provisional(rid, rd), dict)
    # 既存 snapshot を書くと以後はそれを再利用する。
    (tmp_path / "data" / "predictions" / f"{rid}.json").write_text(
        __import__("json").dumps({"provisional_index": {"1": 71.0, "2": 42.0, "3": 55.0}}),
        encoding="utf-8")
    assert az._resolve_provisional(rid, rd) == {1: 71.0, 2: 42.0, 3: 55.0}
    # rd の past_runs を空にしても (=別 rd) 凍結値は不変 = 市場更新に対する安定性。
    for h in rd.race.horses:
        h.past_runs = []
    assert az._resolve_provisional(rid, rd) == {1: 71.0, 2: 42.0, 3: 55.0}


def test_index_compare_threads_provisional_and_delta():
    """_build_index_compare が仮指数 (provisional) と prov_delta (Claude − 仮) を各行に載せる。"""
    from src import analyze as az
    rd = _mk_race(2)
    rd.race.horses[0].number, rd.race.horses[1].number = 1, 2
    ic = az._build_index_compare(rd, {1: 80.0, 2: 40.0}, None, None, None, None,
                                 {1: 70.0, 2: 55.0})
    by = {r["number"]: r for r in ic}
    assert by[1]["provisional"] == 70.0 and by[1]["prov_delta"] == 10.0   # Claude 80 − 仮 70
    assert by[2]["provisional"] == 55.0 and by[2]["prov_delta"] == -15.0  # Claude 40 − 仮 55
    # 仮指数が無ければ None (旧 snapshot 互換)。
    ic0 = az._build_index_compare(rd, {1: 80.0}, None, None, None, None, None)
    assert all(r["provisional"] is None and r["prov_delta"] is None for r in ic0)


def _mk_race_with_past(n_horses: int = 8):
    """過去走 (PastRun) 付きの RaceData (前走戦績セクション検証用)。"""
    from src.models import PastRun
    rd = _mk_race(n_horses)
    for h in rd.race.horses:
        h.past_runs = [
            PastRun(date="2026.06.15", venue="盛岡", race_no=11, race_class="B1",
                    surface="ダ", distance=1400, going="良", field_size=12,
                    popularity=h.number, finish_pos=3, last_3f_sec=37.2, passing="5-5"),
            PastRun(date="2026.06.01", venue="水沢", race_no=10, race_class="B2",
                    surface="ダ", distance=1600, going="稍", field_size=11,
                    popularity=2, finish_pos=1, last_3f_sec=38.1, passing="3-3-2-1"),
        ]
    return rd


def test_past_runs_section_in_all_prompts():
    """前走戦績 (公式データ) が score/research/scoring 3 プロンプトに描画され、検索抑止文も入る。"""
    rd = _mk_race_with_past(8)
    src = "地方競馬公式(keiba.go.jp)"
    score = llm.build_horse_score_prompt(rd, past_source=src)
    research = llm.build_horse_research_prompt(rd, [1, 2], past_source=src)
    scoring = llm.build_horse_score_from_research_prompt(
        rd, {1: {"alerts": [], "evidence": ["e"], "support": 1, "digest": "d"}},
        past_source=src)
    for label, p in (("score", score), ("research", research), ("scoring", scoring)):
        assert "## 前走戦績" in p, f"{label}: 前走戦績セクションが無い"
        assert src in p, f"{label}: 公式ソースのラベルが無い"
        assert "盛岡11R" in p, f"{label}: 馬柱の内容が描画されていない"
        assert "検索しない" in p, f"{label}: 前走戦績の検索抑止文が無い"


def test_past_runs_section_does_not_break_partition():
    """前走戦績セクションの本文に partition マーカーを含めない (string-surgery 保護)。"""
    rd = _mk_race_with_past(8)
    base = llm.build_horse_score_prompt(rd, past_source="JRA公式(jra.go.jp)")
    # base の 前走戦績 ～ 検索 MCP マーカー の区間にマーカー文字列が無い。
    i = base.find("## 前走戦績")
    j = base.find("## 検索 MCP の運用ルール")
    assert 0 <= i < j, "前走戦績は 検索 MCP マーカーより前に置く"
    section = base[i:j]
    assert "## 検索 MCP の運用ルール" not in section
    assert "## 指数の付け方" not in section
    # 派生 2 プロンプトの partition が壊れていない (マーカー/差し替えが従来どおり)。
    scoring = llm.build_horse_score_from_research_prompt(rd, {})
    assert "## 指数の付け方" in scoring
    assert "## 検索 MCP の運用ルール" not in scoring


def test_past_runs_absent_keeps_search_for_recent_runs():
    """過去走が無いレースは 前走戦績 を出さず、近走を検索する従来挙動を維持。"""
    rd = _mk_race(8)   # past_runs 無し
    p = llm.build_horse_score_prompt(rd)
    assert "## 前走戦績" not in p
    assert "直近5走の着順詳細" in p   # 従来の検索ルール (近走を検索) が残る


def test_classify_and_persist_tool_usage(tmp_path, monkeypatch):
    """score 段ツール利用の種別判定 + per-race jsonl 永続化 (ユーザ指示 2026-06-30)。"""
    import json
    import src.analyze as a

    assert a._classify_tool("mcp__tavily__tavily_search") == "search"
    assert a._classify_tool("mcp__tavily__tavily_extract") == "extract"
    assert a._classify_tool("WebFetch") == "fetch"
    assert a._classify_tool("WebSearch") == "websearch"
    assert a._classify_tool("Read") == "read"
    assert a._classify_tool("Bash") == "other"

    monkeypatch.setattr(a, "_TOOL_USAGE_DIR", tmp_path)
    a._append_tool_usage("2026440630-630-6", "mcp__tavily__tavily_search", "馬A パドック")
    a._append_tool_usage("2026440630-630-6", "WebFetch", "https://x/y")
    a._append_tool_usage("../evil", "WebFetch", "x")   # path traversal は安全化される
    rows = [json.loads(l) for l in (tmp_path / "2026440630-630-6.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["search", "fetch"]
    assert rows[0]["query"] == "馬A パドック"
    # traversal 文字は除去され ".." 単体パスは作らない
    assert not (tmp_path / ".." / "evil.jsonl").exists()
