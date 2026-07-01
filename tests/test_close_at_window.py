"""締切=発走2分前固定 + レース検索が締切基準で動くことを検証。

`parse.close_at_for_start` の規約 (start_at - 120 で固定) と、`auto_watch._list_due_races`
が close_at - now を delta_sec として使う (= --window/--tolerance が締切基準) ことを担保。
"""
from __future__ import annotations

from src.parse import CLOSE_LEAD_SEC, close_at_for_start
from src import auto_watch


def test_close_at_for_start_fixed_120s_before():
    """締切は発走の 120 秒前で固定。未確定 (0) はそのまま 0。"""
    assert CLOSE_LEAD_SEC == 120
    assert close_at_for_start(1_700_000_000) == 1_700_000_000 - 120
    assert close_at_for_start(0) == 0          # 未確定はそのまま
    assert close_at_for_start(-100) == 0       # 負値は 0 (防御)
    # 120 秒未満は max(0, ...) で 0
    assert close_at_for_start(60) == 0


def test_list_due_races_uses_close_at_window(monkeypatch):
    """`--window 5 --tolerance 2` 指定で「締切5〜7分前」のレースが検出される
    (=「発走7〜9分前」相当、+2分のリード補正)。"""
    now = 1_700_000_000
    # 発走 ts: 比較したい時間軸の race を並べる
    # 発走まで         | 締切まで (=発走-120秒)
    # 5 分前   = 300s | 締切 3 分前 (180s) → window 5〜7 圏外 (発走基準では入る、新基準では入らない)
    # 7 分前   = 420s | 締切 5 分前 (300s) → window 5〜7 圏内 (低端)
    # 9 分前   = 540s | 締切 7 分前 (420s) → window 5〜7 圏内 (高端)
    # 11 分前  = 660s | 締切 9 分前 (540s) → window 5〜7 圏外
    races = [
        {"race_id": f"2026{i:08d}", "url": "u", "start_at": now + s,
         "venue": "笠松", "race_no": i, "source": "test"}
        for i, s in enumerate([300, 420, 540, 660], 1)
    ]
    # discovery は公式ソースのみ (netkeiba live は使わない)。NAR=oddspark の当日 race list を
    # mock し、JRA=keibabook discovery は空にしてテストを hermetic に保つ。
    # fetch_race_list_oddspark は dict list を返す ([{"netkeiba_race_id", "url", "start_at",
    # "venue", "race_no"}, ...])。_list_due_races がこれを netkeiba 形式 races に変換する。
    op_races = [{"netkeiba_race_id": r["race_id"], "url": r["url"],
                 "start_at": r["start_at"], "venue": r["venue"], "race_no": r["race_no"]}
                for r in races]
    monkeypatch.setattr(auto_watch, "fetch_race_list_oddspark", lambda *a, **k: op_races)
    monkeypatch.setattr(auto_watch, "fetch_race_list_keibabook", lambda *a, **k: [])
    # keiba.go.jp の南関東/門別 discovery (2026-06-30 追加) も mock して hermetic に保つ
    # (関数内 import なので auto_watch でなく scrape_keibago 側を差し替える)。
    monkeypatch.setattr("src.scrape_keibago.fetch_race_list_keibago", lambda *a, **k: [])
    out, future_all = auto_watch._list_due_races(window_min=5, tolerance_min=2, now_ts=now)
    detected = {r["race_no"] for r in out}
    assert detected == {2, 3}, f"5〜7分(締切基準)圏内は 7/9分前(発走基準) のみ、got {detected}"
    # future_all = 締切が未来の当日全レース (bet 予約プリパス用, 2026-06-11)。
    # race1 は締切 3 分前 (未来) なので含まれ、4 レース全て締切未来 → 4 件。
    assert {r["race_no"] for r in future_all} == {1, 2, 3, 4}
    # close_at が start_at - 120 で乗っている
    for r in out:
        assert r["close_at"] == r["start_at"] - CLOSE_LEAD_SEC
        # delta_sec は締切までの秒数
        assert r["delta_sec"] == r["close_at"] - now
