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
