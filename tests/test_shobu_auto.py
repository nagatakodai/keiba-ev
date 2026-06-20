"""勝負レース 自動スキャン (ShobuAutoManager) + build_shobu_cmd の test。

configure() の永続化は monkeypatch で no-op にして実ファイル (ユーザ設定) を汚さない。
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
    # edge-min-count は廃止済 (順位乖離スコアに置換)
    assert "--edge-min-count" not in s


def test_shobu_auto_configure_clamps_and_filters(monkeypatch):
    import api.main as m
    from api.runner import JobRegistry
    monkeypatch.setattr(m, "save_shobu_auto", lambda *_a, **_k: None)  # 実設定を汚さない
    mgr = m.ShobuAutoManager(JobRegistry())
    mgr.configure(enabled=False, interval_sec=10,
                  options={"race_type": "banei", "claude_all": False, "bogus": 123})
    st = mgr.status()
    assert st["enabled"] is False
    assert st["interval_sec"] == 60                # 60 未満は 60 にクランプ
    assert st["options"]["race_type"] == "banei"
    assert st["options"]["claude_all"] is False
    assert "bogus" not in st["options"]            # 未知キーは除外
    assert st["options"]["combine"] == "or"        # 既定キーは保持
    assert st["loop_running"] is False             # start() 前なのでループ未起動

    mgr.configure(interval_sec=5000)
    assert mgr.status()["interval_sec"] == 3600    # 上限クランプ
