"""llm.py の JSON 抽出 (parse_evidence / parse_bundle_review) の robustness テスト。"""
from __future__ import annotations

from src import llm


def test_parse_evidence_clean_fence():
    text = 'before\n```json\n{"cuts": ["wide:3-7"], "summary": "ok"}\n```\nafter'
    assert llm.parse_evidence(text) == {"cuts": ["wide:3-7"], "summary": "ok"}


def test_parse_evidence_prose_between_brace_and_fence():
    """閉じ } の後に散文があってフェンスが離れていても拾える (regex は取りこぼす)。"""
    text = '```json\n{"cuts": ["win:1"]}\nこの根拠で cut します。\n```'
    assert llm.parse_evidence(text) == {"cuts": ["win:1"]}


def test_parse_evidence_missing_closing_fence():
    """閉じフェンス ``` が欠落していても brace-balance で拾える。"""
    text = 'まとめ:\n```json\n{"cuts": [], "confidence": "high"}'
    assert llm.parse_evidence(text) == {"cuts": [], "confidence": "high"}


def test_parse_evidence_nested_object():
    text = '```json\n{"notes": {"3": "取消"}, "cuts": ["wide:3-7"]}\n```'
    assert llm.parse_evidence(text) == {"notes": {"3": "取消"}, "cuts": ["wide:3-7"]}


def test_parse_evidence_no_json():
    assert llm.parse_evidence("根拠が見つかりませんでした") == {}
    assert llm.parse_evidence("") == {}


def test_parse_bundle_review_normalizes():
    text = '```json\n{"cuts": ["wide:3-7", 5], "summary": "x", "confidence": "low"}\n```'
    r = llm.parse_bundle_review(text)
    assert r["cuts"] == ["wide:3-7", "5"]   # str 化
    assert r["summary"] == "x"
    assert r["confidence"] == "low"
    assert r["notes"] == {}


def test_parse_bundle_review_picks():
    """新方式: picks (買う leg id) を抽出。cuts 不在でも picks を取る。"""
    text = '選定\n```json\n{"picks": ["win:7", "wide:2-11"], "summary": "x", "confidence": "high"}\n```'
    r = llm.parse_bundle_review(text)
    assert r["picks"] == ["win:7", "wide:2-11"]
    assert r["cuts"] == []          # cuts 不在 → 空
    assert r["confidence"] == "high"


def test_parse_bundle_review_picks_absent_is_none():
    """picks が無ければ None (cuts のみの後方互換経路に回せる)。"""
    r = llm.parse_bundle_review('```json\n{"cuts": ["wide:3-7"]}\n```')
    assert r["picks"] is None and r["cuts"] == ["wide:3-7"]


def test_parse_horse_scores_strength_format():
    """正規形: scores (0-100 指数) + support → scale='strength'。"""
    txt = '```json\n{"scores": {"5": 90, "8": 58}, "support": {"5": 3, "8": 0}, ' \
          '"notes": {"5": "距離適性◎"}, "summary": "x", "confidence": "mid"}\n```'
    p = llm.parse_horse_scores(txt)
    assert p["scale"] == "strength"
    assert p["scores"] == {5: 90.0, 8: 58.0}
    assert p["support"] == {5: 3, 8: 0}


def test_parse_horse_scores_prob_fallback():
    """後方互換: win_prob (%) のみ → scale='prob'。"""
    txt = '```json\n{"win_prob": {"5": 55.0, "8": 12.0}, "summary": "", "confidence": ""}\n```'
    p = llm.parse_horse_scores(txt)
    assert p["scale"] == "prob"
    assert p["scores"] == {5: 55.0, 8: 12.0}
    assert p["support"] == {}


def test_parse_horse_scores_broken_returns_empty():
    p = llm.parse_horse_scores("no json here")
    assert p["scores"] == {}
    assert p["scale"] == "strength"
    assert p["alerts"] == {}   # alerts キーは常に存在 (空でも壊れない)


def test_parse_horse_scores_with_alerts():
    """直前/軟情報フラグ (alerts) を {int:[str]} に正規化。取消馬は scores 0。"""
    txt = ('```json\n{"scores": {"7": 82, "3": 0}, "support": {"7": 2, "3": 1}, '
           '"alerts": {"7": ["前走不利", "厩舎勝負気配"], "3": ["取消", "馬体重-12kg"]}, '
           '"summary": "x", "confidence": "high"}\n```')
    p = llm.parse_horse_scores(txt)
    assert p["scores"] == {7: 82.0, 3: 0.0}
    assert p["alerts"] == {7: ["前走不利", "厩舎勝負気配"], 3: ["取消", "馬体重-12kg"]}


def test_parse_horse_scores_alerts_absent_is_empty():
    """alerts フィールド不在 (旧出力) でも空 dict で後方互換。"""
    txt = '```json\n{"scores": {"5": 90}, "support": {"5": 1}}\n```'
    p = llm.parse_horse_scores(txt)
    assert p["scores"] == {5: 90.0}
    assert p["alerts"] == {}


def test_normalize_alerts_robustness():
    """単一文字列→リスト化、空/None 除外、空配列の馬は落とす、壊れた入力は {}。"""
    assert llm._normalize_alerts({"3": "取消"}) == {3: ["取消"]}          # str → [str]
    assert llm._normalize_alerts({"7": ["前走不利", "", None]}) == {7: ["前走不利"]}  # 空除外
    assert llm._normalize_alerts({"2": []}) == {}                        # 空配列は落とす
    assert llm._normalize_alerts({"bad": ["x"]}) == {}                   # 非整数キーは無視
    assert llm._normalize_alerts(None) == {}                            # 壊れた入力
    assert llm._normalize_alerts("nope") == {}
