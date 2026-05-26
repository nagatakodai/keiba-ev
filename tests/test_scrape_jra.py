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
