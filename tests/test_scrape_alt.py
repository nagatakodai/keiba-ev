"""src/scrape_alt.py (keibalab fallback) の test。

netkeiba block 中に keibalab.jp から race list を取得する fallback パスの
parser 動作を検証する (ネットワーク不要、HTML fixture で test)。
"""
from __future__ import annotations

import re

import pytest


SAMPLE_KEIBALAB_HTML = """
<html>
<head><title>2026年5月23日のレース一覧 | 競馬ラボ</title></head>
<body>
<h1 class="heading01">レース情報</h1>
<h2>2026年5月23日のレース一覧</h2>

<table class="raceTable">
<thead><tr><th>R</th><th>競走名</th><th>条件</th></tr></thead>
<tbody>
<tr>
  <td class="raceNum tC"><div class="rCorner std12 turf"><a href="/db/race/202605230501/">1R</a></div><span class="std11">09:55</span></td>
  <td><a href="/db/race/202605230501/">サラ系3歳未勝利</a><br/><span class="std11">芝1600m 15頭</span></td>
  <td class="tC vM kakuteiBox"></td>
</tr>
<tr>
  <td class="raceNum tC"><div class="rCorner std12 turf"><a href="/db/race/202605230502/">2R</a></div><span class="std11">10:30</span></td>
  <td><a href="/db/race/202605230502/">サラ系3歳新馬</a><br/><span class="std11">芝1400m 16頭</span></td>
  <td class="tC vM kakuteiBox"></td>
</tr>
<tr>
  <td class="raceNum tC"><div class="rCorner std12 dirt"><a href="/db/race/202605230512/">12R</a></div><span class="std11">16:01</span></td>
  <td><a href="/db/race/202605230512/">サラ系4歳上2勝クラス</a><br/><span class="std11">ダ1400m 16頭</span></td>
  <td class="tC vM kakuteiBox"></td>
</tr>
</tbody>
</table>
</body>
</html>
"""


def test_keibalab_parser_extracts_race_id_and_time(monkeypatch):
    """fetch_race_list_keibalab は <tr> から (race_id, HH:MM 発走時刻) ペアを抽出する。"""
    from src import scrape_alt

    # _http_get を fixture HTML に差し替え
    monkeypatch.setattr(scrape_alt, "_http_get", lambda url, **kw: SAMPLE_KEIBALAB_HTML)

    races = scrape_alt.fetch_race_list_keibalab("20260523")
    assert len(races) == 3

    race_ids = [r.race_id for r in races]
    assert "202605230501" in race_ids
    assert "202605230502" in race_ids
    assert "202605230512" in race_ids

    # 発走時刻が unix timestamp として正しく出る
    for r in races:
        assert r.start_at > 0
        assert r.race_no == int(r.race_id[-2:])
        assert r.source == "keibalab"
        # netkeiba style URL が構築されている
        assert "race.netkeiba.com/race/shutuba.html?race_id=" in r.url
        assert r.race_id in r.url


def test_keibalab_parser_dedups(monkeypatch):
    """同 race_id が複数 <tr> に出てきても uniq になる。"""
    from src import scrape_alt
    # 2 つの table に同じ race_id がある HTML
    dup_html = SAMPLE_KEIBALAB_HTML + SAMPLE_KEIBALAB_HTML
    monkeypatch.setattr(scrape_alt, "_http_get", lambda url, **kw: dup_html)

    races = scrape_alt.fetch_race_list_keibalab("20260523")
    ids = [r.race_id for r in races]
    assert len(ids) == len(set(ids))  # uniq


def test_keibalab_empty_page(monkeypatch):
    """race link が無い page (e.g. JRA 非開催日) は 0 race を返す。"""
    from src import scrape_alt
    empty = "<html><body><h2>2026年5月20日のレース一覧</h2><p>本日 JRA 開催無し</p></body></html>"
    monkeypatch.setattr(scrape_alt, "_http_get", lambda url, **kw: empty)
    races = scrape_alt.fetch_race_list_keibalab("20260520")
    assert races == []


def test_keibalab_url_includes_date(monkeypatch):
    """fetch_race_list_keibalab(yyyymmdd) は URL に日付を含める。"""
    from src import scrape_alt
    captured = {}
    def fake_get(url, **kw):
        captured["url"] = url
        return "<html><body></body></html>"
    monkeypatch.setattr(scrape_alt, "_http_get", fake_get)
    scrape_alt.fetch_race_list_keibalab("20260524")
    assert "/db/race/20260524/" in captured["url"]


def test_keibalab_start_at_is_local_jst(monkeypatch):
    """発走時刻が target_date + HH:MM の local timestamp として計算される。"""
    from datetime import datetime
    from src import scrape_alt
    monkeypatch.setattr(scrape_alt, "_http_get", lambda url, **kw: SAMPLE_KEIBALAB_HTML)
    races = scrape_alt.fetch_race_list_keibalab("20260523")
    r1 = next(r for r in races if r.race_id == "202605230501")
    dt = datetime.fromtimestamp(r1.start_at)
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 23
    assert dt.hour == 9
    assert dt.minute == 55
