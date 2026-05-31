"""JRA 公式 (accessO) オッズパーサ + トークン解析のテスト (ネット不要)。

実構造を模した最小 HTML で、全券種が組合せ明示で正しく取れること、馬連/ワイド/馬単の
caption(軸)+th(相手) 構造、3連単の sub_header(1着)+2着区切り+th(3着)、token 解析を検証。
"""
from __future__ import annotations

from src import scrape_jra as jra


def test_parse_tanfuku():
    html = (
        '<tr><td class="num">1</td><td class="odds_tan">6.1</td>'
        '<td class="odds_fuku">1.4-2.0</td></tr>'
        '<tr><td class="num">2</td><td class="odds_tan">2.3</td>'
        '<td class="odds_fuku">1.2-1.7</td></tr>'
    )
    win, place = jra.parse_tanfuku(html)
    assert {b.key[0]: b.odds for b in win} == {1: 6.1, 2: 2.3}
    assert {b.key[0]: b.odds for b in place} == {1: 1.4, 2: 1.2}   # 複勝は min
    assert win[0].key[0] == 2 and win[0].popularity == 1            # 人気=オッズ昇順


# 実機 (2026-05-31 accessO 単複) の行構造を模した最小 HTML。馬名/性齢/馬体重(増減)/斤量/
# 騎手/調教師 + accessU CNAME (= horse_id) を同じ行から取れることを検証。
_TANFUKU_FULL = (
    '<tr><th class="num" scope="col">馬番</th><th class="horse" scope="col">馬名</th></tr>'
    '<tr><td class="waku" rowspan="2"><img alt="枠1白"/></td><td class="num">1</td>'
    '<td class="horse"><a href="/JRADB/accessU.html?CNAME=pw01dud002023104847/0B">フェアリーキャット</a></td>'
    '<td class="odds_tan">132.0</td>'
    '<td class="odds_fuku"><span class="min">15.9</span></td>'
    '<td class="age">牝3</td><td class="h_weight"> 432<span>(+6)</span></td>'
    '<td class="weight">52.0</td>'
    '<td class="jockey"><span class="mark jockey">▲</span><a href="#">石神 深道</a></td>'
    '<td class="trainer"><a href="#">鈴木 慎太郎</a></td></tr>'
    '<tr><td class="num">3</td>'
    '<td class="horse"><a href="/JRADB/accessU.html?CNAME=pw01dud00xyz/0B">ジュピターテソーロ</a></td>'
    '<td class="odds_tan">4.3</td><td class="odds_fuku"><span class="min">1.5</span></td>'
    '<td class="age">牡3</td><td class="h_weight"> 480<span>(-2)</span></td>'
    '<td class="weight">57.0</td>'
    '<td class="jockey"><a href="#">戸崎 圭太</a></td>'
    '<td class="trainer"><a href="#">高木 登</a></td></tr>'
)


def test_parse_jra_horses_extracts_name_jockey_weight():
    info = jra.parse_jra_horses(_TANFUKU_FULL)
    assert set(info) == {1, 3}
    h1 = info[1]
    assert h1["name"] == "フェアリーキャット"
    assert h1["sex_age"] == "牝3"
    assert h1["weight_kg"] == 52.0
    assert h1["body_weight"] == 432 and h1["body_weight_diff"] == 6
    assert "石神" in h1["jockey_name"]            # 減量マークが付いていても名前が取れる
    assert h1["horse_id"].startswith("pw01dud")
    assert info[3]["body_weight_diff"] == -2      # マイナス増減


def test_build_jra_racedata_populates_names_from_horse_info():
    from src.models import BetOdds
    win = [BetOdds(bet_type="win", key=(1,), odds=132.0, popularity=2),
           BetOdds(bet_type="win", key=(3,), odds=4.3, popularity=1)]
    info = jra.parse_jra_horses(_TANFUKU_FULL)
    rd = jra.build_jra_racedata("202605021101", win, info)
    names = {h.number: h.name for h in rd.race.horses}
    assert names == {1: "フェアリーキャット", 3: "ジュピターテソーロ"}
    by = {h.number: h for h in rd.race.horses}
    assert by[3].jockey_name and by[3].body_weight == 480


# accessU 馬詳細の競走成績テーブル (実機 2026-05-31 の最小再現)。
_ACCESSU_HIST = (
    '<table class="basic narrow-xy striped"><tbody>'
    '<tr><th>年月日</th><th>場</th><th>レース名</th><th>距離</th><th>馬場</th>'
    '<th>頭数</th><th>人気</th><th>着順</th><th>騎手名</th><th>負担重量</th>'
    '<th>馬体重</th><th>タイム</th><th>Rt</th><th>1着馬</th></tr>'
    '<tr><td class="date">2026年5月16日</td><td>新潟</td>'
    '<td class="race"><a href="#">3歳未勝利</a></td><td>ダ1200</td><td>良</td>'
    '<td>15</td><td>9</td><td>7</td>'
    '<td class="jockey"><a href="#">古川 奈穂</a></td><td>53.0</td><td>426</td>'
    '<td>1:14.2</td><td class="rate"></td><td class="horse">シュヴァルツシルト</td></tr>'
    '<tr><td class="date">2026年2月1日</td><td>東京</td>'
    '<td class="race"><a href="#">3歳新馬</a></td><td>芝1600</td><td>良</td>'
    '<td>16</td><td>3</td><td>2</td>'
    '<td class="jockey"><a href="#">戸崎 圭太</a></td><td>55.0</td><td>432</td>'
    '<td>1:37.3</td><td class="rate"></td><td class="horse">アスクイキゴミ</td></tr>'
    '</tbody></table>'
)


def test_parse_jra_past_runs():
    runs = jra.parse_jra_past_runs(_ACCESSU_HIST)
    assert len(runs) == 2
    r0 = runs[0]
    assert r0.date == "2026.5.16" and r0.venue == "新潟"
    assert r0.surface == "ダ" and r0.distance == 1200 and r0.going == "良"
    assert r0.field_size == 15 and r0.popularity == 9
    assert r0.finish_pos is None                       # 4着以下は None (1/2/3 のみ int)
    assert "古川" in r0.jockey and r0.body_weight == 426
    assert abs(r0.own_time_sec - 74.2) < 1e-6          # 自走時計
    assert runs[1].finish_pos == 2                     # 2着 → int


_HEADER_HTML = (
    '<div class="type">'
    '<div class="cell category">3歳</div><div class="cell class">未勝利</div>'
    '<div class="cell rule">[指定]</div><div class="cell weight">馬齢</div>'
    '<div class="cell course"><span class="cap">コース：</span>1,400'
    '<span class="unit">メートル</span>（ダート・左）</div></div>'
)


def test_parse_jra_race_header():
    h = jra.parse_jra_race_header(_HEADER_HTML)
    assert h["distance"] == 1400 and h["surface"] == "ダ"
    assert h["race_class"] == "3歳未勝利"


_QUINELLA = (
    "<caption>1</caption><tbody>"
    '<tr><th scope="row">2</th><td>5.7</td></tr>'
    '<tr><th scope="row">3</th><td>40.0</td></tr></tbody>'
    "<caption>2</caption><tbody>"
    '<tr><th scope="row">3</th><td>20.0</td></tr></tbody>'
)


def test_parse_quinella_caption_axis_th_partner():
    q = {b.label: b.odds for b in jra.parse_quinella(_QUINELLA)}
    assert q == {"1-2": 5.7, "1-3": 40.0, "2-3": 20.0}   # caption=軸, th=相手 明示


def test_parse_wide_min_of_range():
    html = ('<caption>1</caption><tbody>'
            '<tr><th scope="row">2</th>'
            '<td class="odds"><span class="min">2.2</span>-<span class="max">2.6</span></td></tr>'
            '</tbody>')
    w = jra.parse_wide(html)
    assert w[0].label == "1-2" and w[0].odds == 2.2   # 下限採用


def test_parse_exacta_ordered():
    html = ("<caption>2</caption><tbody>"
            '<tr><th scope="row">1</th><td>13.6</td></tr></tbody>'
            "<caption>1</caption><tbody>"
            '<tr><th scope="row">2</th><td>7.5</td></tr></tbody>')
    e = {b.label: b.odds for b in jra.parse_exacta(html)}
    assert e == {"2-1": 13.6, "1-2": 7.5}   # caption=1着, th=2着, 順序保持


def test_parse_trio_pair_caption_plus_third():
    html = ('<caption>1-2</caption><tbody>'
            '<tr><th scope="row">3</th><td>6.7</td></tr>'
            '<tr><th scope="row">4</th><td>9.9</td></tr></tbody>')
    t = {b.label: b.odds for b in jra.parse_trio(html)}
    assert t == {"1-2-3": 6.7, "1-2-4": 9.9}


def test_parse_trifecta_subheader_nichaku():
    # sub_header=1着 → 2着 区切り → th(3着)+td(odds)
    html = (
        '<h4 class="sub_header lg"><span class="num">1</span></h4>'
        '<div class="cap"><span>1着</span></div><div class="num">1</div>'
        '<div class="cap"><span>2着</span></div><div class="num">2</div>'
        '<table class="tan3"><tbody>'
        '<tr><th scope="row">3</th><td>10.0</td></tr>'
        '<tr><th scope="row">4</th><td>12.0</td></tr></tbody></table>'
        '<div class="cap"><span>2着</span></div><div class="num">3</div>'
        '<table class="tan3"><tbody>'
        '<tr><th scope="row">2</th><td>20.0</td></tr></tbody></table>'
    )
    tri = {t.label: t.odds for t in jra.parse_trifecta(html)}
    assert tri == {"1-2-3": 10.0, "1-2-4": 12.0, "1-3-2": 20.0}


def test_token_parsers():
    rl = jra._parse_racelist_token("pw15orl10052026021020260524/7A")
    assert rl == {"venue": "05", "year": "2026", "kai": "02", "day": "10",
                  "date": "20260524", "token": "pw15orl10052026021020260524/7A"}
    od = jra._parse_odds_token("pw158ou1005202602101120260524Z/B7")
    assert od["bt"] == "8" and od["venue"] == "05" and od["race_no"] == 11
    assert od["date"] == "20260524"


def test_consistency_flags_wide_gt_quinella():
    bad = [jra.BetOdds(bet_type="wide", key=(1, 2), odds=99.0)]
    other = {"quinella": jra.parse_quinella(_QUINELLA), "wide": bad}
    c = jra.check_consistency(other, [])
    assert c["wide_gt_quinella"] == 1 and c["ok"] is False


def test_parse_jra_result():
    html = (
        '<tr><td class="place">1</td><td class="num">16</td></tr>'
        '<tr><td class="place">2</td><td class="num">12</td></tr>'
        '<tr><td class="place">3</td><td class="num">18</td></tr>'
        '<li class="tierce"><span>3連単</span>'
        '<div class="num">16-12-18</div><div class="yen">30,330円</div></li>'
    )
    r = jra.parse_jra_result(html)
    assert r["finish_order"] == [16, 12, 18]
    assert r["payout"] == 30330


def test_parse_wide_always_lower_bound_even_if_reversed():
    """JRA ワイドは span が max,min の順でも常に下限。"""
    h=('<caption>1</caption><tbody><tr><th scope="row">2</th>'
       '<td class="odds"><span class="max">7.2</span>-<span class="min">5.0</span></td></tr></tbody>')
    assert jra.parse_wide(h)[0].odds == 5.0
