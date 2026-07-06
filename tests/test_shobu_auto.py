"""build_shobu_cmd フラグ + 結果自動取得ループ (ResultAutoFetcher) の test。

副作用 (pending queue 書き込み) は monkeypatch で遮断し、実データを汚さない。
"""
from __future__ import annotations


def test_build_shobu_cmd_flags():
    """基準B 単独 (基準A=強弱は廃止 2026-06-28)。sep/combine/fetch_odds フラグは出さない。"""
    from api.runner import build_shobu_cmd
    cmd = build_shobu_cmd(
        "/tmp/out.json", date="20260620", race_type="banei",
        edge_margin=5, edge_threshold=30,
        upcoming_only=False, claude_all=True, max_races=7)
    s = " ".join(cmd)
    assert "src.shobu" in s
    assert "--race-type banei" in s
    assert "--edge-threshold 30" in s
    assert "--edge-margin 5" in s
    assert "--include-finished" in s
    assert "--claude-all" in s
    assert "--max-races 7" in s
    # 廃止済フラグは出さない。
    assert "--separation" not in s
    assert "--no-separation" not in s
    assert "--combine" not in s
    assert "--sep-threshold" not in s
    assert "--no-fetch-odds" not in s
    assert "--no-claude" not in s
    # リサーチ方式 (ARCH-B): 既定は "ab" (レース毎 50/50 A/B, 2026-07-06)。shobu CLI 側の
    # 既定も "ab" になったため**常に明示**で渡す (省略＝agentic ではなくなった)。
    assert "--research ab" in s
    s2 = " ".join(build_shobu_cmd("/tmp/out.json", research="prefetch"))
    assert "--research prefetch" in s2
    s3 = " ".join(build_shobu_cmd("/tmp/out.json", research="agentic"))
    assert "--research agentic" in s3    # 明示 agentic は ab に呑まれず固定される


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


def test_paddock_rescorer_due_window(tmp_path, monkeypatch):
    """ShobuPaddockRescorer._due は **推奨 NAR/JRA で締切5-7分前** のレースだけ返し dedup する。"""
    import json
    import time
    import api.main as m

    monkeypatch.setattr(m, "SHOBU_DIR", tmp_path)
    monkeypatch.setattr(m, "shobu_today_jst", lambda: "20260630")
    now = time.time()
    races = [
        # 推奨・NAR・締切6分前 → due
        {"race_id": "due-1", "netkeiba_race_id": "202644063006", "recommended": True,
         "race_type": "nar", "close_at": now + 360, "start_at": now + 480,
         "venue": "大井", "race_no": 6},
        # 締切10分前 → 遠すぎ
        {"race_id": "far-1", "netkeiba_race_id": "202644063007", "recommended": True,
         "race_type": "nar", "close_at": now + 600, "start_at": now + 720},
        # 締切1分前 → 近すぎ (締切間際は撃たない)
        {"race_id": "near-1", "netkeiba_race_id": "202644063008", "recommended": True,
         "race_type": "nar", "close_at": now + 60, "start_at": now + 180},
        # 非推奨 → skip
        {"race_id": "norec", "netkeiba_race_id": "202644063010", "recommended": False,
         "race_type": "nar", "close_at": now + 360, "start_at": now + 480},
        # ばんえい (nar/jra 以外) → skip
        {"race_id": "banei", "netkeiba_race_id": "202665063001", "recommended": True,
         "race_type": "banei", "close_at": now + 360, "start_at": now + 480},
    ]
    (tmp_path / "20260630.json").write_text(json.dumps({"races": races}), encoding="utf-8")
    rs = m.ShobuPaddockRescorer()
    due = rs._due()
    assert {d["internal"] for d in due} == {"due-1"}
    assert due[0]["netkeiba"] == "202644063006" and due[0]["rtype"] == "nar"
    # 1 度 fire したら window 内で再び返さない (二重撃ち防止)
    rs._fired.add("due-1")
    assert rs._due() == []


def test_nightly_prescanner_due_gate(tmp_path, monkeypatch):
    """夜間プリスキャナ: 発火は「設定時刻以降 × 翌日未実行 × 有効」のときだけ (2026-07-05)。"""
    import datetime
    from zoneinfo import ZoneInfo
    import api.main as m

    monkeypatch.setattr(m.ShobuNightlyPrescanner, "STATE_FILE",
                        tmp_path / "nightly_state.json")
    ps = m.ShobuNightlyPrescanner(m.JOBS)
    ps.enabled = True
    ps.hour = 21
    jst = ZoneInfo("Asia/Tokyo")
    evening = datetime.datetime(2026, 7, 5, 21, 30, tzinfo=jst)
    noon = datetime.datetime(2026, 7, 5, 12, 0, tzinfo=jst)

    assert ps._due(noon) is None                      # 時刻前は発火しない
    assert ps._due(evening) == "20260706"             # 21時以降 → 翌日日付
    # 実行済みガード (state file) があれば同じ翌日に二重発火しない
    (tmp_path / "nightly_state.json").write_text(
        '{"date": "20260706", "job_id": "x"}', encoding="utf-8")
    assert ps._due(evening) is None
    # 別の日の state なら発火する
    (tmp_path / "nightly_state.json").write_text(
        '{"date": "20260705", "job_id": "x"}', encoding="utf-8")
    assert ps._due(evening) == "20260706"
    # 無効化フラグ
    ps.enabled = False
    assert ps._due(evening) is None


def test_nightly_prescanner_status_shape():
    import api.main as m
    st = m.NIGHTLY_PRESCANNER.status()
    assert set(st) >= {"enabled", "hour_jst", "loop_running", "launches",
                       "last_job_id", "last_launched_date", "last_run_at"}
    assert st["loop_running"] is False   # start() 前 (テストは lifespan を通らない)


def test_daily_scanner_due_gate(tmp_path, monkeypatch):
    """当日キャッチアップ: snapshot 被覆 50% までリトライする (2026-07-06 オッズ発売前スキャン事故の修正)。

    初日実機: 09:02 発火 → NAR オッズ発売前で全レース odds_empty → snapshot 0 のまま
    「1日1回」ガードで以後発火せず当日台帳が空に。→ 発火条件を「被覆達成 or 試行上限まで
    間隔リトライ」に変更。
    """
    import datetime
    import json
    import os
    from zoneinfo import ZoneInfo
    import api.main as m

    monkeypatch.setattr(m, "SHOBU_DIR", tmp_path)
    monkeypatch.setattr(m.ShobuDailyCatchupScanner, "STATE_FILE",
                        tmp_path / "daily_scan_state.json")
    sc = m.ShobuDailyCatchupScanner(m.JOBS)
    sc.enabled = True
    sc.hour = 9
    jst = ZoneInfo("Asia/Tokyo")
    morning = datetime.datetime(2026, 7, 6, 9, 30, tzinfo=jst)
    early = datetime.datetime(2026, 7, 6, 8, 0, tzinfo=jst)

    assert sc._due(early) is None                       # 時刻前は発火しない
    assert sc._due(morning) == "20260706"               # ファイル無し → 初回発火

    # 前夜 nightly 産のファイル (mtime 昨夜・snapshot 0) → 再スキャンする
    f = tmp_path / "20260706.json"
    f.write_text(json.dumps(
        {"races": [], "summary": {"evaluated": 36, "with_snapshot": 0}}), encoding="utf-8")
    stale = datetime.datetime(2026, 7, 5, 21, 30, tzinfo=jst).timestamp()
    os.utime(f, (stale, stale))
    assert sc._due(morning) == "20260706"

    # 直近スキャン済 (mtime が retry 間隔内・snapshot 0 = オッズ発売前) → 間隔を空けて待つ
    recent = datetime.datetime(2026, 7, 6, 9, 2, tzinfo=jst).timestamp()
    os.utime(f, (recent, recent))
    (tmp_path / "daily_scan_state.json").write_text(
        '{"date": "20260706", "job_id": "x", "attempts": 1}', encoding="utf-8")
    assert sc._due(morning) is None
    # 間隔経過後は attempts が残っていれば再発火 (= オッズ発売待ちリトライ)
    later = datetime.datetime(2026, 7, 6, 10, 30, tzinfo=jst)
    assert sc._due(later) == "20260706"

    # snapshot 被覆 90% 未満 (24/36=67% = 発売が遅い場が未取得) ではまだ完了しない
    f.write_text(json.dumps(
        {"races": [], "summary": {"evaluated": 36, "with_snapshot": 24}}), encoding="utf-8")
    os.utime(f, (stale, stale))
    assert sc._due(later) == "20260706"
    # 90% 以上で完了 (以後発火しない)
    f.write_text(json.dumps(
        {"races": [], "summary": {"evaluated": 36, "with_snapshot": 34}}), encoding="utf-8")
    os.utime(f, (stale, stale))
    assert sc._due(later) is None

    # 試行上限に達したら発火しない
    f.write_text(json.dumps(
        {"races": [], "summary": {"evaluated": 36, "with_snapshot": 0}}), encoding="utf-8")
    os.utime(f, (stale, stale))
    (tmp_path / "daily_scan_state.json").write_text(
        json.dumps({"date": "20260706", "job_id": "x", "attempts": sc.max_attempts}),
        encoding="utf-8")
    assert sc._due(later) is None
    # 旧形式 state (attempts 無し) は attempts=1 として読む (後方互換)
    (tmp_path / "daily_scan_state.json").write_text(
        '{"date": "20260706", "job_id": "x"}', encoding="utf-8")
    assert sc._due(later) == "20260706"
    # 別の日の state なら attempts=0 扱いで発火する
    (tmp_path / "daily_scan_state.json").write_text(
        '{"date": "20260705", "job_id": "x", "attempts": 8}', encoding="utf-8")
    assert sc._due(later) == "20260706"
    # 無効化フラグ (KEIBA_DAILY_SCAN=0 相当)
    sc.enabled = False
    assert sc._due(later) is None


def test_daily_scanner_waits_for_running_job(tmp_path, monkeypatch):
    """前回の daily スキャン Job が実行中なら次を発火しない。"""
    import datetime
    import json
    from zoneinfo import ZoneInfo
    import api.main as m
    from api.runner import Job

    monkeypatch.setattr(m, "SHOBU_DIR", tmp_path)
    monkeypatch.setattr(m.ShobuDailyCatchupScanner, "STATE_FILE",
                        tmp_path / "daily_scan_state.json")
    sc = m.ShobuDailyCatchupScanner(m.JOBS)
    sc.enabled = True
    sc.hour = 9
    job = m.JOBS.new(label="shobu-daily: test", cmd=["true"])
    job.status = "running"
    try:
        (tmp_path / "daily_scan_state.json").write_text(
            json.dumps({"date": "20260706", "job_id": job.id, "attempts": 1}),
            encoding="utf-8")
        later = datetime.datetime(2026, 7, 6, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        assert sc._due(later) is None
        job.status = "failed"   # 終了すれば (間隔さえ経てば) 再発火できる
        assert sc._due(later) == "20260706"
    finally:
        job.status = "failed"


def test_daily_scanner_status_shape():
    import api.main as m
    st = m.DAILY_SCANNER.status()
    assert set(st) >= {"enabled", "hour_jst", "loop_running", "launches",
                       "last_job_id", "last_launched_date", "last_run_at"}
    assert st["loop_running"] is False   # start() 前 (テストは lifespan を通らない)
    assert 6 <= st["hour_jst"] <= 15     # 既定 9・6-15 クランプ


def test_paddock_rescore_event_jsonl(tmp_path, monkeypatch):
    """パドック再score の発火 1 回 = EVENTS_FILE に 1 行 (rid/date/fired_at/rc/duration_sec/ok)。"""
    import json
    import subprocess
    import api.main as m

    events = tmp_path / "paddock_rescore_events.jsonl"
    monkeypatch.setattr(m.ShobuPaddockRescorer, "EVENTS_FILE", events)
    monkeypatch.setattr(m, "shobu_today_jst", lambda: "20260706")
    race = {"netkeiba": "202644070606", "internal": "20260706-44-6", "rtype": "nar",
            "start_at": 1780000000, "venue": "大井", "race_no": 6}

    # 成功パス: subprocess が rc=0 で終了 → ok=True, rc=0
    class _Proc:
        returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert m.ShobuPaddockRescorer._rescore(race) is True

    # 失敗パス: timeout (例外) → ok=False, rc=None。イベントは失敗でも記録される。
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=240)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert m.ShobuPaddockRescorer._rescore(race) is False

    lines = [json.loads(x) for x in events.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    ok_ev, ng_ev = lines
    assert set(ok_ev) >= {"rid", "netkeiba", "date", "fired_at", "rc", "duration_sec", "ok"}
    assert ok_ev["rid"] == "20260706-44-6" and ok_ev["date"] == "20260706"
    assert ok_ev["rc"] == 0 and ok_ev["ok"] is True
    assert isinstance(ok_ev["duration_sec"], (int, float))
    assert "T" in ok_ev["fired_at"]                      # ISO 形式
    assert ng_ev["rc"] is None and ng_ev["ok"] is False  # timeout は rc 不明で記録


def test_shobu_scan_request_research_default_ab():
    """ShobuScanRequest.research の既定は "ab" (A/B 自動蓄積)。明示 agentic/prefetch も valid。"""
    import api.main as m
    assert m.ShobuScanRequest().research == "ab"
    assert m.ShobuScanRequest(research="agentic").research == "agentic"
    assert m.ShobuScanRequest(research="prefetch").research == "prefetch"
