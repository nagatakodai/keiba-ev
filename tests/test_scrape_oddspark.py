"""oddspark NAR フォールバック (src/scrape_oddspark.py) の純粋関数テスト (ネット不要)。"""
from __future__ import annotations

from src import scrape_oddspark as op


def test_date_key_handles_non_zero_padded():
    """leakage 除外の日付比較が非ゼロ詰めでも正しく順序付く (文字列比較の罠の回帰防止)。"""
    # '2026.5.2' は文字列比較だと '2026.05.26' より大きく判定され正当な過去走を誤除外する
    assert op._date_key("2026.5.2") < op._date_key("2026.05.26")
    assert op._date_key("2026/5/2") == (2026, 5, 2)
    assert op._date_key("2026.5.30") > op._date_key("2026.5.3")
    assert op._date_key("garbage") == (0, 0, 0)


# 単複テーブルの最小 HTML (実構造を模倣): [枠, 馬番, 馬名, 単勝, 複勝 "min - max"]
# 末尾2行は別テーブル混入を模した「馬名が数字のみ」の spurious 行 → 除外されること。
_TANFUKU_HTML = """
<table>
<tr><th>枠</th><th>馬番</th><th>馬名</th><th>単勝</th><th>複勝</th></tr>
<tr><td>1</td><td>1</td><td>チョボナイノ</td><td>17.5</td><td>1.4 - 2.6</td></tr>
<tr><td>3</td><td>5</td><td>マリアディオーサ</td><td>1.2</td><td>1.0 - 1.0</td></tr>
<tr><td>5</td><td>8</td><td>ピナーカ</td><td>14.7</td><td>2.7 - 5.8</td></tr>
<tr><td>6</td><td>6</td><td>6</td><td>29.2</td><td></td></tr>
<tr><td>7</td><td>7</td><td>7</td><td>180.5</td><td></td></tr>
</table>
"""


def test_parse_tanfuku_extracts_real_horses():
    horses = op.parse_tanfuku(_TANFUKU_HTML)
    nums = [h.number for h in horses]
    assert nums == [1, 5, 8]                      # spurious 6/7 (馬名=数字) は除外
    assert len(nums) == len(set(nums))            # 重複なし
    fav = next(h for h in horses if h.number == 5)
    assert fav.name == "マリアディオーサ"
    assert fav.win_odds == 1.2
    assert (fav.place_min, fav.place_max) == (1.0, 1.0)


def test_market_win_probs_from_tanfuku_unnormalized():
    # de-vig (power_method_overround) が overround を観測できるよう **未正規化** 1/odds を
    # 返す仕様 (2026-06-10〜)。正規化済みを渡すと power method が k=1 の no-op に縮退する。
    horses = op.parse_tanfuku(_TANFUKU_HTML)
    mwp = op.market_win_probs_from_tanfuku(horses)
    for h in horses:
        if h.win_odds > 0:
            assert abs(mwp[h.number] - 1.0 / h.win_odds) < 1e-12
    # 単勝 1.2 の馬が最大の市場暗黙率
    assert max(mwp, key=mwp.get) == 5


def test_nar_date_and_jra_none():
    # NAR (場 46=金沢): YYYYMMDD = 2026 + MMDD(rid[6:10])
    assert op._nar_date("202646052502") == "20260525"
    # JRA race_id は NAR 判定外 → None
    assert op._nar_date("202605021203") is None


_QUINELLA_HTML = """
<table>
<tr><th>1</th><th>2</th></tr>
<tr><th class="th2">2</th><td><span>5.0</span></td><th class="th2">3</th><td><span>7.0</span></td></tr>
<tr><th class="th2">3</th><td><span>9.0</span></td><td class="nob_all"></td></tr>
</table>
"""


def test_parse_pair_grid_triangular():
    # 列位置=1着, th=2着 → 馬連 (1,2),(2,3),(1,3)
    q = op.parse_pair_grid(_QUINELLA_HTML)
    assert sorted((a, b) for a, b, _ in q) == [(1, 2), (1, 3), (2, 3)]
    d = {(a, b): o for a, b, o in q}
    assert d[(1, 2)] == 5.0 and d[(2, 3)] == 7.0 and d[(1, 3)] == 9.0


_WIDE_HTML = """
<table>
<tr><th>1</th></tr>
<tr><th class="th2">2</th><td>129.2\n - 148.6</td></tr>
</table>
"""


def test_parse_pair_grid_wide_uses_min():
    w = op.parse_pair_grid(_WIDE_HTML, value_mode="min")
    assert w == [(1, 2, 129.2)]   # 範囲の下限を採用


_TRIFECTA_HTML = """
<table>
<tr><th>馬番</th><th>オッズ</th></tr>
<tr><th class="th2">1 → 2 → 3</th><td><span>10.5</span></td></tr>
<tr><th class="th2">1 → 2 → 4</th><td><span>9999.9</span></td></tr>
</table>
"""


def test_parse_triple_list_trifecta_ordered():
    tri = op.parse_triple_list(_TRIFECTA_HTML, ordered=True)
    assert tri == [((1, 2, 3), 10.5), ((1, 2, 4), 9999.9)]


# 3連複: 1着軸ごとに別テーブル (table per axis)。全テーブルを舐めて完全列挙する。
_TRIO_HTML = """
<table><tr><th>1-2</th><th>1-3</th></tr>
<tr><th class="th2">3</th><td><span>5.0</span></td><th class="th2">4</th><td><span>7.0</span></td></tr></table>
<table><tr><th>2-3</th></tr>
<tr><th class="th2">4</th><td><span>9.0</span></td></tr></table>
"""


def test_parse_trio_grid_spans_multiple_tables():
    trio = op.parse_trio_grid(_TRIO_HTML)
    d = {k: o for k, o in trio}
    # table1: {1,2,3}=5.0, {1,3,4}=7.0  / table2: {2,3,4}=9.0
    assert d == {(1, 2, 3): 5.0, (1, 3, 4): 7.0, (2, 3, 4): 9.0}


# 馬単: 列見出し=2着、行 th=1着 (一定)。2着=3 の列が別テーブルに分かれる例。
_EXACTA_HTML = """
<table><tr><th>1</th><th>2</th></tr>
<tr><th class="th2">1</th><td></td><th class="th2">1</th><td><span>5.0</span></td></tr>
<tr><th class="th2">2</th><td><span>8.0</span></td><th class="th2">2</th><td></td></tr></table>
<table><tr><th>3</th></tr>
<tr><th class="th2">1</th><td><span>12.0</span></td></tr>
<tr><th class="th2">2</th><td><span>15.0</span></td></tr></table>
"""


def test_parse_exacta_grid_ordered_multitable():
    e = {(a, b): o for a, b, o in op.parse_exacta_grid(_EXACTA_HTML)}
    # 1着=1: 2着2=5.0, 2着3=12.0 / 1着=2: 2着1=8.0, 2着3=15.0 (自分の列は空=skip)
    assert e == {(1, 2): 5.0, (2, 1): 8.0, (1, 3): 12.0, (2, 3): 15.0}


_HORSEDETAIL_HTML = """
<table>
<tr><th>年月日</th><th>競馬場</th><th>レース名</th><th>距離</th><th>馬場(天候)</th><th>頭数</th>
<th>枠番</th><th>馬番</th><th>人気</th><th>着順</th><th>騎手</th><th>負担重量</th><th>馬体重</th>
<th>タイム</th><th>着差</th><th>上3F</th><th>通過順位</th><th>1着馬</th></tr>
<tr><td>2026/05/12</td><td>金沢</td><td>特別Ｂ１ <a>映像</a></td><td>ダ1500</td><td>良(曇)</td><td>9</td>
<td>2</td><td>2</td><td>4</td><td>1</td><td>青柳正</td><td>57.0</td><td>477</td>
<td>1:41.2</td><td>1.7</td><td>41.9</td><td>4-4-4</td><td>テイコク</td></tr>
<tr><td>2026/04/17</td><td>金沢</td><td>一般 <a>映像</a></td><td>芝1400</td><td>稍重(雨)</td><td>10</td>
<td>3</td><td>5</td><td>7</td><td>9</td><td>松戸政</td><td>56.0</td><td>481</td>
<td>1:35.4</td><td>3.0</td><td>39.6</td><td>2-2-3</td><td>ウマＸ</td></tr>
</table>
"""


def test_parse_horse_detail():
    runs = op.parse_horse_detail(_HORSEDETAIL_HTML)
    assert len(runs) == 2
    r0 = runs[0]
    assert r0.date == "2026.05.12" and r0.venue == "金沢"
    assert r0.surface == "ダート" and r0.distance == 1500
    assert r0.going == "良"                       # (天候) を除去
    assert r0.field_size == 9 and r0.popularity == 4
    assert r0.finish_pos == 1                      # 1着 → int
    assert abs(r0.own_time_sec - 101.2) < 1e-6     # 1:41.2 = 101.2s (自走時計)
    assert r0.last_3f_sec == 41.9 and r0.body_weight == 477
    # 4着以下は netkeiba 慣習に合わせ finish_pos=None
    assert runs[1].finish_pos is None and runs[1].surface == "芝"


def test_build_oddspark_racedata_filters_leakage(monkeypatch):
    """build_oddspark_racedata は対象 race 日付以降の past_run を除外し直近5走に制限する。

    HorseDetail は過去 race 解析時に対象 race 自身を含む (leakage) ので、その除去を検証。
    """
    from src.models import PastRun

    def fake_runs(lineage_nb):
        # newest-first。先頭は対象 race と同日 (=leak)、以降は過去 7 走
        return [PastRun(date=d, surface="ダート", distance=1400, going="良",
                        winner_time_sec=85.0, finish_pos=None)
                for d in ["2026.05.24", "2026.05.10", "2026.04.28", "2026.04.15",
                          "2026.04.01", "2026.03.18", "2026.03.04", "2026.02.20"]]
    monkeypatch.setattr(op, "fetch_horse_past_runs", fake_runs)
    horses = [op.OddsparkHorse(1, "ウマA", 3.0, 1.2, 1.8, lineage_nb="L1")]
    # 佐賀 (55) R12 2026/05/24 → race_date 2026.05.24
    rd = op.build_oddspark_racedata(horses, "202655052412", fetch_past=True)
    runs = rd.race.horses[0].past_runs
    assert all(r.date < "2026.05.24" for r in runs)   # 対象日(leak)を除外
    assert len(runs) <= 5                              # 直近5走に制限
    assert runs[0].date == "2026.05.10"                # leak 除去後の最新


def test_build_oddspark_racedata_minimal():
    horses = op.parse_tanfuku(_TANFUKU_HTML)
    rd = op.build_oddspark_racedata(horses, "202646052502")
    assert rd.race.venue_name == "金沢"
    assert {h.number for h in rd.race.horses} == {1, 5, 8}
    assert "win" in rd.other_bets and "place" in rd.other_bets
    # 単勝 BetOdds は人気順 (オッズ昇順) で popularity 付与
    assert rd.other_bets["win"][0].odds == 1.2
