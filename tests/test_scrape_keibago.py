"""keiba.go.jp (地方競馬公式) オッズパーサの純粋関数テスト (ネット不要)。

実構造を模した最小 HTML で、全券種が **組合せ明示** で正しく取れること、
複勝/ワイドの下限採用、馬単の順序保持、整合チェックを検証する。
"""
from __future__ import annotations

from src import scrape_keibago as kg


_TANFUKU = """
<table>
<tr><th>枠</th><th>馬番</th><th>馬名</th><th>単勝</th><th>複勝</th><th></th><th>性齢</th></tr>
<tr><td>1</td><td>1</td><td>アルファ</td><td>6.1</td><td>1.4-</td><td>2.0</td><td>牝5</td></tr>
<tr><td>2</td><td>2</td><td>ブラボー</td><td>2.3</td><td>1.2-</td><td>1.7</td><td>牝4</td></tr>
<tr><td>3</td><td>3</td><td>チャーリー</td><td>41.6</td><td>5.1-</td><td>8.7</td><td>牡7</td></tr>
<tr><td>4</td><td>4</td><td>取消馬</td><td>取消</td><td></td><td></td><td>牡4</td></tr>
</table>
"""

# 馬連 (順不同, 単一オッズ)
_QUINELLA = """
<table>
<tr><td>1-2</td><td class="odd_red">5.7</td></tr>
<tr><td>1-3</td><td>40.0</td></tr>
<tr><td>2-3</td><td>20.0</td></tr>
</table>
"""

# ワイド (順不同, 'min -max' → 下限採用)
_WIDE = """
<table>
<tr><td>1-2</td><td>2.2 -2.6</td></tr>
<tr><td>1-3</td><td>12.7 -14.5</td></tr>
<tr><td>2-3</td><td>9.0 -10.0</td></tr>
</table>
"""

# 馬単 (順序あり, a-b ≠ b-a)
_EXACTA = """
<table>
<tr><td>2-1</td><td>7.5</td></tr>
<tr><td>1-2</td><td>13.6</td></tr>
<tr><td>2-3</td><td>55.0</td></tr>
</table>
"""

# 3連複 (順不同, 昇順)
_TRIO = """
<table>
<tr><td>1-2-3</td><td>6.7</td></tr>
<tr><td>2-3-4</td><td>11.3</td></tr>
</table>
"""

# 3連単 (順序あり)
_TRIFECTA = """
<table>
<tr><td>2-1-3</td><td>24.0</td></tr>
<tr><td>1-2-3</td><td>32.4</td></tr>
<tr><td>3-2-1</td><td>120.0</td></tr>
</table>
"""


def test_parse_tanfuku_win_place_and_skip_scratch():
    win, place = kg.parse_tanfuku(_TANFUKU)
    win_d = {b.key[0]: b.odds for b in win}
    place_d = {b.key[0]: b.odds for b in place}
    assert win_d == {1: 6.1, 2: 2.3, 3: 41.6}   # 取消(4) は除外
    assert place_d == {1: 1.4, 2: 1.2, 3: 5.1}   # 複勝は下限採用
    # 人気は単勝オッズ昇順
    assert win[0].key[0] == 2 and win[0].popularity == 1


def test_parse_quinella_unordered():
    q = {b.label: b.odds for b in kg.parse_quinella(_QUINELLA)}
    assert q == {"1-2": 5.7, "1-3": 40.0, "2-3": 20.0}


def test_parse_wide_uses_lower_bound():
    w = {b.label: b.odds for b in kg.parse_wide(_WIDE)}
    assert w == {"1-2": 2.2, "1-3": 12.7, "2-3": 9.0}   # 'min -max' の下限


def test_parse_exacta_preserves_order():
    e = {b.label: b.odds for b in kg.parse_exacta(_EXACTA)}
    assert e["2-1"] == 7.5 and e["1-2"] == 13.6   # 順序で別物


def test_parse_trio_sorted_key():
    t = {b.label: b.odds for b in kg.parse_trio(_TRIO)}
    assert t == {"1-2-3": 6.7, "2-3-4": 11.3}
    assert all(len(b.key) == 3 for b in kg.parse_trio(_TRIO))


def test_parse_trifecta_ordered():
    tri = kg.parse_trifecta(_TRIFECTA)
    d = {t.label: t.odds for t in tri}
    assert d == {"2-1-3": 24.0, "1-2-3": 32.4, "3-2-1": 120.0}
    assert tri[0].label == "2-1-3" and tri[0].popularity == 1   # 最安が人気1


def test_consistency_ok_when_wide_le_quinella():
    other = {
        "win": kg.parse_tanfuku(_TANFUKU)[0],
        "quinella": kg.parse_quinella(_QUINELLA),
        "wide": kg.parse_wide(_WIDE),
    }
    c = kg.check_consistency(other, kg.parse_trifecta(_TRIFECTA))
    assert c["ok"] is True
    assert c["wide_gt_quinella"] == 0


def test_consistency_flags_wide_gt_quinella():
    """ワイド > 馬連 (パース異常 / 誤オッズ) を検知する。"""
    bad_wide = [kg.BetOdds(bet_type="wide", key=(1, 2), odds=99.0)]
    other = {"quinella": kg.parse_quinella(_QUINELLA), "wide": bad_wide}
    c = kg.check_consistency(other, [])
    assert c["wide_gt_quinella"] == 1
    assert c["ok"] is False


# 出馬表: 馬番 (horseNum) + 競走馬ID (k_lineageLoginCode) + 馬名
_DEBA = """
<tr class="tBorder">
<td rowspan="5" class="courseNum course_01">1</td>
<td rowspan="5" class="horseNum">1</td>
<td colspan="3"><a class="horseName" href="../DataRoom/HorseMarkInfo?k_lineageLoginCode=30006401886">ワイドマルガリータ</a></td>
</tr>
<tr class="tBorder">
<td rowspan="5" class="courseNum course_02">2</td>
<td rowspan="5" class="horseNum">2</td>
<td colspan="3"><a class="horseName" href="../DataRoom/HorseMarkInfo?k_lineageLoginCode=30062400996">シアトルプリンセス</a></td>
</tr>
"""

# 競走成績 (spacer 空セルを挟んだ実構造を模す): 論理列で date/距離/馬場/着順/タイム
_HISTORY = """
<table>
<tr><th>年月日</th><th>競馬場</th><th>R</th><th>競走名</th><th>格組</th><th>距離</th>
    <th>天候</th><th>馬場</th><th>頭数</th><th>枠</th><th>馬番</th><th>人気</th>
    <th>着順</th><th>タイム</th><th>差</th></tr>
<tr><td>2026/05/11</td><td>盛岡</td><td>5</td><td>Ｃ２四組</td><td></td><td>Ｃ２四組</td><td>1400</td>
    <td>晴</td><td></td><td>良</td><td>9</td><td>7</td><td></td><td>7</td><td>3</td>
    <td>2</td><td>1:27.4</td><td>0.2</td></tr>
<tr><td>2026/04/20</td><td>水沢</td><td>5</td><td>Ｃ２五組</td><td></td><td>Ｃ２五組</td><td>1300</td>
    <td>晴</td><td></td><td>良</td><td>11</td><td>8</td><td></td><td>10</td><td>8</td>
    <td>5</td><td>1:26.2</td><td>1.0</td></tr>
</table>
"""


def test_parse_deba_table_horse_ids():
    d = kg.parse_deba_table(_DEBA)
    assert d == [(1, "ワイドマルガリータ", "30006401886"),
                 (2, "シアトルプリンセス", "30062400996")]


def test_parse_deba_table_missing_link_no_mispair():
    """リンク無し馬 (取消等) があっても、その馬番を次の馬の ID と誤ペアにしない。

    馬柱を別馬に付ける最悪の取り違え回帰防止。リンク無し馬は code 空で残す。
    """
    html = (
        '<td rowspan="5" class="horseNum">1</td>'
        '<a class="horseName" href="x?k_lineageLoginCode=111">ホースA</a>'
        '<td rowspan="5" class="horseNum">2</td>'   # 取消 = リンク無し
        '<td rowspan="5" class="horseNum">3</td>'
        '<a class="horseName" href="x?k_lineageLoginCode=333">ホースC</a>'
    )
    assert kg.parse_deba_table(html) == [
        (1, "ホースA", "111"),
        (2, "", ""),          # 次の馬の ID を借りない
        (3, "ホースC", "333"),  # 落とさない
    ]


def test_parse_combo_rejects_date_like():
    """日付 (2026-05-26) 等の馬番域外の組番を弾く (誤った組/オッズを作らない)。"""
    html = "<td>2026-05-26</td><td>5.0</td><td>1-2-3</td><td>10.0</td>"
    tri = {t.label: t.odds for t in kg.parse_trifecta(html)}
    assert tri == {"1-2-3": 10.0}        # 2026-05-26 は採用されない
    q = {b.label: b.odds for b in kg._parse_combo(html, "trio", ordered=False, length=3)}
    assert q == {"1-2-3": 10.0}


def test_parse_horse_history_fields():
    runs = kg.parse_horse_history(_HISTORY)
    assert len(runs) == 2
    r = runs[0]
    assert r.date == "2026.05.11" and r.venue == "盛岡"
    assert r.distance == 1400 and r.going == "良" and r.field_size == 9
    assert r.finish_pos == 2                      # 1/2/3 は int
    assert abs(r.winner_time_sec - 87.4) < 1e-6   # 1:27.4 → 87.4s
    assert runs[1].finish_pos is None             # 着順 5 は None (3着内のみ int)


def test_parse_horse_history_jra_layout_row():
    """1頭の履歴に混在する JRA 行 (距離='芝2000'・列数多い) を列ずれなく取れる。

    固定 index だと距離='曇'・タイム=着差 を誤採用していた回帰の防止 (値パターン錨)。
    """
    jra_row = (
        "<table><tr>"
        "<td>2024/06/30</td><td>Ｊ函館</td><td>6</td><td>３歳未勝利</td>"
        "<td>芝2000</td><td>曇</td><td>良</td><td>16</td><td>4</td><td>7</td>"
        "<td>14</td><td>10</td><td>2:03.0</td><td>1.3</td><td>36.9</td>"
        "<td>464</td><td>高杉吏</td><td>52.0</td><td>西園正</td><td>0</td><td>サラトガ</td>"
        "</tr></table>"
    )
    runs = kg.parse_horse_history(jra_row)
    assert len(runs) == 1
    r = runs[0]
    assert r.surface == "芝" and r.distance == 2000   # '芝2000' を正しく分解
    assert r.going == "良" and r.field_size == 16
    assert r.finish_pos is None                        # 着順 10 (3着外)
    assert abs(r.winner_time_sec - 123.0) < 1e-6       # 2:03.0、着差 1.3 を誤採用しない


def test_time_sec_and_date_key():
    assert abs(kg._time_sec("1:27.4") - 87.4) < 1e-9
    assert abs(kg._time_sec("41.9") - 41.9) < 1e-9
    # 非ゼロ詰めでも正しく順序付く (leakage 比較の回帰防止)
    assert kg._date_key("2026.5.2") < kg._date_key("2026.05.26")
