"""西田式スピード指数 (簡易版)。

公式:
  SI = (基準タイム − 自走破タイム) × 距離指数 + 馬場指数 + (斤量 − 55) × 2 + クラス指数 + 80

基準タイム:
  本格運用では「過去 3 年 × クラス × 場 × 距離 × サーフェス」の平均を使うが、
  本実装ではまず暫定として **同 (サーフェス, 距離) の代表値テーブル** を使う。
  後で `data/base_time/<surface>_<dist>.json` に置き換え可能にする。

距離指数 / クラス指数 / 馬場指数の係数は西田式の公開ソース
(team-d.club, walkintheforest.net 等) に準拠。

参考: https://team-d.club/speed-index/about-speed-index/
      https://walkintheforest.net/original-speed-index-1/

Notes:
  - 「指数」は大きいほど良い (80 を基準、+1 ≒ 0.2 秒のタイム差相当)
  - JRA / NAR で基準タイムは大きく違う (NAR は遅め) → 場別補正は base_time が吸収
"""
from __future__ import annotations

from .models import PastRun


# --- 距離指数 (秒→指数換算係数。距離が短いほど 1 秒差が大きい指数差になる) ---
# 西田式 CrossFactor 版。芝とダートで微妙に異なる。
_DISTANCE_FACTOR_TURF: list[tuple[int, float]] = [
    (1000, 1.80), (1200, 1.45), (1400, 1.22), (1600, 1.06),
    (1800, 0.93), (2000, 0.83), (2200, 0.74), (2400, 0.68),
    (2500, 0.65), (3000, 0.53), (3200, 0.50), (3600, 0.45),
]
_DISTANCE_FACTOR_DIRT: list[tuple[int, float]] = [
    (1000, 1.70), (1150, 1.50), (1200, 1.39), (1400, 1.18),
    (1600, 1.02), (1700, 0.95), (1800, 0.88), (2000, 0.79),
    (2400, 0.64), (3000, 0.50),
]


def _interp_distance_factor(distance: int, surface: str) -> float:
    """距離指数の線形補間。範囲外は端を使う。"""
    table = _DISTANCE_FACTOR_DIRT if surface == "ダート" else _DISTANCE_FACTOR_TURF
    if distance <= table[0][0]:
        return table[0][1]
    if distance >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        d1, f1 = table[i]
        d2, f2 = table[i + 1]
        if d1 <= distance <= d2:
            return f1 + (f2 - f1) * (distance - d1) / (d2 - d1)
    return 1.0


# --- クラス指数 (出走したクラスの強さ補正、高いほど強いクラス) ---
# JRA / NAR 共通の粗い分類。フルマッチではなく部分一致で判定。
def class_index(race_class: str) -> int:
    """レースクラス名から指数を返す。判定不能は 0。"""
    if not race_class:
        return 0
    s = race_class
    # JRA 重賞
    if "G1" in s or "GⅠ" in s or "Jpn1" in s or "JpnⅠ" in s:
        return 20
    if "G2" in s or "GⅡ" in s or "Jpn2" in s or "JpnⅡ" in s:
        return 17
    if "G3" in s or "GⅢ" in s or "Jpn3" in s or "JpnⅢ" in s:
        return 14
    if "L)" in s or "(L" in s:
        return 11
    if "OP" in s or "オープン" in s or "オープン特別" in s:
        return 10
    if "3勝" in s:
        return 8
    if "2勝" in s:
        return 3
    if "1勝" in s:
        return 0
    if "新馬" in s or "未勝利" in s:
        return -3
    # NAR クラス
    if "(A1)" in s or "A1" in s:
        return 8
    if "(A2)" in s or "A2" in s:
        return 5
    if "(A3)" in s or "A3" in s:
        return 3
    if "(B1)" in s or "B1" in s:
        return 0
    if "(B2)" in s or "B2" in s:
        return -2
    if "(B3)" in s or "B3" in s:
        return -4
    if "(C1)" in s or "C1" in s:
        return -6
    if "(C2)" in s or "C2" in s:
        return -8
    if "(C3)" in s or "C3" in s:
        return -10
    return 0


# --- 馬場指数 (馬場状態によるタイム補正) ---
# 良基準。重・不良ほどタイムが落ちるので +補正する (= 同じタイムでも価値が高い)
_GOING_INDEX_TURF = {
    "良": 0,
    "稍": 3, "稍重": 3,
    "重": 6,
    "不": 9, "不良": 9,
}
_GOING_INDEX_DIRT = {
    "良": 0,
    "稍": -2, "稍重": -2,  # ダートは脚場が締まって速くなることが多い
    "重": -4,
    "不": -5, "不良": -5,
}


def going_index(going: str, surface: str) -> int:
    table = _GOING_INDEX_DIRT if surface == "ダート" else _GOING_INDEX_TURF
    return table.get(going, 0)


# --- 基準タイム (Tier 0: 暫定ハードコード) ---
# JRA + NAR で大きく違うので surface × distance のみで持ち、場の補正は後で base_time で。
# 値は「中央 1 勝クラス 良馬場 良走破タイム」の概算 (秒)。NAR は別途 venue 補正をかける。
_BASE_TIME_TURF: dict[int, float] = {
    1000: 56.0, 1200: 68.5, 1400: 81.0, 1600: 93.5,
    1800: 106.5, 2000: 119.5, 2200: 132.5, 2400: 145.0,
    2500: 152.0, 3000: 184.0, 3200: 196.5,
}
_BASE_TIME_DIRT: dict[int, float] = {
    1000: 58.5, 1150: 67.0, 1200: 70.5, 1300: 77.0,
    1400: 84.5, 1600: 96.0, 1700: 102.5, 1800: 109.5, 2000: 122.5,
}

# NAR 場ごとのタイム補正 (秒)。基準は JRA。
# 大井・川崎・船橋などは砂が重く JRA よりタイムがかかる。+ 秒 = JRA より遅い。
_NAR_VENUE_OFFSET: dict[str, float] = {
    "門別": 2.0, "盛岡": 2.5, "水沢": 3.0,
    "浦和": 2.0, "船橋": 2.0, "大井": 2.5, "川崎": 3.0,
    "金沢": 3.0, "笠松": 2.5, "名古屋": 2.5,
    "園田": 3.0, "姫路": 3.0,
    "高知": 3.0, "佐賀": 2.5,
    "帯広": 0.0,  # ばんえいは別物
}


def _base_time(surface: str, distance: int, venue: str = "") -> float:
    """基準タイム (秒)。NAR 場は補正を加算。"""
    table = _BASE_TIME_DIRT if surface == "ダート" else _BASE_TIME_TURF
    if not table:
        return 0.0
    # 完全一致 / 線形補間
    if distance in table:
        bt = table[distance]
    else:
        keys = sorted(table.keys())
        if distance <= keys[0]:
            bt = table[keys[0]]
        elif distance >= keys[-1]:
            bt = table[keys[-1]]
        else:
            # 線形補間
            for i in range(len(keys) - 1):
                d1, d2 = keys[i], keys[i + 1]
                if d1 <= distance <= d2:
                    f = (distance - d1) / (d2 - d1)
                    bt = table[d1] + (table[d2] - table[d1]) * f
                    break
            else:
                bt = table[keys[-1]]
    bt += _NAR_VENUE_OFFSET.get(venue, 0.0)
    return bt


# --- スピード指数 ---


def speed_index(past: PastRun) -> float:
    """1 走分のスピード指数を返す。データ欠損時は 0.0。"""
    own = past.own_time_sec
    if own <= 0 or past.distance <= 0 or not past.surface:
        return 0.0
    bt = _base_time(past.surface, past.distance, past.venue)
    if bt <= 0:
        return 0.0
    df = _interp_distance_factor(past.distance, past.surface)
    gi = going_index(past.going, past.surface)
    ci = class_index(past.race_class)
    weight_adj = (past.weight_kg - 55.0) * 2.0 if past.weight_kg > 0 else 0.0
    return (bt - own) * df + gi + weight_adj + ci + 80.0


def best_speed_index(runs: list[PastRun]) -> float:
    """直近走の中での最大指数。データなしは 0.0。"""
    if not runs:
        return 0.0
    vals = [speed_index(r) for r in runs]
    vals = [v for v in vals if v > 0]
    return max(vals) if vals else 0.0


def mean_recent_speed_index(runs: list[PastRun], n: int = 3) -> float:
    """直近 n 走の単純平均。"""
    if not runs:
        return 0.0
    vals = [speed_index(r) for r in runs[:n]]
    vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else 0.0


def weighted_recent_speed_index(runs: list[PastRun], weights: tuple[float, ...] = (0.5, 0.3, 0.2)) -> float:
    """直近 N 走 (len(weights)) を重み付け平均。weights は新しい走から。"""
    if not runs or not weights:
        return 0.0
    n = min(len(runs), len(weights))
    num = 0.0
    den = 0.0
    for i in range(n):
        v = speed_index(runs[i])
        if v <= 0:
            continue
        num += v * weights[i]
        den += weights[i]
    return num / den if den > 0 else 0.0
