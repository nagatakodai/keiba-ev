"""テスト共通: タイム換算モデルの学習データ (live ledger) とモデルファイルを tmp に隔離する。

analyze._save_prediction_snapshot は保存のたびに各馬の過去走を data/cache/time_ledger/ へ追記し
(src/time_model.record_live_runs)、time_eval は data/cache/time_model.json を読む。テストの合成レースが
本物の学習データに混ざったり、手元のモデルの有無でテスト結果が変わったりしないよう全テストで差し替える。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_time_model(tmp_path, monkeypatch):
    from src import time_model as tm
    monkeypatch.setattr(tm, "LEDGER_DIR", tmp_path / "time_ledger")
    monkeypatch.setattr(tm, "MODEL_PATH", tmp_path / "time_model.json")
    tm._MODEL_CACHE.update({"mtime": None, "model": None})
    yield
    tm._MODEL_CACHE.update({"mtime": None, "model": None})
