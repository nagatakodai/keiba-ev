"""analyze._decide_selection_bundle: claude の picks/cuts から最終束を決める分岐。

主眼: claude が picks=[] (明示的な見送り) を返したとき、モデルの全束が validated 扱いで
投入されてしまう回帰を防ぐ (`if picks:` だと [] が falsy で素通りしていた)。
"""
from __future__ import annotations

from src import analyze as az
from src import portfolio as pf


def _patch_build_bundle(monkeypatch):
    """build_bundle を「渡された脚をそのまま legs に並べる」スタブに差し替え。"""
    monkeypatch.setattr(
        pf, "build_bundle",
        lambda legs, probs: {"legs": [{"bet_type": c["bet_type"], "key": list(c["key"])}
                                      for c in legs]})


CANDS = [
    {"bet_type": "win", "key": [1]},
    {"bet_type": "wide", "key": [3, 7]},
    {"bet_type": "trifecta", "key": [1, 2, 3]},
]
MODEL = {"legs": [dict(c) for c in CANDS], "src": "model"}


def test_explicit_empty_picks_is_kenagshi(monkeypatch):
    """picks=[] (見送り) → 空束を適用 (モデル束を投入しない)。"""
    _patch_build_bundle(monkeypatch)
    bundle, applied = az._decide_selection_bundle({"picks": []}, CANDS, None, MODEL)
    assert applied is True
    assert bundle["legs"] == []          # 見送り = 空束 (モデルの全束ではない)


def test_valid_picks_rebuild(monkeypatch):
    _patch_build_bundle(monkeypatch)
    bundle, applied = az._decide_selection_bundle(
        {"picks": ["wide:3-7"]}, CANDS, None, MODEL)
    assert applied is True
    assert bundle["legs"] == [{"bet_type": "wide", "key": [3, 7]}]


def test_all_invalid_picks_keeps_model_unvalidated(monkeypatch):
    """picks が全て候補 id に不一致 → 適用不可 (applied=False)、モデル束を維持。"""
    _patch_build_bundle(monkeypatch)
    bundle, applied = az._decide_selection_bundle(
        {"picks": ["win:99", "wide:8-9"]}, CANDS, None, MODEL)
    assert applied is False
    assert bundle is MODEL               # validated バッジを付けずモデル束のまま


def test_cuts_only_backward_compat(monkeypatch):
    """picks 不在 + cuts → 後方互換で cuts を除いた脚で再構築。"""
    _patch_build_bundle(monkeypatch)
    bundle, applied = az._decide_selection_bundle(
        {"picks": None, "cuts": ["win:1"]}, CANDS, None, MODEL)
    assert applied is True
    keys = {(l["bet_type"], tuple(l["key"])) for l in bundle["legs"]}
    assert keys == {("wide", (3, 7)), ("trifecta", (1, 2, 3))}


def test_no_picks_no_cuts_keeps_model_unvalidated(monkeypatch):
    _patch_build_bundle(monkeypatch)
    bundle, applied = az._decide_selection_bundle(
        {"picks": None, "cuts": []}, CANDS, None, MODEL)
    assert applied is False
    assert bundle is MODEL
