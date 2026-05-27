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
