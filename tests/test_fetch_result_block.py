"""netkeiba block 中の result fetch が attempt を消費せず、解除後に取得できるテスト。"""
from __future__ import annotations

import src.fetch_result as fr


def test_is_block_failure():
    assert fr._is_block_failure("fetch_html: NetkeibaBlocked: empty body")
    assert fr._is_block_failure("... CloudFront 400 ...")
    assert not fr._is_block_failure("no finish_order in result page (race not yet settled?)")
    assert not fr._is_block_failure("")


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(fr, "_PENDING_LOCK_FILE", tmp_path / "pending.lock")
    monkeypatch.setattr(fr, "RESULTS_DIR", tmp_path / "results")


def test_block_failure_keeps_pending_then_succeeds(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    fr.schedule("R1", "http://x/shutuba", race_start_at=0,
                delay_sec=0, max_attempts=2, retry_interval_sec=60)

    # block 中: 何 tick 回しても attempts を消費せず failed にもならない
    monkeypatch.setattr(fr, "fetch_result_with_reason",
                        lambda url, **k: (None, "fetch_html: NetkeibaBlocked: empty body"))
    for t in range(3):
        fr.process_pending(now_ts=10_000 + t * 1_000_000)  # 各 tick で due になるよう時間を進める
    e = fr._load_pending()[0]
    assert e.status == "pending"
    assert e.attempts == 0  # block は attempt を消費しない

    # 解除後: 通常の result 取得 → success
    monkeypatch.setattr(fr, "fetch_result_with_reason",
                        lambda url, **k: ({"finish_order": [1, 2, 3], "payout": 1000}, ""))
    fr.process_pending(now_ts=99_000_000)
    e = fr._load_pending()[0]
    assert e.status == "success"


def test_genuine_failure_still_terminates(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    fr.schedule("R2", "http://x/shutuba", race_start_at=0,
                delay_sec=0, max_attempts=2, retry_interval_sec=60)
    # block でない失敗 (結果ページが想定外) は従来どおり max_attempts で failed
    monkeypatch.setattr(fr, "fetch_result_with_reason",
                        lambda url, **k: (None, "no finish_order in result page"))
    for t in range(3):
        fr.process_pending(now_ts=10_000 + t * 1_000_000)
    e = fr._load_pending()[0]
    assert e.status == "failed"
    assert e.attempts >= 2
