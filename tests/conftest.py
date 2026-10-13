"""テスト共通: タイム換算モデルの学習データ (live ledger) とモデルファイルを tmp に隔離する。

analyze._save_prediction_snapshot は保存のたびに各馬の過去走を data/cache/time_ledger/ へ追記し
(src/time_model.record_live_runs)、time_eval は data/cache/time_model.json を読む。テストの合成レースが
本物の学習データに混ざったり、手元のモデルの有無でテスト結果が変わったりしないよう全テストで差し替える。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_time_model(tmp_path, monkeypatch, request):
    from src import nar_official as no
    from src import time_model as tm
    monkeypatch.setattr(tm, "LEDGER_DIR", tmp_path / "time_ledger")
    monkeypatch.setattr(tm, "MODEL_PATH", tmp_path / "time_model.json")
    tm._MODEL_CACHE.update({"mtime": None, "model": None})
    # 地方の公式データ (keiba.go.jp DataDownload) のキャッシュも隔離し、テストからは絶対に通信しない。
    #  - KEIBA_NAR_OFFICIAL=0: fetch_* は手元のキャッシュだけ・sync_due は False。環境変数なので
    #    subprocess (例: API の日次再学習が起動する scripts/build_time_model.py) にも効く
    #    (monkeypatch は子プロセスに届かず、本物の data/cache を上書きしかねない)。
    #  - _download も「呼ばれたら記録して失敗」に差し替え、意図しない通信の試行は teardown で落とす。
    #    取得経路を検証するテストは setenv(KEIBA_NAR_OFFICIAL=1) + _download の差し替え、失敗経路を検証する
    #    テストは `@pytest.mark.network_attempt_ok`。
    monkeypatch.setenv(no.ENV_SWITCH, "0")
    monkeypatch.setattr(no, "CACHE_DIR", tmp_path / "nar_official")
    no._INFO_CACHE.clear()
    attempts: list[str] = []

    def _no_network(url, **_kw):
        attempts.append(url)
        raise RuntimeError(f"tests must not hit the network: {url}")

    monkeypatch.setattr(no, "_download", _no_network)
    yield
    tm._MODEL_CACHE.update({"mtime": None, "model": None})
    no._INFO_CACHE.clear()
    if attempts and request.node.get_closest_marker("network_attempt_ok") is None:
        pytest.fail(f"unexpected network attempt(s) to keiba.go.jp DataDownload: {attempts}")


def pytest_configure(config):
    config.addinivalue_line("markers", "network_attempt_ok: 通信の試行 (conftest の stub が失敗させる) を許すテスト")
