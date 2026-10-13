"""当レース条件 (距離/馬場/周回/天候・馬場状態) と全履歴 past_runs_ext のテスト (ネット不要)。

- keiba.go.jp 出馬表 (DebaTable) ヘッダ → parse_deba_header → Race (ばんえいは距離/馬場を載せない)
- JRA 馬場情報 (静的ページ parse_jra_baba / live JSON parse_jra_baba_live) → fetch_jra_going
  (cache / fallback / 通信失敗) → Race.weather_text
- weather_text の形 ("天候 / 馬場") が provisional._today_going で正しく読めること
- past_runs_ext (leakage 除外・最大12走) と past_runs (5走窓のまま) の両立

fixture は 2026-10-10 の実機 HTML / JSON から必要部分だけを切り出した最小再現
(リポジトリは *.html を git 管理しないので inline 文字列で持つ)。
"""
from __future__ import annotations

import pytest

import json

from src import provisional
from src import scrape_jra as jra
from src import scrape_keibago as kg
from src import scrape_oddspark as op
from src.models import BetOdds, Horse, PastRun
from src.parse import format_weather_text, normalize_going_word


def _deba(data_li: str, *, h3: str = "コスモス特別Ｂ", horses: str = "") -> str:
    """DebaTable の実構造 (h4 → racetime → section.raceTitle → 馬ブロック → コースレコード欄)。"""
    return f"""
<h4>
        2026年10月10日（土）　佐　賀　第5競走　17:00発走    </h4>
    <p class="racetime">
        サガテレビかちスポ賞    <br />
            <br />
    </p>
    <section class="raceTitle">
        <p class="roundInfo"></p>
        <p class="date"></p>
        <p class="subTitle"></p>
        <h3>{h3}</h3>
        <ul class="dataArea">
            <li>{data_li}            </li>
                        <li>賞金　1着1,000,000円　2着350,000円　3着200,000円</li>
        </ul>
    </section>
{horses}
            <section class="tableScale">
                <ul class="dataArea">
                    <li>コースレコード　1:19.9　ダノングッド　56　多田羅　　誠　也　2023/01/15</li>
                </ul>
            </section>
"""


# 馬ブロック (馬柱ミニ表に「不良」「右1750」等がある = ヘッダと取り違えてはいけない)
_HORSES = """
<table><tbody>
<tr><td rowspan="5" class="horseNum">1</td>
<td><a class="horseName" href="../DataRoom/HorseMarkInfo?k_lineageLoginCode=30123456789">アルファ</a></td>
<td><div class="raceInfo"><span class="pastRank rank_05">5</span>
    26.05.03　不良　9頭<br>
    佐賀　右1750　1番</div></td></tr>
<tr><td rowspan="5" class="horseNum">2</td>
<td><a class="horseName" href="../DataRoom/HorseMarkInfo?k_lineageLoginCode=30123456790">ブラボー</a></td>
<td><div class="raceInfo"><span class="pastRank rank_02">2</span>
    26.04.18　重　12頭<br>
    佐賀　右1400　12番</div></td></tr>
</tbody></table>
"""

_LI_SAGA = "ダート　1300ｍ（右）　　天候：曇　　馬場：良　　サラブレッド系　一般 別定　　＊電話投票コード：61#"
_LI_KOCHI = ("ダート　1400ｍ（右）　　天候：晴　　馬場：稍重　　サラブレッド系　3歳以上 別定　　"
             "＊電話投票コード：55#")
_LI_BANEI = "ダート　200ｍ（直）　　天候：晴　　馬場：1.2　　混合　3歳以上 規定　　＊電話投票コード：03#"

_DEBA_SAGA = _deba(_LI_SAGA, horses=_HORSES)
_DEBA_KOCHI = _deba(_LI_KOCHI, h3="横浜特別（ベストウォーリア賞）３歳以上", horses=_HORSES)
_DEBA_BANEI = _deba(_LI_BANEI, h3="夫よ、早く税理士になれよ杯Ｃ２－４", horses=_HORSES)


# ---------------------------------------------------------- keiba.go.jp --

def test_parse_deba_header_saga():
    h = kg.parse_deba_header(_DEBA_SAGA)
    assert h == {"distance": 1300, "surface": "ダート", "direction": "右", "course": "",
                 "weather": "曇", "going": "良", "race_name": "コスモス特別B"}


def test_parse_deba_header_kochi_heavyish():
    h = kg.parse_deba_header(_DEBA_KOCHI)
    # 馬場は keiba.go.jp と同じ全語 (netkeiba の略記「稍」ではない)。レース名の括弧に惑わされない。
    assert (h["distance"], h["direction"], h["weather"], h["going"]) == (1400, "右", "晴", "稍重")


def test_parse_deba_header_without_weather_going():
    # 天候/馬場の発表前。馬ブロックの「不良」「重」を当レースの馬場と取り違えない。
    h = kg.parse_deba_header(_deba("ダート　1600ｍ（左）　　サラブレッド系　一般 別定", horses=_HORSES))
    assert h == {"distance": 1600, "surface": "ダート", "direction": "左", "course": "",
                 "race_name": "コスモス特別B"}
    # section.raceTitle の外に「天候：/馬場：」形の記述があっても拾わない (ヘッダに限定)
    decoy = '<div class="raceInfo">前走 ダート　1800ｍ（右）　天候：雨　馬場：不良</div>'
    h2 = kg.parse_deba_header(_deba("ダート　1600ｍ（左）　　サラブレッド系", horses=decoy + _HORSES))
    assert h2 == {"distance": 1600, "surface": "ダート", "direction": "左", "course": "",
                  "race_name": "コスモス特別B"}


def test_parse_deba_header_outer_course_and_turf():
    h = kg.parse_deba_header(_deba("ダート　1000ｍ（外コース・右）　　天候：雨　　馬場：不良"))
    assert (h["distance"], h["direction"], h["course"], h["weather"], h["going"]) == \
        (1000, "右", "外", "雨", "不良")
    t = kg.parse_deba_header(_deba("芝　1700ｍ（左）　　天候：曇　　馬場：重"))
    assert (t["surface"], t["distance"], t["direction"], t["going"]) == ("芝", 1700, "左", "重")


def test_parse_deba_header_fullwidth_digits():
    h = kg.parse_deba_header(_deba("ダート　１４００ｍ（左）　　天候：小雨　　馬場：重"))
    assert (h["distance"], h["direction"], h["weather"], h["going"]) == (1400, "左", "小雨", "重")


def test_parse_deba_header_banei_moisture_is_not_going():
    h = kg.parse_deba_header(_DEBA_BANEI)
    assert h["distance"] == 200 and h["direction"] == "直" and h["weather"] == "晴"
    assert "going" not in h and h["moisture"] == 1.2   # ばんえいの馬場欄は水分量


def test_parse_deba_header_degrades_without_raising():
    assert kg.parse_deba_header("") == {}
    assert kg.parse_deba_header(None) == {}  # type: ignore[arg-type]
    assert kg.parse_deba_header("<html><head></head><body></body></html>") == {}   # 骨組み HTML
    # section が無い DOM 変化でも最初の馬ブロックより前だけを見る (馬柱の「馬場：不良」は拾わない)
    html = ('<ul class="dataArea"><li>ダート 1400m（右） 天候：晴 馬場：重</li></ul>'
            '<td class="horseNum">1</td><li>馬場：不良</li>')
    assert kg.parse_deba_header(html)["going"] == "重"


def test_build_keibago_racedata_applies_header():
    deba = kg.parse_deba_table(_DEBA_SAGA)
    rd = kg.build_keibago_racedata("202655101005", deba, {1: 2.0, 2: 3.5}, fetch_past=False,
                                   race_header=kg.parse_deba_header(_DEBA_SAGA))
    r = rd.race
    assert (r.distance, r.surface, r.direction, r.weather_text) == (1300, "ダート", "右", "曇 / 良")


def test_build_keibago_racedata_without_header_keeps_old_values():
    rd = kg.build_keibago_racedata("202655101005", [(1, "A", "", False)], {}, fetch_past=False,
                                   race_header={})
    r = rd.race
    assert (r.distance, r.surface, r.direction, r.weather_text) == (0, "", "", "")


def test_banei_keeps_distance_zero_and_empty_surface():
    # banei LightGBM は netkeiba 出馬表 (distance=0 / surface='') で学習 → skew を作らない
    rd = kg.build_keibago_racedata("202665101001", [(1, "A", "", False)], {}, fetch_past=False,
                                   race_header=kg.parse_deba_header(_DEBA_BANEI))
    r = rd.race
    assert (r.distance, r.surface, r.direction) == (0, "", "")
    assert r.weather_text == "晴 / "                       # 天候だけは載せる (馬場=水分量は空)
    assert provisional._today_going(rd) == ""            # 天候が馬場欄に入らない


def test_weather_text_shape_read_by_provisional():
    rd = kg.build_keibago_racedata("202654101004", [(1, "A", "", False), (2, "B", "", False)], {},
                                   fetch_past=False, race_header=kg.parse_deba_header(_DEBA_KOCHI))
    assert rd.race.weather_text == "晴 / 稍重"
    assert provisional._today_going(rd) == "稍重"
    assert provisional._going_sev(provisional._today_going(rd)) == 1
    # 天候のみ判明 ("曇 / ") は馬場不明 = None (天候を馬場と誤読しない)
    rd2 = kg.build_keibago_racedata("202654101004", [(1, "A", "", False)], {}, fetch_past=False,
                                    race_header={"weather": "曇"})
    assert rd2.race.weather_text == "曇 / "
    assert provisional._going_sev(provisional._today_going(rd2)) is None


def test_analyze_keibago_threads_deba_header_without_extra_get(monkeypatch):
    """analyze_keibago は取得済 DebaTable 1 回分のヘッダから条件を載せる (追加 GET 無し)。"""
    from src import analyze as az
    rid = "203255101005"    # 未来日付 = data/raw の netkeiba cache に存在しない佐賀5R
    loc = kg.KeibagoLoc(race_date="2032/10/10", race_no=5, baba_code="32", venue="佐賀")
    win = [BetOdds(bet_type="win", key=(1,), odds=2.0, popularity=1),
           BetOdds(bet_type="win", key=(2,), odds=3.5, popularity=2)]
    monkeypatch.setattr(kg, "find_keibago_race", lambda r: loc)
    monkeypatch.setattr(kg, "fetch_keibago_bets", lambda _loc: {
        "other_bets": {"win": win, "place": [], "quinella": [], "wide": [], "exacta": [],
                       "trio": []},
        "trifecta": [], "body_weights": {}, "consistency": {"ok": True}})
    gets: list[str] = []

    def fake_get(url, **_kw):
        gets.append(url)
        assert "DebaTable" in url
        return _DEBA_SAGA

    monkeypatch.setattr(kg, "_get", fake_get)
    monkeypatch.setattr(kg, "fetch_horse_past_runs", lambda code: [])
    seen = {}
    monkeypatch.setattr(az, "_run_score_stage", lambda race_id, rd, **kw: seen.setdefault("rd", rd))
    res = kg.analyze_keibago(rid, phase="score", with_llm=False)
    r = res["rd"].race
    assert (r.distance, r.surface, r.direction, r.weather_text) == (1300, "ダート", "右", "曇 / 良")
    assert len(gets) == 1                  # DebaTable 1 回のみ
    assert seen["rd"] is res["rd"]         # score 段 (Claude 見出し) にも条件が渡る


# ------------------------------------------------------------- 共通 helper --

def test_normalize_going_word_and_format():
    assert [normalize_going_word(s) for s in ("良", "稍重", "稍", "重", "不良", "不", "不明", "", "1.2")] \
        == ["良", "稍重", "稍重", "重", "不良", "不良", "", "", ""]
    assert format_weather_text("曇", "良") == "曇 / 良"
    assert format_weather_text("曇", "") == "曇 / "
    assert format_weather_text("", "良") == " / 良"
    assert format_weather_text("", "") == ""


# ------------------------------------------------------------ past_runs_ext --

def _runs(dates: list[str]) -> list[PastRun]:
    return [PastRun(date=d, surface="ダート", distance=1400, going="良", winner_time_sec=88.0)
            for d in dates]


# 先頭は対象 race 当日 (= leakage)、以降 13 走の過去 (新しい順)
_DATES_14 = ["2026.10.10", "2026.9.26", "2026.9.12", "2026.8.29", "2026.8.15", "2026.8.1",
             "2026.7.18", "2026.7.4", "2026.6.20", "2026.6.6", "2026.5.23", "2026.5.9",
             "2026.4.25", "2026.4.11"]


def _date_ok(r: PastRun) -> bool:
    return kg._date_key(r.date) < (2026, 10, 10)


def test_keibago_past_runs_ext_full_history(monkeypatch):
    monkeypatch.setattr(kg, "fetch_horse_past_runs", lambda code: _runs(_DATES_14))
    deba = [(1, "A", "301", False), (2, "B", "", False), (3, "C", "303", True)]
    rd = kg.build_keibago_racedata("202655101005", deba, {1: 2.0}, fetch_past=True)
    by = {h.number: h for h in rd.race.horses}
    h1 = by[1]
    assert len(h1.past_runs) == 5                 # LightGBM/仮指数の 5 走窓は不変
    assert len(h1.past_runs_ext) == 12            # leak 除外後 13 走 → 上限 12
    assert all(_date_ok(r) for r in h1.past_runs_ext)
    assert h1.past_runs == h1.past_runs_ext[:5]
    assert by[2].past_runs_ext == [] and by[3].past_runs_ext == []   # code 無し / 取消馬
    assert Horse(number=9, name="x").past_runs_ext == []             # netkeiba 経路は空のまま


def test_oddspark_past_runs_ext_full_history(monkeypatch):
    monkeypatch.setattr(op, "fetch_horse_past_runs", lambda ln: _runs(_DATES_14))
    horses = [op.OddsparkHorse(1, "ウマA", 3.0, 1.2, 1.8, lineage_nb="L1")]
    rd = op.build_oddspark_racedata(horses, "202655101012", fetch_past=True)
    h = rd.race.horses[0]
    assert len(h.past_runs) == 5 and len(h.past_runs_ext) == 12
    assert all(_date_ok(r) for r in h.past_runs_ext)


def test_jra_past_runs_ext_full_history(monkeypatch):
    monkeypatch.setattr(jra, "_load_jra_pastruns", lambda rid: {1: _runs(_DATES_14)})
    win = [BetOdds(bet_type="win", key=(1,), odds=3.0, popularity=1)]
    rd = jra.build_jra_racedata("202608040301", win, {}, {}, target_date=(2026, 10, 10))
    h = rd.race.horses[0]
    assert len(h.past_runs) == 5 and len(h.past_runs_ext) == 12
    assert all(_date_ok(r) for r in h.past_runs_ext)


# --------------------------------------------------------------- JRA 馬場 --

def _baba_page(current: str, *, date_ja: str = "2026年10月10日（土曜）",
               as_of: str = "（10月9日（金曜）正午現在）", weather: str = "晴",
               turf: str = "良", dirt: str = "良") -> str:
    """馬場情報の静的ページ (keiba/baba/index*.html) の最小再現。current = 表示中の場。"""
    tabs = []
    for href, v in (("index.html", "東京"), ("index2.html", "京都")):
        cls = ' class="current"' if v == current else ""
        tabs.append(f'<div{cls}><a href="{href}">{v}競馬場</a></div>')
    course = "A" if current == "東京" else "B"
    kai = "第4回東京競馬第3日" if current == "東京" else "第4回京都競馬第3日"
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="Shift_JIS">
<title>馬場情報（{current}競馬場）　JRA</title></head><body>
<li><a href="/kouza/baken/index.html#cat_sell">発売</a></li>
<div class="kaisai_tab">
<div class="nav tab">
{chr(10).join(tabs)}

</div>
</div>

<div id="baba" data-current-course="{course}">
<div class="unit" id="condition">
<div class="contents_header opt">
<div class="inner">
<div class="main"><h2>{kai}<span class="date">（{date_ja}）</span></h2></div>
</div>
</div>
<div class="line condition" id="course_condition" data-loading="on">
<div class="block_header opt">
<div class="content">
<div class="main"><h3>馬場状態<span class="time">{as_of}</span></h3></div>
</div>
</div>
<div class="grid">
<div class="cell weather">
<div class="weather sunny">
<div class="inner">
<div class="cell img"><img src="/keiba/baba/_img/weather/icon_weather_sunny.png" alt=""></div>
<div class="cell txt">天候：<strong>{weather}</strong></div>
</div>
</div>
</div>
<div class="cell turf">
<ul class="data_line_list">
<li>
<div class="data_list_unit">
<div class="head"><h4>芝</h4></div>
<div class="content">
<p>{turf}</p>
</div>
</div>
</li>
</ul>
</div>
<div class="cell dirt">
<ul class="data_line_list">
<li>
<div class="data_list_unit">
<div class="head"><h4>ダート</h4></div>
<div class="content">
<p>{dirt}</p>
</div>
</div>
</li>
</ul>
</div>
</div>
</div><!-- /[.line] -->
<table class="moist_graph_info"><thead><tr>
<th class="hard">良</th><th class="wet">稍重</th><th class="soft">重</th><th class="heavy">不良</th>
</tr></thead></table>
</div>
</div>
</body></html>
"""


_BABA_TOKYO = _baba_page("東京")
_BABA_KYOTO = _baba_page("京都", weather="曇", turf="稍重", dirt="重")

# accessJ.html (CNAME=pw01iwtS3/CD) の live JSON (実機 2026-10-10 15:12 の形を短縮)。
# 翌日 (D=日曜東京) は天候/馬場が空欄。
_BABA_LIVE = """{
  "kaisai_info" : [ {
    "sort" : "A", "kai" : "4", "day" : "3", "decided" : "9レースまで確定",
    "weather" : "晴", "class" : "ajo", "jyoname" : "東京",
    "kaisai_ymd" : "20261010", "youbi" : "土曜日", "ba_s" : "良", "ba_d" : "良", "race" : [ ]
  }, {
    "sort" : "B", "kai" : "4", "day" : "3", "decided" : "10レースまで確定",
    "weather" : "曇", "class" : "bjo", "jyoname" : "京都",
    "kaisai_ymd" : "20261010", "youbi" : "土曜日", "ba_s" : "稍重", "ba_d" : "重", "race" : [ ]
  }, {
    "sort" : "D", "kai" : "4", "day" : "4", "decided" : "",
    "weather" : "", "class" : "djo", "jyoname" : "東京",
    "kaisai_ymd" : "20261011", "youbi" : "日曜日", "ba_s" : "", "ba_d" : "",
    "race" : [ { "race_name" : "アイルランドT", "grade" : "G2", "race_no" : "11" } ]
  } ]
}
"""
_BABA_LIVE_EMPTY = '{\r\n  "kaisai_info" : [ ]\r\n}\r\n'


def test_parse_jra_baba_static_page():
    b = jra.parse_jra_baba(_BABA_TOKYO)
    assert b["venue"] == "東京" and b["date"] == "20261010" and b["course"] == "A"
    assert (b["weather"], b["芝"], b["ダート"]) == ("晴", "良", "良")
    assert b["as_of"] == "10月9日（金曜）正午現在"
    assert b["tabs"] == {"東京": "index.html", "京都": "index2.html"}   # 他ページの index.html は拾わない
    k = jra.parse_jra_baba(_BABA_KYOTO)
    assert (k["venue"], k["weather"], k["芝"], k["ダート"]) == ("京都", "曇", "稍重", "重")
    assert jra.parse_jra_baba("") == {}


def test_parse_jra_baba_live_json():
    live = jra.parse_jra_baba_live(_BABA_LIVE)
    assert set(live) == {"20261010"}                    # 翌日の空欄 entry は載せない
    assert live["20261010"]["京都"] == {"weather": "曇", "芝": "稍重", "ダート": "重",
                                       "as_of": "2026年10月10日（土曜日）", "source": "live"}
    assert jra.parse_jra_baba_live(_BABA_LIVE_EMPTY) == {}


def test_decode_jra_shift_jis_and_utf8():
    sj = _BABA_TOKYO.encode("cp932")
    assert jra.parse_jra_baba(jra._decode_jra(sj, "text/html"))["venue"] == "東京"   # meta 宣言
    live_sj = _BABA_LIVE.encode("cp932")          # live JSON は charset 宣言なしの Shift_JIS
    assert "京都" in jra.parse_jra_baba_live(jra._decode_jra(live_sj, "text/html"))["20261010"]
    assert jra._decode_jra("馬場".encode("utf-8"), "text/html; charset=UTF-8") == "馬場"


def _fake_baba(monkeypatch, tmp_path, *, live: str | None, pages: dict | None = None,
               fail: bool = False) -> list[str]:
    calls: list[str] = []

    def fake_http(url, data=None, **_kw):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        if fail:
            raise OSError("network down")
        if name == "accessJ.html":
            assert data == {"CNAME": jra._BABA_LIVE_CNAME}
            if live is None:
                raise OSError("live down")
            return live
        if pages and name in pages:
            return pages[name]
        raise OSError(f"404 {name}")

    monkeypatch.setattr(jra, "_baba_http", fake_http)
    monkeypatch.setattr(jra, "_BABA_CACHE_DIR", tmp_path)
    monkeypatch.setattr(jra, "_BABA_MEM", {})
    return calls


def test_fetch_jra_going_live_first_and_cached(monkeypatch, tmp_path):
    calls = _fake_baba(monkeypatch, tmp_path, live=_BABA_LIVE,
                       pages={"index.html": _BABA_TOKYO, "index2.html": _BABA_KYOTO})
    g = jra.fetch_jra_going("東京競馬場", "20261010")
    assert g == {"weather": "晴", "芝": "良", "ダート": "良",
                 "as_of": "2026年10月10日（土曜日）", "source": "live"}
    assert calls == ["accessJ.html"]                  # live に当日値があれば静的ページは見ない
    # 1 POST で全場が cache される → 別の場もネット無しで返る
    assert jra.fetch_jra_going("京都", "20261010")["ダート"] == "重"
    assert calls == ["accessJ.html"]
    # 別 subprocess 相当 (メモリ cache 空) でもディスク cache (TTL 内) を使う
    monkeypatch.setattr(jra, "_BABA_MEM", {})
    assert jra.fetch_jra_going("東京", "20261010")["芝"] == "良"
    assert calls == ["accessJ.html"]
    assert (tmp_path / "20261010.json").exists()


def test_fetch_jra_going_ttl_expiry_refetches(monkeypatch, tmp_path):
    calls = _fake_baba(monkeypatch, tmp_path, live=_BABA_LIVE)
    assert jra.fetch_jra_going("東京", "20261010") is not None
    p = tmp_path / "20261010.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    for e in d.values():
        e["fetched_at"] -= 2000                       # TTL (900s) 超過を模擬
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(jra, "_BABA_MEM", {})
    assert jra.fetch_jra_going("東京", "20261010") is not None
    assert calls == ["accessJ.html", "accessJ.html"]


def test_fetch_jra_going_static_fallback_selects_venue_page(monkeypatch, tmp_path):
    # live が空 (実機でも時間帯により kaisai_info=[]) → 静的ページを開催タブで辿る。
    # 前日 (10/9) 時点の取得 = 開催日 10/10 の前日発表は「最新の予想」として採用される
    monkeypatch.setattr(jra, "_jst_today", lambda: "20261009")
    calls = _fake_baba(monkeypatch, tmp_path, live=_BABA_LIVE_EMPTY,
                       pages={"index.html": _BABA_TOKYO, "index2.html": _BABA_KYOTO})
    g = jra.fetch_jra_going("京都", "20261010")
    assert g == {"weather": "曇", "芝": "稍重", "ダート": "重",
                 "as_of": "10月9日（金曜）正午現在", "source": "page"}
    assert calls == ["accessJ.html", "index.html", "index2.html"]   # index3 は引かない
    # 東京は index.html を読んだ時点で cache 済
    assert jra.fetch_jra_going("東京", "20261010")["source"] == "page"
    assert calls == ["accessJ.html", "index.html", "index2.html"]


def test_fetch_jra_going_rejects_previous_day_values_on_race_day(monkeypatch, tmp_path):
    """開催当日、静的ページは「前日正午現在」のまま → 当日の馬場として使わない (2026-10-10 レビュー)。"""
    monkeypatch.setattr(jra, "_jst_today", lambda: "20261010")
    _fake_baba(monkeypatch, tmp_path, live=_BABA_LIVE_EMPTY,
               pages={"index.html": _BABA_TOKYO, "index2.html": _BABA_KYOTO})
    assert jra.fetch_jra_going("東京", "20261010") is None
    assert jra._static_values_current("10月10日（土曜）9時現在", "20261010")
    assert not jra._static_values_current("10月9日（金曜）正午現在", "20261010")
    assert not jra._static_values_current("", "20261010")
    assert jra._static_values_current("10月9日（金曜）正午現在", "20991231")   # 未来日は前日発表を採用


def test_fetch_jra_going_cache_only_never_hits_network(monkeypatch, tmp_path):
    """bet 段は cache だけを見る (JRA の馬場ページが詰まっても投票判断を待たせない)。"""
    calls = _fake_baba(monkeypatch, tmp_path, live=None, fail=True)
    assert jra.fetch_jra_going("東京", "20261010", cache_only=True) is None
    assert calls == []


def test_fetch_jra_going_static_date_mismatch_and_unknown_venue(monkeypatch, tmp_path):
    calls = _fake_baba(monkeypatch, tmp_path, live=_BABA_LIVE_EMPTY,
                       pages={"index.html": _BABA_TOKYO, "index2.html": _BABA_KYOTO})
    # 静的ページの開催日 (10/10) ≠ 対象日 (10/11) → 別日の発表値は使わない
    assert jra.fetch_jra_going("東京", "20261011") is None
    assert calls == ["accessJ.html", "index.html"]
    # 「データ無し」も TTL 内は cache (叩き直さない)
    assert jra.fetch_jra_going("東京", "20261011") is None
    assert calls == ["accessJ.html", "index.html"]
    # 開催タブに無い場 (今週開催なし) は index.html だけ見て打ち切る
    calls.clear()
    assert jra.fetch_jra_going("中山", "20261010") is None
    assert calls == ["accessJ.html", "index.html"]


def test_fetch_jra_going_network_failure_returns_none_uncached(monkeypatch, tmp_path):
    calls = _fake_baba(monkeypatch, tmp_path, live=None, fail=True)
    assert jra.fetch_jra_going("東京", "20261010") is None
    assert not (tmp_path / "20261010.json").exists()   # 通信失敗は cache しない
    assert jra.fetch_jra_going("東京", "20261010") is None
    assert calls == ["accessJ.html", "index.html", "accessJ.html", "index.html"]   # 次段で再試行
    assert jra.fetch_jra_going("", "20261010") is None


def test_jra_weather_text_by_surface():
    going = {"weather": "曇", "芝": "稍重", "ダート": "重"}
    win = [BetOdds(bet_type="win", key=(1,), odds=3.0, popularity=1)]

    def wt(surface: str) -> str:
        hdr = {"distance": 1800, "surface": surface} if surface else {}
        return jra.build_jra_racedata("202608040301", win, {}, hdr, going=going).race.weather_text

    assert wt("ダート") == "曇 / 重"
    assert wt("芝") == "曇 / 稍重"
    assert wt("障害") == "曇 / 稍重"           # 障害は芝の馬場
    assert wt("") == "曇 / "                  # surface 不明なら馬場は推測しない
    rd = jra.build_jra_racedata("202608040301", win, {}, {"surface": "ダート"}, going=None)
    assert rd.race.weather_text == ""
    rd = jra.build_jra_racedata("202608040301", win, {}, {"surface": "芝"}, going=going)
    assert provisional._today_going(rd) == "稍重"


def test_overlay_jra_going_does_not_clobber_with_weather_only():
    from src.models import Race
    r = Race(cup_id="", schedule_index=0, race_number=1, venue_id=8, venue_name="京都",
             race_class="", distance=1800, surface="ダート", weather_text="晴 / 良")
    jra._overlay_jra_going(r, {"weather": "曇"})            # 馬場不明 → netkeiba 値を残す
    assert r.weather_text == "晴 / 良"
    jra._overlay_jra_going(r, {"weather": "曇", "ダート": "重"})
    assert r.weather_text == "曇 / 重"
    jra._overlay_jra_going(r, None)
    assert r.weather_text == "曇 / 重"


def test_analyze_jra_sets_weather_text_from_going(monkeypatch):
    """analyze_jra は fetch_jra_going (開催日・場名) の値を当レースの surface で weather_text に載せる。"""
    from src import analyze as az
    rid = "203208040301"    # 未来日付 = data/raw の netkeiba cache に存在しない京都1R
    loc = jra.JraLoc(netkeiba_rid=rid, venue="08", kai="04", day="03", race_no=1,
                     date="20321010", racelist_token="tok", odds_tokens={"tanfuku": "t"})
    win = [BetOdds(bet_type="win", key=(1,), odds=2.0, popularity=1),
           BetOdds(bet_type="win", key=(2,), odds=3.5, popularity=2)]
    monkeypatch.setattr(jra, "find_jra_race", lambda r: loc)
    monkeypatch.setattr(jra, "fetch_jra_bets", lambda _loc: {
        "other_bets": {"win": win, "place": []}, "trifecta": [], "consistency": {"ok": True},
        "horse_info": {1: {"name": "A"}, 2: {"name": "B"}},
        "race_header": {"distance": 1800, "surface": "ダート", "race_class": "3歳未勝利"}})
    asked = []
    monkeypatch.setattr(jra, "fetch_jra_going", lambda venue, date=None, **kw: (
        asked.append((venue, date)) or {"weather": "曇", "芝": "稍重", "ダート": "重"}))
    monkeypatch.setattr(jra, "_load_jra_pastruns", lambda r: None)
    monkeypatch.setattr(jra, "fetch_jra_past_runs", lambda hid, **kw: [])
    monkeypatch.setattr(jra, "_save_jra_pastruns", lambda r, by: None)
    monkeypatch.setattr(az, "_run_score_stage", lambda race_id, rd, **kw: None)
    res = jra.analyze_jra(rid, phase="score", with_llm=False)
    r = res["rd"].race
    assert asked == [("京都", "20321010")]
    assert (r.distance, r.surface, r.weather_text) == (1800, "ダート", "曇 / 重")


# ------------------------------------------- 当レースのクラス / 公式データ補完 (2026-10-13) --

def test_header_race_name_sets_race_class_including_banei():
    rd = kg.build_keibago_racedata("202655101005", [(1, "A", "", False)], {}, fetch_past=False,
                                   race_header=kg.parse_deba_header(_DEBA_SAGA))
    assert rd.race.race_class == "コスモス特別B"
    # ばんえいも race_class は載せる (LightGBM 特徴量は当レースの race_class を使わない・仮指数のクラス因子用)
    rb = kg.build_keibago_racedata("202665101001", [(1, "A", "", False)], {}, fetch_past=False,
                                   race_header=kg.parse_deba_header(_DEBA_BANEI))
    assert rb.race.race_class == "夫よ、早く税理士になれよ杯C2-4" and rb.race.distance == 0
    from src.speed_index import class_index
    assert class_index(rb.race.race_class) == -8


def _official_month_with(rid: str, *, surface="ダート", dist=1400, turn="右", weather="曇", going="重",
                         name="Ｃ１－３") -> None:
    """tmp の公式月次キャッシュに 1 レース分 (rid の場・日付・R) を置く (通信なし)。"""
    import datetime as dt
    import io
    import zipfile

    from src import nar_official as no
    from src.parse import VENUE_CODE
    venue = VENUE_CODE[rid[4:6]] if rid[4:6] != "65" else "帯広ば"
    ymd = rid[:4] + rid[6:10]
    hdr = ("競馬場,競走年月日,レース番号,発走時刻,競走種類名称,レース名,芝ダート区分,回り,距離,天候,馬場,頭数,条件")
    row = f"{venue},{ymd},{int(rid[10:12])},1500,普通,{name},{surface},{turn},{dist},{weather},{going},8,一般"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{ymd[:6]}_racelist.csv", ("﻿" + hdr + "\n" + row + "\n").encode("utf-8"))
    ym = ymd[:6]
    no.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    no.monthly_zip_path(ym).write_bytes(buf.getvalue())
    no._write_meta(ym, {"downloaded_at": dt.datetime(2026, 10, 13, 9, 0, tzinfo=no.JST).isoformat()})


def test_oddspark_racedata_gets_conditions_from_official(monkeypatch):
    """oddspark 経路は当レース条件を持たない → 公式データ (月次キャッシュ) から距離・芝ダ・馬場・クラスを補う。"""
    _official_month_with("202655091012")
    horses = [op.OddsparkHorse(1, "ウマA", 3.0, 1.2, 1.8, lineage_nb="")]
    rd = op.build_oddspark_racedata(horses, "202655091012", fetch_past=False)
    r = rd.race
    assert (r.distance, r.surface, r.direction, r.weather_text, r.race_class) == \
        (1400, "ダート", "右", "曇 / 重", "C1-3")
    from src import time_eval
    assert time_eval.target_of(r) == {"venue": "佐賀", "surface": "ダート", "distance": 1400,
                                      "going": "重", "going_source": "announced"}
    # 公式に無いレースは従来どおり条件不明 (例外なし)
    rd2 = op.build_oddspark_racedata(horses, "202655091011", fetch_past=False)
    assert (rd2.race.distance, rd2.race.surface, rd2.race.race_class) == (0, "", "")
    # race_header={} を明示すると公式データを引かない
    rd3 = op.build_oddspark_racedata(horses, "202655091012", fetch_past=False, race_header={})
    assert rd3.race.distance == 0


def test_oddspark_banei_keeps_distance_zero_with_official_header():
    _official_month_with("202665091005", surface="", dist=200, turn="直", weather="晴", going="1.1",
                         name="ばんえいＢ２－１")
    horses = [op.OddsparkHorse(1, "ウマA", 3.0, 1.2, 1.8, lineage_nb="")]
    rd = op.build_oddspark_racedata(horses, "202665091005", fetch_past=False)
    assert (rd.race.distance, rd.race.surface, rd.race.weather_text) == (0, "", "晴 / ")
    assert rd.race.race_class == "ばんえいB2-1"


def test_keibago_fill_header_from_official_when_deba_header_missing():
    """DebaTable のヘッダが取れない (DOM 変化・骨組み HTML) ときだけ公式データで補う。"""
    from src.nar_official import fill_race_header
    _official_month_with("202644091003", dist=1200, going="稍重", name="Ｂ３")
    hdr = fill_race_header(kg.parse_deba_header("<html><body></body></html>"), "202644091003")
    rd = kg.build_keibago_racedata("202644091003", [(1, "A", "", False)], {}, fetch_past=False, race_header=hdr)
    assert (rd.race.distance, rd.race.surface, rd.race.weather_text, rd.race.race_class) == \
        (1200, "ダート", "曇 / 稍重", "B3")
    # ヘッダが揃っていれば公式データは見ない (ヘッダの値のまま)
    full = kg.parse_deba_header(_DEBA_SAGA)
    assert fill_race_header(full, "202644091003") == full


def test_parse_deba_header_fallback_ignores_navi_h3():
    """section.raceTitle が無いページ (DOM 変化・骨組み) ではナビの <h3>オッズ</h3> をレース名にしない。"""
    html = ('<h3 class="naviTitle">オッズ</h3><ul class="dataArea"><li>ダート 1400m（右） 天候：晴 馬場：重</li></ul>'
            '<td class="horseNum">1</td>')
    h = kg.parse_deba_header(html)
    assert "race_name" not in h and h["distance"] == 1400
    # class 名が増えた section (raceTitle clearfix) は section として読む
    renamed = _DEBA_SAGA.replace('class="raceTitle"', 'class="raceTitle clearfix"')
    assert kg.parse_deba_header(renamed)["race_name"] == "コスモス特別B"


class _Stop(Exception):
    pass


@pytest.mark.parametrize("phase,allow", [("bet", False), ("score", True)])
def test_keibago_header_fill_never_downloads_in_bet_phase(monkeypatch, phase, allow):
    """bet 段 (締切 1〜2.5 分前) は公式データの補完で通信しない (手元のキャッシュだけ)。"""
    from src import nar_official
    from src.models import BetOdds
    monkeypatch.setattr(kg, "find_keibago_race",
                        lambda rid: kg.KeibagoLoc(race_date="2026/10/13", race_no=99, baba_code="21", venue="川崎"))
    monkeypatch.setattr(kg, "fetch_keibago_bets", lambda loc: {
        "other_bets": {"win": [BetOdds(bet_type="win", key=(1,), odds=2.0)]}, "trifecta": [],
        "consistency": {"ok": True}, "body_weights": {}})
    monkeypatch.setattr(kg, "_get", lambda url, **_k: "<html><body></body></html>")   # ヘッダの無い出馬表
    seen: dict = {}

    def fake_fill(hdr, rid, *, allow_download=True):
        seen["allow"] = allow_download
        raise _Stop

    monkeypatch.setattr(nar_official, "fill_race_header", fake_fill)
    monkeypatch.setattr(kg, "parse_deba_table", lambda html: [(1, "A", "", False)])
    with pytest.raises(_Stop):
        kg.analyze_keibago("202645101399", phase=phase)
    assert seen["allow"] is allow


@pytest.mark.parametrize("allow", [False, True])
def test_oddspark_official_header_respects_allow_download(monkeypatch, allow):
    from src import nar_official
    seen: dict = {}

    def fake_header(rid, *, allow_download=True):
        seen["allow"] = allow_download
        return {}

    monkeypatch.setattr(nar_official, "official_race_header", fake_header)
    horses = [op.OddsparkHorse(1, "ウマA", 3.0, 1.2, 1.8, lineage_nb="")]
    op.build_oddspark_racedata(horses, "202655101012", fetch_past=False, allow_official_download=allow)
    assert seen["allow"] is allow


_TANFUKU_ENTRIES = """
<table>
<tr><th>枠</th><th>馬番</th><th>馬名</th><th>単勝</th><th>複勝</th><th></th><th>性齢</th><th>馬体重</th><th>重量</th><th>騎手</th><th>場</th><th>調教師</th></tr>
<tr><td>1</td><td>1</td><td>アルファ</td><td>3.1</td><td>1.2-</td><td>1.6</td><td>牡4</td><td>480（+2）</td><td>▲53.0</td><td>笹川翼</td><td>大井</td><td>荒山勝</td></tr>
<tr><td>2</td><td>2</td><td>ブラボー</td><td>5.0</td><td>1.5-</td><td>2.0</td><td>セン5</td><td>500（-4）</td><td>56.0</td><td>矢野貴</td><td>大井</td><td>森下淳</td></tr>
<tr><td>3</td><td>3</td><td>チャーリー</td><td>取消</td><td></td><td></td><td>牝3</td><td></td><td>54.0</td><td>山本聡</td><td>大井</td><td>藤田輝</td></tr>
</table>
"""


def test_parse_tanfuku_entries_today_fields():
    e = kg.parse_tanfuku_entries(_TANFUKU_ENTRIES)
    assert e[1] == {"bracket": 1, "sex_age": "牡4", "weight": 53.0, "jockey": "笹川翼", "trainer": "荒山勝"}
    assert e[2]["sex_age"] == "セ5" and e[2]["weight"] == 56.0               # セン馬は netkeiba と同じ「セ」
    assert e[3]["jockey"] == "山本聡"                                         # 取消行でも出走表の項目は読む


def test_builder_applies_today_entries_before_official():
    entries = kg.parse_tanfuku_entries(_TANFUKU_ENTRIES)
    rd = kg.build_keibago_racedata("202644101001", [(1, "アルファ", "", False), (2, "ブラボー", "", False)],
                                   {1: 3.1}, fetch_past=False, entries=entries)
    a, b = rd.race.horses
    assert (a.jockey_name, a.weight_kg, a.sex_age, a.bracket, a.trainer_name) == ("笹川翼", 53.0, "牡4", 1, "荒山勝")
    assert rd.official_enrich["entries_filled"] == 0                          # 当日ページで埋まった = 公式は触らない
    assert rd.official_enrich["runners_no_jockey"] == 0


def test_builder_counts_runners_without_jockey():
    """当日ページにも公式データにも出走表が無い = 騎手不明の頭数を残す (縮退レースを後から探す用)。"""
    rd = kg.build_keibago_racedata("202644101001", [(1, "アルファ", "", False), (2, "ブラボー", "", False)],
                                   {1: 3.1}, fetch_past=False)
    assert rd.official_enrich["runners_no_jockey"] == 2


@pytest.mark.parametrize("phase,allow", [("bet", False), ("score", True)])
def test_keibago_enrichment_downloads_only_in_score_phase(monkeypatch, phase, allow):
    from src import nar_official
    from src.models import BetOdds
    monkeypatch.setattr(kg, "find_keibago_race",
                        lambda rid: kg.KeibagoLoc(race_date="2026/10/13", race_no=99, baba_code="21", venue="川崎"))
    monkeypatch.setattr(kg, "fetch_keibago_bets", lambda loc: {
        "other_bets": {"win": [BetOdds(bet_type="win", key=(1,), odds=2.0)]}, "trifecta": [],
        "consistency": {"ok": True}, "body_weights": {}, "entries": {}})
    monkeypatch.setattr(kg, "_get", lambda url, **_k: _DEBA_SAGA)
    monkeypatch.setattr(kg, "parse_deba_table", lambda html: [(1, "A", "", False)])
    seen: dict = {}

    def fake_enrich(rd, rid, *, banei=False, allow_download=False):
        seen["allow"] = allow_download
        raise _Stop

    monkeypatch.setattr(nar_official, "enrich_racedata", fake_enrich)
    with pytest.raises(_Stop):
        kg.analyze_keibago("202645101399", phase=phase)
    assert seen["allow"] is allow
