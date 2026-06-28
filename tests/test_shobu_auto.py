"""build_shobu_cmd フラグ + 結果自動取得ループ (ResultAutoFetcher) の test。

副作用 (pending queue 書き込み) は monkeypatch で遮断し、実データを汚さない。
"""
from __future__ import annotations


def test_build_shobu_cmd_flags():
    from api.runner import build_shobu_cmd
    cmd = build_shobu_cmd(
        "/tmp/out.json", date="20260620", race_type="banei",
        use_separation=False, use_claude_edge=True, combine="and",
        sep_threshold=40, edge_margin=5, edge_threshold=30,
        upcoming_only=False, fetch_odds=False, claude_all=True, max_races=7)
    s = " ".join(cmd)
    assert "src.shobu" in s
    assert "--race-type banei" in s
    assert "--edge-threshold 30" in s
    assert "--edge-margin 5" in s
    assert "--no-separation" in s
    assert "--include-finished" in s
    assert "--no-fetch-odds" in s
    assert "--claude-all" in s
    assert "--max-races 7" in s
    assert "--edge-min-count" not in s   # 廃止済 (順位乖離スコアに置換)


def test_results_auto_status_shape():
    import api.main as m
    f = m.ResultAutoFetcher()
    st = f.status()
    assert set(st) >= {"interval_sec", "loop_running", "last_run_at",
                       "next_run_at", "runs", "last_summary"}
    assert st["loop_running"] is False     # start() 前
    assert st["runs"] == 0
    assert st["interval_sec"] >= 60


def test_results_auto_enqueue_filters(monkeypatch):
    """発走済・結果未取得 の予測を **日付不問で** schedule する (ユーザ指示 2026-06-28)。

    本日分のみ → 全レースに拡大。未発走 / 結果あり は引き続き skip。terminal failed を
    復活させないよう schedule(..., resurrect_failed=False) を渡すことも検証。
    """
    import datetime
    import time as _t
    from zoneinfo import ZoneInfo
    import api.main as m

    today = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    now = int(_t.time())
    items = [
        # 対象: JRA 発走済・本日・結果なし
        {"race_id": "20260501-1-2", "start_at": now - 3600, "has_result": False,
         "saved_at": f"{today}T10:00:00"},
        # skip: 結果あり
        {"race_id": "2026320601-601-1", "start_at": now - 3600, "has_result": True,
         "saved_at": f"{today}T10:00:00"},
        # skip: 未発走
        {"race_id": "20260501-1-3", "start_at": now + 3600, "has_result": False,
         "saved_at": f"{today}T10:00:00"},
        # 対象: 別日でも発走済・結果なしなら enqueue (今は本日縛りなし)
        {"race_id": "20260501-1-4", "start_at": now - 3600, "has_result": False,
         "saved_at": "2020-01-01T10:00:00"},
    ]
    monkeypatch.setattr(m, "list_predictions", lambda limit=5000: items)
    scheduled: list = []
    monkeypatch.setattr("src.fetch_result.schedule",
                        lambda rid, url, sa, **k: scheduled.append((rid, url, sa, k)))

    n = m.ResultAutoFetcher._enqueue_finished()
    assert n == 2                                       # 本日 + 別日 の発走済2件
    rids = {s[0] for s in scheduled}
    assert rids == {"20260501-1-2", "20260501-1-4"}    # 結果あり/未発走は除外
    first = next(s for s in scheduled if s[0] == "20260501-1-2")
    assert "race_id=202605010102" in first[1]           # 内部id→netkeiba rid 復元
    assert "race.netkeiba.com" in first[1]              # JRA host (場 01-10)
    assert all(s[3].get("resurrect_failed") is False for s in scheduled)  # 無限リトライ防止
