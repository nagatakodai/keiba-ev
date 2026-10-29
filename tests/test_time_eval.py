"""タイム換算比較 (src/time_model.py + src/time_eval.py, te1)。

ユーザ指示 (2026-10-10): 過去走の走破時計を、当レースと同条件ならそのまま、別距離・別馬場なら当レース
条件に換算して比較する。換算モデルは同じ馬が別条件を走った差から条件効果を推定する固定効果モデル。
"""
from __future__ import annotations

import json
import math
import random

import pytest

from src import time_eval as te_mod
from src import time_model as tm
from src.models import Horse, PastRun, Race, RaceData


# ------------------------------------------------------------ helpers ----

def _race(venue="大井", surface="ダート", distance=1400, weather_text="晴 / 良", horses=None,
          venue_id=44, start_at=0):
    return RaceData(race=Race(cup_id="x", schedule_index=1, race_number=1, venue_id=venue_id,
                              venue_name=venue, race_class="C1", distance=distance, surface=surface,
                              weather_text=weather_text, start_at=start_at, horses=horses or []),
                    trifecta=[])


def _pr(date, venue, surface, distance, going, own, race_class="", finish=None):
    # keibago/JRA 形式: winner_time_sec に自走時計・diff 0
    return PastRun(date=date, venue=venue, surface=surface, distance=distance, going=going,
                   winner_time_sec=own, time_diff_sec=0.0, race_class=race_class, finish_pos=finish)


def _model():
    """手組みの換算モデル: 大井ダート 1200/1400/1600 と 川崎ダート1400、良/稍。"""
    return {
        "version": "te1", "fitted_at": "2026-10-10T00:00:00+09:00", "data_until": "2026-05-28",
        "n_runs": 100, "params": {"day_effects": True},
        "theta": {"ダート|1200|大井": math.log(75.0), "ダート|1400|大井": math.log(89.0),
                  "ダート|1600|大井": math.log(104.0), "ダート|1400|川崎": math.log(90.0),
                  "芝|1400|東京": math.log(82.0)},
        "theta_n": {},
        "gamma": {"nar|ダート|良": 0.0, "nar|ダート|稍": -0.01, "jra|芝|良": 0.0},
        "a_vs": {"大井|ダート": math.log(89.0) - 1.1 * math.log(1400)},
        "b": {"nar|ダート": 1.1, "jra|芝": 1.1},
        "delta": {"大井|20260901|ダート": 0.01},
        "par_win": {"ダート|1400|大井|": [88.0, 50]},
    }


# ------------------------------------------------------------ 正規化 ----

def test_normalizers():
    assert tm.norm_venue("Ｊ函館") == "函館"
    assert tm.norm_venue("帯広(ば)") == "帯広"
    assert tm.norm_venue("名古") == "名古屋"
    assert tm.norm_going("稍重") == "稍" and tm.norm_going("不良") == "不" and tm.norm_going("－") == ""
    assert tm.date_key("2026.3.1") == 20260301 and tm.date_key("2026/03/01") == 20260301
    assert tm.norm_surface("芝", "障害4歳上未勝利") == "障害"     # JRA accessU は障害を芝で返す
    assert tm.norm_surface("ダ") == "ダート"
    assert tm.norm_horse("(地)ドウデュース ") == "ドウデュース"
    assert tm.valid_flat_time("ダート", 1400, 88.0)
    assert not tm.valid_flat_time("ダート", 1400, 52.0)           # 負担重量を時計と誤認した行
    assert not tm.valid_flat_time("障害", 2860, 190.0)


# ------------------------------------------------------------ fit ----

def test_fit_recovers_condition_effects():
    """同じ馬が別距離を走るデータから、距離間の時計比を復元できる (クラス構成に依らない)。"""
    rnd = random.Random(3)
    true = {1200: 75.0, 1400: 89.0, 1600: 104.0}
    rows = []
    for h in range(300):
        ability = rnd.gauss(0.0, 0.01)
        # 強い馬ほど長距離を多く走る (= 条件ごとのクラス構成の偏り) 状況でも比が保たれること
        dists = [1200, 1400] if ability < 0 else [1400, 1600]
        for k in range(4):
            d = dists[k % 2]
            t = true[d] * math.exp(ability + rnd.gauss(0.0, 0.002))
            rows.append({"h": f"h{h}", "d": 20260101 + k, "v": "大井", "s": "ダート", "dist": d,
                         "g": "良", "t": round(t, 2), "wt": 0.0})
    m = tm.fit(rows, day_effects=False)
    r = math.exp(m["theta"]["ダート|1400|大井"] - m["theta"]["ダート|1200|大井"])
    assert r == pytest.approx(89.0 / 75.0, rel=0.004)
    r2 = math.exp(m["theta"]["ダート|1600|大井"] - m["theta"]["ダート|1400|大井"])
    assert r2 == pytest.approx(104.0 / 89.0, rel=0.004)
    json.dumps(m, allow_nan=False)


# ------------------------------------------------------------ evaluate ----

def _horses():
    a = Horse(number=1, name="アルファ", past_runs=[
        _pr("2026.09.20", "大井", "ダート", 1400, "良", 88.5, finish=1),   # 同条件
        _pr("2026.08.30", "大井", "ダート", 1200, "良", 75.0),             # 距離換算
        _pr("2026.08.10", "川崎", "ダート", 1400, "良", 90.5),             # 同距離 (他場)
    ])
    b = Horse(number=2, name="ブラボー", past_runs=[
        _pr("2026.09.21", "大井", "ダート", 1600, "稍重", 104.5),
        _pr("2026.09.01", "大井", "ダート", 1400, "良", 89.9),             # δ=+0.01 の遅い日
        _pr("2026.08.01", "東京", "芝", 1400, "良", 82.5),                 # 芝ダ換算
    ])
    c = Horse(number=3, name="チャーリー", past_runs=[
        _pr("2026.09.15", "大井", "障害", 2600, "良", 170.0),              # 対象外
        _pr("2026.09.01", "シャティン", "芝", 1400, "良", 81.0),            # 海外 → 対象外
        _pr("2026.08.20", "大井", "ダート", 1400, "良", 52.0),              # 時計異常 → 対象外
    ])
    d = Horse(number=4, name="デルタ", absent=True, past_runs=[_pr("2026.09.20", "大井", "ダート", 1400, "良", 85.0)])
    return [a, b, c, d]


def test_evaluate_same_and_converted_runs():
    te = te_mod.evaluate(_race(horses=_horses()), _model())
    assert te["available"] is True and te["version"] == tm.VERSION
    assert {k: te["target"][k] for k in ("venue", "surface", "distance", "going", "going_source", "par_sec", "par_n")} == {
        "venue": "大井", "surface": "ダート", "distance": 1400, "going": "良",
        "going_source": "announced", "par_sec": 88.0, "par_n": 50}
    assert te["target"]["going_effect"]["group"] == "良" and te["target"]["going_effect"]["pct"] == 0.0
    by = {h["number"]: h for h in te["horses"]}
    assert 4 not in by                                   # 取消馬は出さない
    a_runs = by[1]["runs"]
    assert a_runs[0]["kind"] == "same" and a_runs[0]["conv_sec"] == 88.5   # 同条件は生時計のまま
    assert a_runs[1]["kind"] == "distance"
    assert a_runs[1]["conv_sec"] == pytest.approx(round(75.0 * 89.0 / 75.0, 1))   # 1200→1400 換算
    assert a_runs[2]["kind"] == "same_dist"
    assert a_runs[2]["conv_sec"] == pytest.approx(round(90.5 * 89.0 / 90.0, 1))   # 川崎→大井
    assert by[1]["same_n"] == 1 and by[1]["same_best_sec"] == 88.5
    # 開催日の馬場差 (δ=+0.01 = 時計の掛かる日) は換算で速くし、補正量を +秒で出す
    b_runs = by[2]["runs"]
    assert b_runs[1]["day_adj_sec"] > 0 and b_runs[1]["conv_sec"] < 89.9
    assert b_runs[2]["kind"] == "surface"
    # 対象外の走は理由付きで並べるが推定には使わない
    notes = [r["note"] for r in by[3]["runs"]]
    assert notes == ["障害", "海外/不明の場", "時計異常"]
    assert by[3]["est_sec"] is None and by[3]["rank"] is None and by[3]["confidence"] == "none"
    # 順位・差・指数
    ranked = [h for h in te["horses"] if h["rank"]]
    assert [h["rank"] for h in ranked] == [1, 2]
    assert ranked[0]["gap_sec"] == 0.0 and ranked[1]["gap_sec"] > 0
    assert ranked[0]["index"] > 50 > ranked[1]["index"]
    assert te["horses"][-1]["number"] == 3               # 順位なしは末尾
    json.dumps(te, allow_nan=False)                      # NaN が混ざると詳細 API が 500 になる


def test_evaluate_weighted_mean_matches_definition():
    """推定タイム = 重み (新しさ × 種別 × 別距離なら距離の近さと同場) 付き平均。1 頭だけなら中央値寄せは
    自分自身へ (= 重み付き平均のまま)。"""
    m = _model()
    P = te_mod.params()
    te = te_mod.evaluate(_race(horses=_horses()[:1]), m)
    h = te["horses"][0]
    convs = [88.5, 75.0 * 89.0 / 75.0, 90.5 * 89.0 / 90.0]
    ws = [P["RECENCY_DECAY"] ** 0 * P["KIND_W"]["same"],
          P["RECENCY_DECAY"] ** 1 * P["KIND_W"]["distance"]
          * math.exp(-abs(math.log(1400 / 1200)) / P["DIST_SCALE"]) * P["SAME_VENUE_W"],
          P["RECENCY_DECAY"] ** 2 * P["KIND_W"]["same_dist"]]
    want = sum(w * c for w, c in zip(ws, convs)) / sum(ws)
    assert h["est_raw_sec"] == pytest.approx(round(want, 1))
    assert h["est_sec"] == pytest.approx(round(want, 1)) and h["shrink"] == pytest.approx(
        round(P["SHRINK_W0"] / (sum(ws) + P["SHRINK_W0"]), 2))
    assert [r["weight"] for r in h["runs"]] == [pytest.approx(round(w, 3)) for w in ws]
    assert h["evidence"] == pytest.approx(round(sum(ws), 2))


def test_going_assumed_when_not_announced():
    te = te_mod.evaluate(_race(weather_text="", horses=_horses()[:1]), _model())
    assert te["target"]["going"] == "良" and te["target"]["going_source"] == "assumed"
    te2 = te_mod.evaluate(_race(weather_text="曇 / 稍重", horses=_horses()[:1]), _model())
    assert te2["target"]["going"] == "稍" and te2["target"]["going_source"] == "announced"
    # 稍 (γ=-0.01) に換算すると良より速い
    assert te2["horses"][0]["est_sec"] < te["horses"][0]["est_sec"]


@pytest.mark.parametrize("kw,reason", [
    ({"venue": "帯広", "venue_id": 65, "distance": 0, "surface": ""}, "ばんえい"),
    ({"surface": "障害", "distance": 2860}, "障害"),
    ({"distance": 0, "surface": ""}, "不明"),
    ({"venue": "シャティン"}, "換算データ"),
])
def test_unavailable_cases(kw, reason):
    te = te_mod.evaluate(_race(horses=_horses(), **kw), _model())
    assert te["available"] is False and reason in te["reason"] and te["horses"] == []
    json.dumps(te, allow_nan=False)


def test_unavailable_without_model():
    te = te_mod.evaluate(_race(horses=_horses()))   # conftest で MODEL_PATH は空の tmp
    assert te["available"] is False and "make time-model" in te["reason"]


def test_market_independent_and_deterministic():
    hs = _horses()
    te1 = te_mod.evaluate(_race(horses=hs), _model())
    for h in hs:
        h.win_odds = 99.9
    te2 = te_mod.evaluate(_race(horses=hs), _model())
    assert te1 == te2


def test_same_day_runs_are_dropped():
    """当日以降の走は使わない (過去レース再解析で結果が混入しない安全策)。"""
    import datetime as dt
    start = int(dt.datetime(2026, 9, 20, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp())
    te = te_mod.evaluate(_race(horses=_horses()[:1], start_at=start), _model())
    dates = [r["date"] for r in te["horses"][0]["runs"]]
    assert "2026-09-20" not in dates and dates[0] == "2026-08-30"


def test_past_runs_ext_preferred():
    h = Horse(number=1, name="エコー", past_runs=[_pr("2026.09.20", "大井", "ダート", 1400, "良", 88.5)])
    h.past_runs_ext = h.past_runs + [_pr("2026.07.01", "大井", "ダート", 1400, "良", 87.9)]
    te = te_mod.evaluate(_race(horses=[h]), _model())
    assert te["horses"][0]["n_runs"] == 2 and te["horses"][0]["same_best_sec"] == 87.9


def test_time_index_and_cli_print(capsys):
    te = te_mod.evaluate(_race(horses=_horses()), _model())
    idx = te_mod.time_index(te)
    assert set(idx) == {1, 2} and all(0 <= v <= 100 for v in idx.values())
    from rich.console import Console
    te_mod.print_time_eval(te, Console(width=140), detail=True)
    out = capsys.readouterr().out
    assert "タイム換算比較" in out and "アルファ" in out and "🔍" not in out


def test_fmt_time():
    assert te_mod.fmt_time(72.8) == "1:12.8"
    assert te_mod.fmt_time(58.4) == "58.4"
    assert te_mod.fmt_time(59.96) == "1:00.0"
    assert te_mod.fmt_time(None) == "—"


# ------------------------------------------------------------ ledger ----

def test_record_live_runs_once_per_race():
    rd = _race(horses=_horses())
    n1 = tm.record_live_runs(rd, race_key="R-1")
    n2 = tm.record_live_runs(rd, race_key="R-1")
    assert n1 > 0 and n2 == 0
    rows = tm.load_ledger()
    # 障害・海外・時計異常・(取消馬も過去走は有効) は ledger に入らない/入る を確認
    assert all(r["s"] in ("芝", "ダート") for r in rows)
    assert not any(r["t"] == 52.0 for r in rows)
    assert {r["h"] for r in rows} >= {"アルファ", "ブラボー"}


def test_snapshot_carries_time_eval(tmp_path, monkeypatch):
    """analyze の snapshot に time_eval / time_index が載り、過去走が live ledger に貯まる。"""
    from src import analyze as az
    from src import odds_timeline as otl
    monkeypatch.setattr(az, "ROOT", tmp_path)
    monkeypatch.setattr(otl, "TIMELINE_DIR", tmp_path / "odds_timeline", raising=False)
    tm.save_model(_model())
    rd = _race(horses=_horses())
    az._save_prediction_snapshot("T-1-1", rd, [], [], probs=None)
    snap = json.loads((tmp_path / "data" / "predictions" / "T-1-1.json").read_text(encoding="utf-8"))
    assert snap["time_eval"]["available"] is True
    assert set(snap["time_index"]) == {"1", "2"}
    assert len(tm.load_ledger()) > 0
    # 次の保存で過去走が取れなくても (全馬未評価) 前回の評価を引き継ぐ (今回居ない馬は落とす)
    rd2 = _race(horses=[Horse(number=1, name="アルファ"), Horse(number=2, name="ブラボー")])
    az._save_prediction_snapshot("T-1-1", rd2, [], [], probs=None)
    snap2 = json.loads((tmp_path / "data" / "predictions" / "T-1-1.json").read_text(encoding="utf-8"))
    keep = {h["number"]: (h["rank"], h["est_sec"]) for h in snap["time_eval"]["horses"] if h["number"] in (1, 2)}
    assert {h["number"]: (h["rank"], h["est_sec"]) for h in snap2["time_eval"]["horses"]} == keep


# ------------------------------------------------------------ レビュー指摘の回帰テスト (2026-10-10) ----

def _te(horses):
    return te_mod.evaluate(_race(horses=horses), _model())


def test_merge_drops_scratched_and_reranks():
    """前回の評価を引き継ぐときも、後から取り消した馬は落として順位を付け直す。"""
    prev = _te(_horses())
    top = next(h["number"] for h in prev["horses"] if h["rank"] == 1)
    cur = _te([Horse(number=n, name=f"x{n}") for n in (1, 2, 3)])      # 過去走なし = 全馬未評価
    runners = {1, 2, 3} - {top}
    out = te_mod.merge_previous(cur, prev, runners)
    nums = [h["number"] for h in out["horses"]]
    assert top not in nums
    assert [h["rank"] for h in out["horses"] if h["rank"]] == [1]
    assert out["horses"][0]["gap_sec"] == 0.0


def test_merge_carries_over_horses_missing_runs_only_for_same_target():
    full = _te(_horses())
    hs = _horses()
    hs[0].past_runs = []                                                # #1 だけ馬柱取得に失敗
    partial = _te(hs)
    assert next(h for h in partial["horses"] if h["number"] == 1)["rank"] is None
    out = te_mod.merge_previous(partial, full, {1, 2, 3})
    by = {h["number"]: h for h in out["horses"]}
    assert by[1]["rank"] is not None and by[1]["est_sec"] == next(
        h for h in full["horses"] if h["number"] == 1)["est_sec"]
    # 当レース条件 (馬場状態) が変わったら前回の換算時計は混ぜない
    wet = te_mod.evaluate(_race(weather_text="雨 / 稍重", horses=hs), _model())
    out2 = te_mod.merge_previous(wet, full, {1, 2, 3})
    assert next(h for h in out2["horses"] if h["number"] == 1)["rank"] is None


def test_day_adj_none_when_day_not_estimated():
    h = Horse(number=1, name="アルファ", past_runs=[
        _pr("2026.09.01", "大井", "ダート", 1400, "良", 89.9),   # δ あり
        _pr("2026.09.08", "大井", "ダート", 1400, "良", 89.9),   # δ なし (未推定)
    ])
    runs = {r["date"]: r for r in _te([h])["horses"][0]["runs"]}
    assert runs["2026-09-01"]["day_adj_sec"] > 0
    assert runs["2026-09-08"]["day_adj_sec"] is None


@pytest.mark.parametrize("surface,rc,dist,want", [
    ("ダート", "YJSF名2勝", 1400, "ダート"),     # ヤングジョッキーズシリーズ = 平地
    ("芝", "YJSF名1", 2000, "芝"),
    ("ダート", "LJS名(C)", 1400, "ダート"),
    ("芝", "ペガサスJS", 3000, "障害"),
    ("芝", "中山GJ", 4250, "障害"),
    ("芝", "障害4歳上未勝利", 2860, "障害"),
    ("芝", "東京ハイJ", 3110, "障害"),           # accessU の末尾 J 略称 (芝 2750m 以上)
    ("ダート", "次開催は15年ぶりJ", 1800, "ダート"),
    ("ダート", "2025J", 1800, "ダート"),
    ("芝", "ステイヤーズS", 3600, "芝"),
    # ゴールデンジョッキーズシリーズ (地方の平地, 2026-10-13: 公式データで 水沢 2025-12-09 R9 を障害扱いしていた)
    ("ダート", "オッズパークＧＪＳキャンペーン賞Ｂ２", 1400, "ダート"),
    ("ダート", "ＧＪＳ第１戦", 1400, "ダート"),
    ("芝", "中山GJ(J・G1)", 4250, "障害"),
    ("芝", "京都JS", 3170, "障害"),
    ("芝", "中山GJJGI", 4260, "障害"),            # netkeiba 表記 (グレード付き) も障害のまま
    ("芝", "牛若丸JSOP", 3170, "障害"),
])
def test_jump_detection(surface, rc, dist, want):
    assert tm.norm_surface(surface, rc, dist) == want


def test_par_win_counts_each_race():
    """同じ日・同じ条件の別レース (勝ち時計が違う) を 1 レースに潰さない。"""
    rows = []
    for day in range(6):
        for k, wt in enumerate((89.0, 87.0)):
            rows.append({"h": f"h{day}{k}", "d": 20260101 + day, "v": "大井", "s": "ダート", "dist": 1400,
                         "g": "良", "t": wt, "wt": wt})
            rows.append({"h": f"h{day}{k}", "d": 20250101 + day, "v": "大井", "s": "ダート", "dist": 1200,
                         "g": "良", "t": 75.0, "wt": 0.0})
    m = tm.fit(rows)
    assert m["par_win"]["ダート|1400|大井|"] == [88.0, 12]


def test_record_live_runs_per_horse():
    """過去走が取れなかった馬は記録済みにしない (後の保存で取れたら足される)。"""
    hs = _horses()[:2]
    saved = hs[1].past_runs
    hs[1].past_runs = []
    assert tm.record_live_runs(_race(horses=hs), race_key="R-9") > 0
    hs[1].past_runs = saved
    assert tm.record_live_runs(_race(horses=hs), race_key="R-9") > 0     # ブラボー分だけ追加
    assert tm.record_live_runs(_race(horses=hs), race_key="R-9") == 0
    assert {r["h"] for r in tm.load_ledger()} >= {"アルファ", "ブラボー"}


def test_going_effect_falls_back_to_other_circuit():
    m = _model()
    m["gamma"]["jra|ダート|不"] = -0.02
    assert tm.going_effect(m, "大井", "ダート", "不") == -0.02        # 地方に 不 が無ければ中央の値
    assert tm.going_effect(m, "大井", "ダート", "稍") == -0.01
    assert tm.going_effect(m, "大井", "芝", "重") == 0.0               # どちらにも無ければ 良扱い


def test_fit_converges_and_records_iterations():
    rnd = random.Random(5)
    rows = []
    for h in range(80):
        ab = rnd.gauss(0, 0.01)
        for k, d in enumerate((1200, 1400, 1200, 1400)):
            rows.append({"h": f"h{h}", "d": 20260101 + k, "v": "大井", "s": "ダート", "dist": d, "g": "良",
                         "t": round((75.0 if d == 1200 else 89.0) * math.exp(ab + rnd.gauss(0, 0.002)), 2),
                         "wt": 0.0})
    m = tm.fit(rows)
    assert 1 < m["params"]["iterations"] < tm.MAX_ITERS


def test_model_meta_reports_target_circuit_data_until():
    """地方は公式データで当月まで・中央は 2026-05 まで — 表示する「学習データの新しさ」は当レースの circuit の値。"""
    from src import time_eval as te
    m = {"fitted_at": "2026-10-13T04:00:00+09:00", "data_until": "2026-10-12", "n_runs": 1,
         "data_until_by_circuit": {"jra": "2026-05-28", "nar": "2026-10-12"}, "params": {"day_effects": True}}
    assert te._model_meta(m, "東京")["data_until"] == "2026-05-28"
    assert te._model_meta(m, "大井")["data_until"] == "2026-10-12"
    assert te._model_meta({"data_until": "2026-05-28"}, "大井")["data_until"] == "2026-05-28"   # 旧モデル
    assert te._model_meta(None, "大井") is None


# ------------------------------------------------------------ te2 (2026-10-14): 馬場込み同条件・弱い換算・適性 ----

def _model2():
    """場別の馬場差 γ_v 付きモデル: 大井 ダ 渋 (重/不) は良より 1.5% 速い、川崎は +0.5% 遅い。"""
    m = _model()
    m["gamma"] = {"nar|ダート|良": 0.0, "nar|ダート|稍": -0.002, "nar|ダート|重": -0.005, "nar|ダート|不": -0.008}
    m["gamma_venue"] = {"大井|ダート|重": -0.015, "大井|ダート|不": -0.015, "川崎|ダート|重": 0.005}
    m["gamma_venue_n"] = {"大井|ダート|重": 300, "大井|ダート|不": 120, "川崎|ダート|重": 90}
    m["par_win"] = {"ダート|1400|大井|": [88.0, 50], "ダート|1400|大井|重": [87.0, 12]}
    return m


def test_going_group_and_same_condition_includes_going():
    """同条件 (same) は馬場状態 (良/稍/渋) まで同じ走だけ。同コースで馬場が違う走は same_course。"""
    assert tm.going_group("重") == "渋" and tm.going_group("不良") == "渋" and tm.going_group("") == "良"
    h = Horse(number=1, name="A", past_runs=[
        _pr("2026.09.20", "大井", "ダート", 1400, "重", 87.0),     # 当レース 重 → same
        _pr("2026.09.10", "大井", "ダート", 1400, "不良", 87.2),   # 不良も渋 → same
        _pr("2026.08.30", "大井", "ダート", 1400, "良", 88.6),     # 馬場だけ違う → same_course
    ])
    te = te_mod.evaluate(_race(weather_text="雨 / 重", horses=[h]), _model2())
    row = te["horses"][0]
    assert [r["kind"] for r in row["runs"]] == ["same", "same", "same_course"]
    assert row["same_n"] == 2 and row["same_best_sec"] == 87.0 and row["same_best_going"] == "重"
    assert row["course_n"] == 3 and row["course_best_sec"] == 87.0
    # 良の走は 大井 の重 (−1.5%) に換算されて速くなる
    assert row["runs"][2]["conv_sec"] < 88.6
    ge = te["target"]["going_effect"]
    assert ge["going"] == "重" and ge["group"] == "渋" and ge["source"] == "venue" and ge["n"] == 300
    assert ge["pct"] == -1.5 and ge["sec"] == pytest.approx(round(87.0 * (math.exp(-0.015) - 1), 1))


def test_conversion_runs_get_weaker_weights_than_same_condition():
    P = te_mod.params()
    h = Horse(number=1, name="A", past_runs=[
        _pr("2026.09.20", "東京", "芝", 1400, "良", 82.0),        # 芝ダ換算
        _pr("2026.09.10", "大井", "ダート", 1200, "良", 75.0),     # 距離換算
        _pr("2026.08.30", "川崎", "ダート", 1400, "良", 90.0),     # 他場同距離
        _pr("2026.08.20", "大井", "ダート", 1400, "良", 88.5),     # 同条件 (4 走前でも)
    ])
    te = te_mod.evaluate(_race(horses=[h]), _model())
    w = {r["kind"]: r["weight"] for r in te["horses"][0]["runs"]}
    assert w["same"] > w["same_dist"] > w["distance"] > w["surface"]
    assert w["surface"] == pytest.approx(round(P["RECENCY_DECAY"] ** 0 * P["KIND_W"]["surface"], 3))
    assert w["same"] == pytest.approx(round(P["RECENCY_DECAY"] ** 3 * P["KIND_W"]["same"], 3))


def test_weak_evidence_is_shrunk_toward_field_median():
    """同条件の無い (芝ダ換算 1 走だけの) 馬は、証拠が十分な馬の中央値へ寄る = 換算の運だけで 1 位にならない。"""
    strong = []
    for i in range(3):
        strong.append(Horse(number=i + 1, name=f"S{i}", past_runs=[
            _pr("2026.09.20", "大井", "ダート", 1400, "良", 88.0 + 0.3 * i),
            _pr("2026.09.01", "大井", "ダート", 1400, "良", 88.2 + 0.3 * i),
            _pr("2026.08.10", "大井", "ダート", 1400, "良", 88.1 + 0.3 * i)]))
    weak = Horse(number=9, name="W", past_runs=[_pr("2026.09.20", "東京", "芝", 1400, "良", 78.0)])  # 換算 84.6 = 飛び抜けて速い
    te = te_mod.evaluate(_race(horses=strong + [weak]), _model())
    by = {h["number"]: h for h in te["horses"]}
    assert by[9]["est_raw_sec"] < 86.0 and by[9]["confidence"] == "low"
    assert by[9]["shrink"] > 0.5 and by[9]["est_sec"] > by[9]["est_raw_sec"]      # 中央値へ寄った
    assert by[1]["shrink"] < 0.15                                                   # 証拠が十分な馬はほぼ据え置き
    assert te["target"]["prior_sec"] == pytest.approx(by[2]["est_raw_sec"], abs=0.11)  # 寄せ先 = 強い馬の中央値
    # 寄せ無し (W0=0) なら生の重み付き平均そのまま
    saved = te_mod.params()["SHRINK_W0"]
    te_mod.set_params(SHRINK_W0=0.0)
    try:
        te0 = te_mod.evaluate(_race(horses=strong + [weak]), _model())
        assert next(h for h in te0["horses"] if h["number"] == 9)["est_sec"] == by[9]["est_raw_sec"]
    finally:
        te_mod.set_params(SHRINK_W0=saved)
    json.dumps(te, allow_nan=False)


def test_confidence_levels_by_core_evidence():
    same = lambda d, t: _pr(d, "大井", "ダート", 1400, "良", t)  # noqa: E731
    hs = [
        Horse(number=1, name="high", past_runs=[same("2026.09.20", 88.0), same("2026.09.01", 88.2), same("2026.08.10", 88.1)]),
        Horse(number=2, name="mid", past_runs=[same("2026.09.20", 88.0), _pr("2026.09.01", "大井", "ダート", 1200, "良", 75.0)]),
        Horse(number=3, name="low", past_runs=[_pr("2026.09.20", "東京", "芝", 1400, "良", 82.0),
                                             _pr("2026.09.01", "大井", "ダート", 1200, "良", 75.0)]),
        Horse(number=4, name="none", past_runs=[]),
    ]
    te = te_mod.evaluate(_race(horses=hs), _model())
    assert {h["number"]: h["confidence"] for h in te["horses"]} == {1: "high", 2: "mid", 3: "low", 4: "none"}


def test_fit_labels_distance_surface_going():
    """距離 / 芝ダ / 馬場 の適性 = 近い条件の走と それ以外の走の換算時計の差 (負 = 得意)。"""
    h = Horse(number=1, name="A", past_runs=[
        _pr("2026.09.20", "大井", "ダート", 1400, "重", 86.0, finish=1),    # 当条件 (重) で速い
        _pr("2026.09.01", "大井", "ダート", 1400, "良", 89.5),             # 良では遅い
        _pr("2026.08.20", "大井", "ダート", 1600, "良", 104.0),            # 別距離
        _pr("2026.08.01", "東京", "芝", 1400, "良", 82.0),                 # 芝
    ])
    te = te_mod.evaluate(_race(weather_text="雨 / 重", horses=[h]), _model2())
    f = te["horses"][0]["fit"]
    assert f["going"]["n_like"] == 1 and f["going"]["n_other"] == 2          # 馬場は同じ芝ダ (ダート) の走だけで比べる
    # 1 走 vs 2 走の −1.5 秒は誤差 (±1.2 秒) の範囲 → 普通 (2026-10-29: 得意/不安 は z ≥ FIT_Z のときだけ)
    assert f["going"]["delta_sec"] < -0.3 and f["going"]["top3_like"] == 1
    assert f["going"]["se_sec"] > 1.0 and abs(f["going"]["z"]) < te_mod.FIT_Z and f["going"]["label"] == "普通"
    assert f["distance"]["n_like"] == 3 and f["distance"]["n_other"] == 1    # ±10% = 1400 のみ (1600 は外)
    assert f["surface"]["n_like"] == 3 and f["surface"]["n_other"] == 1
    assert f["surface"]["label"] in ("得意", "普通", "不安")
    # 未経験 / 実績のみ
    h2 = Horse(number=2, name="B", past_runs=[_pr("2026.09.20", "東京", "芝", 1400, "良", 82.0)])
    f2 = te_mod.evaluate(_race(horses=[h2]), _model2())["horses"][0]["fit"]
    assert f2["surface"]["label"] == "未経験" and f2["going"]["label"] == "—"     # 同じ芝ダの走が無い
    h3 = Horse(number=3, name="C", past_runs=[_pr("2026.09.20", "大井", "ダート", 1400, "良", 88.0)])
    f3 = te_mod.evaluate(_race(horses=[h3]), _model2())["horses"][0]["fit"]
    assert f3["distance"]["label"] == f3["surface"]["label"] == f3["going"]["label"] == "実績のみ"


def test_merge_ignores_display_only_target_fields():
    """par (標準勝ち時計) が再学習で変わっても、条件が同じなら前回の評価を引き継ぐ。"""
    full = _te(_horses())
    hs = _horses()
    hs[0].past_runs = []
    partial = _te(hs)
    partial["target"]["par_sec"] = 87.5
    partial["target"]["going_effect"] = {"going": "良", "group": "良", "pct": 0.0, "sec": 0.0, "source": "venue", "n": 999}
    out = te_mod.merge_previous(partial, full, {1, 2, 3})
    assert next(h for h in out["horses"] if h["number"] == 1)["rank"] is not None


def test_fit_learns_venue_level_going_effects():
    """同じ馬が同じ場で 良 と 重 を走った差から、場ごとに向きの違う馬場差を復元する (大井 速い / 船橋 遅い)。"""
    rnd = random.Random(7)
    truth = {"大井": -0.02, "船橋": +0.01}
    rows = []
    for v, eff in truth.items():
        for h in range(150):
            ab = rnd.gauss(0.0, 0.01)
            for k in range(6):
                g = "重" if k % 2 else "良"
                t = 89.0 * math.exp(ab + (eff if g == "重" else 0.0) + rnd.gauss(0.0, 0.002))
                rows.append({"h": f"{v}{h}", "d": 20260101 + k * 3, "v": v, "s": "ダート", "dist": 1400,
                             "g": g, "t": round(t, 2), "wt": 0.0})
    m = tm.fit(rows)
    assert m["gamma_venue"]["大井|ダート|重"] == pytest.approx(-0.02, abs=0.002)
    assert m["gamma_venue"]["船橋|ダート|重"] == pytest.approx(+0.01, abs=0.002)
    assert m["gamma_venue"]["大井|ダート|良"] == 0.0
    # δ (開催日差) に馬場状態の効果が残らない (再中心化)
    import statistics as st
    d_heavy = [m["delta"].get(f"大井|{20260101 + k * 3}|ダート", 0.0) for k in range(6) if k % 2]
    assert abs(st.mean(d_heavy)) < 0.002
    # 旧モデル (場別なし) は 2 場の平均に潰れる
    tm.VENUE_GOING = False
    try:
        m_old = tm.fit(rows)
    finally:
        tm.VENUE_GOING = True
    assert m_old["gamma_venue"] == {} and -0.012 < m_old["gamma"]["nar|ダート|重"] < 0.0
    assert tm.going_effect(m, "大井", "ダート", "重") == pytest.approx(-0.02, abs=0.002)
    assert tm.going_effect(m_old, "大井", "ダート", "重") == m_old["gamma"]["nar|ダート|重"]


def test_going_effect_detail_fallback_order():
    m = _model2()
    assert tm.going_effect_detail(m, "大井", "ダート", "重") == (-0.015, "venue", 300)
    assert tm.going_effect_detail(m, "船橋", "ダート", "重") == (-0.005, "circuit", 0)      # 場別が無ければ全体
    m["gamma"]["jra|ダート|不"] = -0.02
    del m["gamma"]["nar|ダート|不"]
    assert tm.going_effect_detail(m, "船橋", "ダート", "不") == (-0.02, "other", 0)
    assert tm.going_effect_detail(m, "大井", "芝", "重")[1] == "none"
    assert tm.going_effect_detail(m, "大井", "ダート", "良")[0] == 0.0


def test_set_params_rejects_unknown():
    with pytest.raises(KeyError):
        te_mod.set_params(NOPE=1)


# ------------------------------------------------------------ te2.1 (2026-10-29): te2 レビュー指摘の回帰テスト ----

@pytest.fixture
def restore_presets():
    """use_preset / 定数差し替えのテストが他のテストへ漏れないように、集約パラメータと換算モデルの定数を戻す。"""
    saved = te_mod.params()
    saved_tm = {k: getattr(tm, k) for k in ("VENUE_GOING", "K_GOING", "GOING_FIT", "MAX_ITERS")}
    yield
    te_mod.set_params(**saved)
    for k, v in saved_tm.items():
        setattr(tm, k, v)


def test_fit_labels_need_z_not_just_03_sec():
    """適性ラベルは差 0.3 秒だけでは付けない: 同じ馬の換算時計は走ごとに ±1% ばらつくので、走数が少ない差は 普通。"""
    few = Horse(number=1, name="A", past_runs=[
        _pr("2026.09.20", "大井", "ダート", 1400, "重", 88.0),            # 当条件 1 走
        _pr("2026.09.05", "大井", "ダート", 1400, "良", 89.9),            # 別馬場 1 走 (換算で −1.5% → ≈ 88.6)
    ])
    g = te_mod.evaluate(_race(weather_text="雨 / 重", horses=[few]), _model2())["horses"][0]["fit"]["going"]
    assert g["n_like"] == 1 and g["n_other"] == 1 and g["delta_sec"] is not None and g["delta_sec"] < 0
    assert g["delta_sec"] <= -0.3                                       # 旧判定 (±0.3 秒だけ) なら 得意 だった差
    assert g["se_sec"] == pytest.approx(0.011 * 88.3 * math.sqrt(2), abs=0.06) and g["label"] == "普通"
    # 走数が揃って差がはっきりしていれば 得意
    many = Horse(number=2, name="B", past_runs=[
        *[_pr(f"2026.09.{20 - k:02d}", "大井", "ダート", 1400, "重", 86.0 + 0.1 * k) for k in range(4)],
        *[_pr(f"2026.08.{20 - k:02d}", "大井", "ダート", 1400, "良", 90.5 + 0.1 * k) for k in range(4)],
    ])
    g2 = te_mod.evaluate(_race(weather_text="雨 / 重", horses=[many]), _model2())["horses"][0]["fit"]["going"]
    assert g2["n_like"] == 4 and g2["n_other"] == 4
    assert g2["z"] <= -te_mod.FIT_Z and g2["label"] == "得意"
    # te2 の判定 (z なし) に戻すと 1 走 vs 1 走でもラベルが付く (= 旧挙動)
    saved = te_mod.params()
    te_mod.set_params(FIT_Z=0.0)
    try:
        g0 = te_mod.evaluate(_race(weather_text="雨 / 重", horses=[few]), _model2())["horses"][0]["fit"]["going"]
    finally:
        te_mod.set_params(**saved)
    assert g0["label"] == "得意"


def _one_day_heavy_rows(seed=13, n_heavy=10, eff=-0.03):
    """大井 (良 6 日 × 120 頭) と 船橋 (良 + 重 の日が多い) に、水沢 の 重 が 1 日だけ (n_heavy 走) あるデータ。"""
    rnd = random.Random(seed)
    rows = []
    day_eff = {}

    def de(v, d):
        return day_eff.setdefault((v, d), rnd.gauss(0, 0.002))

    for h in range(120):
        ab = rnd.gauss(0, 0.01)
        for k in range(6):
            g = "重" if (k % 3 == 2) else "良"
            d = 20260101 + k
            t = 89.0 * math.exp(ab + (0.004 if g == "重" else 0.0) + de("船橋", d) + rnd.gauss(0, 0.002))
            rows.append({"h": f"F{h}", "d": d, "v": "船橋", "s": "ダート", "dist": 1400, "g": g, "t": round(t, 2), "wt": 0.0})
            # 同じ馬が 水沢 の良も走る (場同士がつながる)
            d2 = 20260201 + k
            t2 = 90.0 * math.exp(ab + de("水沢", d2) + rnd.gauss(0, 0.002))
            rows.append({"h": f"F{h}", "d": d2, "v": "水沢", "s": "ダート", "dist": 1400, "g": "良", "t": round(t2, 2), "wt": 0.0})
    for h in range(n_heavy):                       # 水沢 の 重 は 1 日・n_heavy 走だけ
        ab = rnd.gauss(0, 0.01)
        for k in range(3):
            d = 20260201 + k
            rows.append({"h": f"M{h}", "d": d, "v": "水沢", "s": "ダート", "dist": 1400, "g": "良",
                         "t": round(90.0 * math.exp(ab + de("水沢", d) + rnd.gauss(0, 0.002)), 2), "wt": 0.0})
        d = 20260220
        rows.append({"h": f"M{h}", "d": d, "v": "水沢", "s": "ダート", "dist": 1400, "g": "重",
                     "t": round(90.0 * math.exp(ab + eff + de("水沢", d) + rnd.gauss(0, 0.002)), 2), "wt": 0.0})
    return rows


def test_small_going_cell_is_shrunk_toward_circuit_as_designed(restore_presets):
    """1 日 10 走しかない場別の馬場セルは、走数 n と K_GOING の比で全体の γ へ寄る (n/(n+K) だけ生の差が残る)。
    te2 (GOING_FIT=transfer) は δ のセル平均を γ_v へ丸ごと移すので縮約が打ち消され、生の差のままだった。"""
    rows = _one_day_heavy_rows()
    m = tm.fit(rows)
    prior = m["gamma"]["nar|ダート|重"]
    n = m["gamma_venue_n"]["水沢|ダート|重"]
    assert n == 10 and m["params"]["going_fit"] == "shrink" and m["params"]["converged"]
    w = n / (n + tm.K_GOING)
    gv = m["gamma_venue"]["水沢|ダート|重"]
    # 生の差 −3% を n/(n+K) = 25% だけ残して全体の γ へ (日の偶然 ±0.2% ぶんの許容)
    assert gv == pytest.approx(prior + w * (-0.03 - prior), abs=0.003)
    tm.GOING_FIT = "transfer"
    m_t = tm.fit(rows)
    assert m_t["params"]["going_fit"] == "transfer"
    gv_t = m_t["gamma_venue"]["水沢|ダート|重"]
    assert gv_t < gv - 0.01                                            # 旧推定は縮約されず生の差に近い
    # 縮約で γ_v に入らなかった分はその日の δ が持つ = γ_v + δ の合計 (学習データ上の当てはめ) は旧推定と同じ
    total = gv + m["delta"]["水沢|20260220|ダート"]
    assert total == pytest.approx(gv_t + m_t["delta"].get("水沢|20260220|ダート", 0.0), abs=0.002)


def test_fit_converges_when_a_venue_shares_no_horses(restore_presets):
    """他の場と馬を共有しない場があっても θ が漂流せず収束する (te2 = GOING_FIT=transfer は θ へ水準を移す量が毎回
    同じで、反復回数に比例して θ が動き MAX_ITERS まで回っていた)。共有馬が無い場の水準はデータで決まらないので、
    初期値 (各コースの平均時計) のまま止まるのが正しい。"""
    rnd = random.Random(21)
    rows = []
    day = {}
    de = lambda v, d: day.setdefault((v, d), rnd.gauss(0, 0.002))  # noqa: E731
    for h in range(150):                                   # 大井: 良と重を交互に 10 日
        ab = rnd.gauss(0, 0.01)
        for k in range(10):
            g, d = ("重" if k % 2 else "良"), 20260101 + k
            rows.append({"h": f"O{h}", "d": d, "v": "大井", "s": "ダート", "dist": 1400, "g": g, "wt": 0.0,
                         "t": round(89.0 * math.exp(ab + (0.007 if g == "重" else 0) + de("大井", d) + rnd.gauss(0, 0.002)), 2)})
    for h in range(100):                                   # 水沢: 良 6 日 + 15 頭だけ 重 の日 (大井とは馬を共有しない)
        ab = rnd.gauss(0, 0.01)
        for k in range(6):
            d = 20260201 + k
            rows.append({"h": f"M{h}", "d": d, "v": "水沢", "s": "ダート", "dist": 1400, "g": "良", "wt": 0.0,
                         "t": round(90.0 * math.exp(ab + de("水沢", d) + rnd.gauss(0, 0.002)), 2)})
        if h < 15:
            rows.append({"h": f"M{h}", "d": 20260220, "v": "水沢", "s": "ダート", "dist": 1400, "g": "重", "wt": 0.0,
                         "t": round(90.0 * math.exp(ab - 0.03 + de("水沢", 20260220) + rnd.gauss(0, 0.002)), 2)})
    m = tm.fit(rows)
    diff = m["theta"]["ダート|1400|水沢"] - m["theta"]["ダート|1400|大井"]
    assert m["params"]["converged"] and m["params"]["iterations"] < 50
    assert diff == pytest.approx(math.log(90.0 / 89.0), abs=0.003)
    tm.GOING_FIT = "transfer"
    m_t = tm.fit(rows)
    assert not m_t["params"]["converged"] and m_t["params"]["iterations"] == tm.MAX_ITERS
    assert m_t["theta"]["ダート|1400|水沢"] - m_t["theta"]["ダート|1400|大井"] > diff + 0.01   # 漂流した


def test_merge_does_not_mix_versions():
    """版の違う前回 (例 te2 → te2.1、te1 → te2) の馬行は今回の順位付けに混ぜない。丸ごと引き継ぐときは前回の版のまま。"""
    full = _te(_horses())
    hs = _horses()
    hs[0].past_runs = []
    partial = _te(hs)
    old = json.loads(json.dumps(full))
    old["version"] = "te1"
    out = te_mod.merge_previous(partial, old, {1, 2, 3})
    assert next(h for h in out["horses"] if h["number"] == 1)["rank"] is None
    assert out["version"] == tm.VERSION
    # 今回が評価不能なら前回を丸ごと (版は前回のまま)
    none = _te([Horse(number=n, name=f"x{n}") for n in (1, 2, 3)])
    out2 = te_mod.merge_previous(none, old, {1, 2, 3})
    assert out2["version"] == "te1" and any(h["rank"] for h in out2["horses"])


def test_presets_reproduce_registered_versions(restore_presets):
    """事前登録の判定に使う版の設定は凍結 (te1 = 2026-10-13 / te2 = 2026-10-15)。use_preset が集約と換算モデルを揃える。"""
    p1, p2 = te_mod.PRESETS["te1"], te_mod.PRESETS["te2"]
    assert p2["params"]["EST_RUNS"] == 8 and p2["params"]["SHRINK_W0"] == 0.3 and p2["params"]["FIT_Z"] == 0.0
    assert p2["params"]["KIND_W"] == {"same": 1.0, "same_course": 0.8, "same_dist": 0.6, "distance": 0.7, "surface": 0.25}
    assert p2["model"] == {"VENUE_GOING": True, "K_GOING": 30.0, "GOING_FIT": "transfer"}
    assert p1["params"]["EST_RUNS"] == 5 and p1["params"]["SHRINK_W0"] == 0.0 and not p1["model"]["VENUE_GOING"]
    te_mod.use_preset("te1")
    assert te_mod.EST_RUNS == 5 and tm.VENUE_GOING is False
    te_mod.KIND_W["same"] = 9.9                    # 版の定義 (PRESETS) は書き換わらない
    assert te_mod.PRESETS["te1"]["params"]["KIND_W"]["same"] == 1.15
    te_mod.use_preset("te2.1")
    assert te_mod.params() == te_mod.PRESETS["te2.1"]["params"] and tm.GOING_FIT == "shrink" and tm.VENUE_GOING
    with pytest.raises(KeyError):
        te_mod.use_preset("te9")


def test_old_same_condition_runs_are_used_beyond_window(restore_presets):
    """直近 8 走が換算走で埋まっても、9 走目以降の 同条件 の走は (新しさの減衰付きで) 推定に入る = 同条件 列と一致 (te2.1)。"""
    runs = [_pr(f"2026.09.{28 - 2 * k:02d}", "大井", "ダート", 1600, "良", 104.0) for k in range(8)]   # 直近 8 走 = 距離換算
    runs += [_pr("2026.07.20", "大井", "ダート", 1400, "良", 88.0), _pr("2026.07.05", "大井", "ダート", 1400, "良", 88.2)]
    h = Horse(number=1, name="A", past_runs_ext=runs, past_runs=runs[:5])
    row = te_mod.evaluate(_race(horses=[h]), _model())["horses"][0]
    same = [r for r in row["runs"] if r["kind"] == "same"]
    assert row["same_n"] == 2 and len(same) == 2
    assert all(r["weight"] == pytest.approx(round(te_mod.RECENCY_DECAY ** (8 + i), 3)) for i, r in enumerate(same))
    assert row["n_used"] == 10
    te_mod.set_params(SAME_ALL_RUNS=False)               # te2: 枠外の同条件走は捨てていた (重み None・推定は換算だけ)
    row2 = te_mod.evaluate(_race(horses=[h]), _model())["horses"][0]
    assert [r["weight"] for r in row2["runs"] if r["kind"] == "same"] == [None, None] and row2["n_used"] == 8
    assert row["est_raw_sec"] < row2["est_raw_sec"]      # 同条件の 88.0 秒台が入って推定が速くなる
