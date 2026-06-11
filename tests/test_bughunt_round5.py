"""bughunt 第5ラウンドの回帰テスト (2026-06-11)。

対象: 重賞表記 (ASCII ローマ数字) / JRA 短距離タイム / ばんえい Data06 /
parse_trifecta_multi の per-html fallback / keibago refund のレース区切り /
validate_claude_value の fundamental 逆算 / market_signal の複勝 de-vig /
fire cooldown の窓内導出 / _recently_failed の phase フィルタ。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- 重賞表記 (ASCII GI/GII/GIII, NAR 重賞) ----------

def test_class_index_ascii_roman_grades():
    from src.speed_index import class_index
    assert class_index("東京優駿GI") == 20
    assert class_index("AJCCGII") == 17
    assert class_index("鳴尾記念GIII") == 14
    # JBC2歳JpnIII が NAR C2 (-8) に誤判定されていた regression
    assert class_index("JBC2歳JpnIII") == 14
    assert class_index("かしわ記念JpnI") == 20
    # NAR 地方重賞 = Listed 相当
    assert class_index("園田金盃重賞") == 11
    # 既存表記の互換
    assert class_index("G1") == 20
    assert class_index("GⅢ") == 14
    assert class_index("SAGA(C2)") == -8


def test_aptitude_graded_weight_ascii():
    from src.aptitude import _graded_weight, _normalize_grade_tag
    assert _graded_weight("東京優駿GI") == 10.0
    assert _graded_weight("日経新春杯GII") == 5.0
    assert _graded_weight("JBC2歳JpnIII") == 3.0
    assert _graded_weight("園田金盃重賞") == 2.0
    assert _normalize_grade_tag("鳴尾記念GIII") == "G3"
    assert _normalize_grade_tag("東京優駿GI") == "G1"
    # Listed > G1 の逆転が起きないこと
    assert _graded_weight("東京優駿GI") > _graded_weight("(L)")


# ---------- JRA 短距離タイム (コロン無し秒表記) ----------

def test_parse_jra_time_plain_seconds():
    from src.scrape_jra import _parse_jra_time
    assert _parse_jra_time("57.8") == 57.8
    assert _parse_jra_time("1:12.0") == 72.0
    assert _parse_jra_time("") == 0.0
    assert _parse_jra_time("中止") == 0.0


# ---------- ばんえい Data06 (通過順なし・4桁馬体重) ----------

def test_past_body_weight_banei_4digit():
    from src.parse import _PAST_BODY_RE
    m = _PAST_BODY_RE.search("1048(+13)")
    assert m and int(m.group(1)) == 1048 and int(m.group(2)) == 13
    # 平地形式 (末尾の馬体重のみマッチ、上3Fの括弧と混同しない)
    m2 = _PAST_BODY_RE.search("7-5\xa0(37.2)\xa0479(-3)")
    assert m2 and int(m2.group(1)) == 479 and int(m2.group(2)) == -3


def test_past_run_banei_no_passing():
    """ばんえい Data06 = 馬体重のみ → passing を誤格納しない。"""
    from bs4 import BeautifulSoup
    from src.parse import _parse_one_past_run
    html = ('<div class="Data_Item"><div class="Data01"><span>2026.04.11 帯広</span>'
            '<span class="Num">8</span></div>'
            '<div class="Data05">ダ200 良</div>'
            '<div class="Data06">1048(+13)</div></div>')
    d_item = BeautifulSoup(html, "lxml").select_one(".Data_Item")
    run = _parse_one_past_run(d_item, ranking_cls=[])
    assert run is not None
    assert run.passing == ""
    assert run.body_weight == 1048
    assert run.body_weight_diff == 13


# ---------- parse_trifecta_multi: fallback が html ごとに評価される ----------

def test_trifecta_multi_fallback_per_html():
    from src.parse import parse_trifecta_multi
    # 旧 embedded-JSON 形式の 2 jiku 分 — グローバルゲートだと 2 html 目が欠落する
    h1 = '<script>{"1-2-3": "12.3", "1-3-2": "45.6"}</script>'
    h2 = '<script>{"2-1-3": "78.9", "2-3-1": "10.1"}</script>'
    out = parse_trifecta_multi([h1, h2])
    keys = {t.key for t in out}
    assert (1, 2, 3) in keys and (2, 1, 3) in keys, f"keys={keys}"
    assert len(keys) == 4


# ---------- keibago refund のレース区切り ----------

def test_refund_segment_for_race():
    from src.scrape_keibago import parse_keibago_result
    racemark = ("<table><tr><th>着順</th><th>馬番</th></tr>"
                "<tr><td>1</td><td>1</td></tr><tr><td>2</td><td>2</td></tr>"
                "<tr><td>3</td><td>3</td></tr></table>")
    # レース1 と レース2 が同一組番 1-2-3 で決着 — race_no で正しい方を取る
    refund = (
        '<a href="/KeibaWeb/TodayRaceInfo/RaceMarkTable?k_raceDate=x&k_raceNo=1&k_babaCode=20">a</a>'
        "<table><tr><td>三連単</td><td>1-2-3</td><td>1,000円</td></tr></table>"
        '<a href="/KeibaWeb/TodayRaceInfo/RaceMarkTable?k_raceDate=x&k_raceNo=2&k_babaCode=20">b</a>'
        "<table><tr><td>三連単</td><td>1-2-3</td><td>2,000円</td></tr></table>"
    )
    assert parse_keibago_result(racemark, refund, race_no=1)["payout"] == 1000
    assert parse_keibago_result(racemark, refund, race_no=2)["payout"] == 2000
    # race_no 無し (旧挙動) は flat 先勝ち
    assert parse_keibago_result(racemark, refund)["payout"] == 1000


# ---------- validate_claude_value: fundamental 逆算の round-trip ----------

def test_recover_fundamental_roundtrip_varying_support():
    import src.ev as E
    from scripts.validate_claude_value import _recover_fundamental, _strength_softmax, _normalize
    f = _normalize({1: .30, 2: .25, 3: .15, 4: .12, 5: .10, 6: .08})
    claude = {1: 80.0, 2: 65.0, 3: 90.0}
    support = {1: 3, 2: 1, 3: 5}     # per-horse w が可変 (旧実装が歪むケース)
    blend = 0.5
    keys = list(f)
    L = _strength_softmax(claude, keys, 1e-9)
    logc = {}
    for k in keys:
        w = blend * E._support_mult(support.get(k)) if k in claude else 0.0
        logc[k] = (1 - w) * math.log(f[k]) + w * math.log(max(L.get(k, 1e-12), 1e-12))
    mx = max(logc.values())
    stored = _normalize({k: math.exp(v - mx) for k, v in logc.items()})
    rec = _recover_fundamental(stored, claude, blend, support, 1e-9)
    assert max(abs(rec[k] - f[k]) for k in keys) < 1e-9


# ---------- market_signal: 複勝 de-vig (target=ポジション数) + 頭数ルール ----------

def test_market_signal_place_devig_engages():
    from src.market_signal import _power_method_overround
    raw = [0.55, 0.45, 0.40, 0.35, 0.30, 0.28, 0.25, 0.22, 0.20, 0.18, 0.12, 0.10]
    out = _power_method_overround(raw, target=3.0)
    assert abs(sum(out) - 3.0) < 1e-3
    # power method が実際に効く (比例配分でなく favorite-longshot 補正方向)
    assert out[0] / out[-1] > raw[0] / raw[-1]


def test_market_signal_small_field_positions():
    from src.models import BetOdds, Horse, Race, RaceData
    from src.market_signal import compute_market_signals
    horses = [Horse(number=i, name=f"h{i}", win_odds=2.0 + i) for i in range(1, 7)]  # 6頭
    race = Race(cup_id="x", schedule_index=1, race_number=1, venue_id=0,
                venue_name="t", race_class="", distance=1400, surface="ダート",
                horses=horses)
    place = [BetOdds(bet_type="place", key=(i,), odds=1.5 + i * 0.3) for i in range(1, 7)]
    rd = RaceData(race=race, trifecta=[], other_bets={"place": place})
    sigs = compute_market_signals(rd)
    # 6頭 → 複勝2着まで → place_implied の合計 ≈ 2
    total = sum(s.place_implied for s in sigs.values())
    assert abs(total - 2.0) < 1e-3


# ---------- fire cooldown: 発火窓内に再試行スロットがある ----------

def test_fire_cooldown_within_window():
    from src.auto_watch import _fire_cooldown_sec, MIN_FIRE_RUNWAY_SEC
    lead = 150
    cd = _fire_cooldown_sec(lead)
    # 失敗が窓の先頭 (close-150) で即時に出ても、cooldown 明けが破棄ライン
    # (close-MIN_FIRE_RUNWAY) より手前 = 再試行可能
    assert cd < lead - MIN_FIRE_RUNWAY_SEC
    assert cd >= 15


# ---------- _recently_failed: phase フィルタ ----------

def test_recently_failed_phase_filter(tmp_path, monkeypatch):
    import src.auto_watch as aw
    hist = tmp_path / "history.jsonl"
    now = 1_000_000
    rec = {"race_id": "X", "phase": "score", "rc": 1, "finished_at": now - 10}
    hist.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr(aw, "HISTORY_FILE", hist)
    # score 失敗は phase="bet" の cooldown 判定をブロックしない
    assert aw._recently_failed("X", now, cooldown_sec=120, phase="bet") is False
    assert aw._recently_failed("X", now, cooldown_sec=120, phase="score") is True
    # phase 未指定は従来挙動 (全 phase 対象)
    assert aw._recently_failed("X", now, cooldown_sec=120) is True
