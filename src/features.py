"""Layer 1: 出馬表 + 馬柱から 1 馬あたり数値特徴量を構築する。

役割:
  RaceData (Horse + PastRun リスト) → { 馬番: FeatureVec }
  FeatureVec は Layer 2 (strength.py) の入力になる。

哲学:
  - 生涯 1 着率 (h.win_rate) は使わない。代わりに「距離・サーフェス条件付き
    shrinkage 勝率」(priors.conditional_shrunk_rate) を使う。
  - スピード指数は「直近 3 走重み付け」と「直近最良」の 2 系統。
    重み付けが安定の主軸、最良はトップエンド能力の補助。
  - 上がり 3F 指数化 (距離で標準化) → 末脚評価。
  - 騎手 / 厩舎統計は外部データが必要なので Phase A では skip。Phase B で追加。
  - 馬体重大幅変動 (±10kg 超) はフラグ化。
  - データ欠損は NaN ではなく 0.0 / False で返し、後段 (strength) が解釈する。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .models import Horse, PastRun, RaceData
from .priors import (
    DEFAULT_DISTANCE_SIGMA,
    conditional_shrunk_rate,
    effective_sample_size,
)
from .speed_index import (
    best_speed_index,
    mean_recent_speed_index,
    speed_index,
    weighted_recent_speed_index,
)


@dataclass
class FeatureVec:
    """1 馬の数値特徴量ベクトル。"""
    number: int                          # 馬番
    # スピード指数系
    speed_idx_weighted: float = 0.0      # 直近 3 走の重み付け平均
    speed_idx_best: float = 0.0          # 直近 5 走中最良
    speed_idx_last: float = 0.0          # 直近 1 走
    # shrinkage 勝率 (当該距離・サーフェス条件付き)
    shrunk_win_rate: float = 0.0
    shrunk_show_rate: float = 0.0
    effective_starts: float = 0.0        # 条件マッチ effective sample size
    # 距離・コース適性
    same_distance_count: int = 0         # 同距離 ±100m の過去走数 (= 適性経験)
    same_surface_count: int = 0
    same_venue_count: int = 0            # 当該場での過去走数
    # 馬場状態適性 (current race の going が分かる時のみ意味を持つ)
    same_going_count: int = 0            # 同馬場状態 (良/稍/重/不) での過去走数
    same_going_show_rate: float = 0.0    # 同馬場状態での 3 着以内率 (0-1)
    going_versatility: float = 0.0       # 異馬場での好走多様性 (0-1, 高いほど馬場不問)
    # 末脚
    last3f_idx_recent: float = 0.0       # 直近 3 走の上がり 3F を距離で標準化
    # コンディション
    days_since_last_run: int = 0         # 休み明け日数 (>30 = 休み明け)
    body_weight: int = 0                 # 当日馬体重
    body_weight_diff: int = 0            # 増減
    big_weight_change: bool = False      # ±10kg 超
    # 騎手乗り替わり
    jockey_change: bool = False          # 直近走と騎手が違う
    # 脚質 / ペース
    style_score: float = 0.0             # -1 (追込) .. +1 (逃げ) — 過去 5 走の通過順 1 コーナーから推定
    pace_fit: float = 0.0                # 当該レースの想定ペースとの整合性 (-1..+1)
    # ベース情報
    weight_kg: float = 0.0               # 今日の斤量
    win_odds: float = 0.0                # 今日の単勝オッズ (P×O で使う側)
    absent: bool = False


# --- 脚質 / pace projection ---

def estimate_style_score(past_runs: list[PastRun]) -> float:
    """過去走の通過順から脚質スコアを推定。

    通過順 "12-14" のような文字列を分解、1 コーナーの位置を見て:
      1 コーナー位置 / 出走頭数 → 0.0 (大逃げ) .. 1.0 (最後方)
    これを -1 (大逃げ) .. +1 (追込) にマッピングして直近 5 走の平均を取る。
    返り値の符号:
      style_score < 0  → 前 (逃げ・先行) 寄り
      style_score > 0  → 後ろ (差し・追込) 寄り
    """
    if not past_runs:
        return 0.0
    vals: list[float] = []
    for r in past_runs:
        if not r.passing or r.field_size <= 0:
            continue
        # 1 コーナーの位置を取り出す (最初の数字)
        m = _RE_FIRST_NUM.match(r.passing)
        if not m:
            continue
        pos = int(m.group(1))
        normalized = (pos - 1) / max(r.field_size - 1, 1)  # 0..1
        # 0 (先頭) → -1 (逃げ), 1 (最後尾) → +1 (追込)
        vals.append(2.0 * normalized - 1.0)
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def estimate_pace_fit(style_score: float, race_pace_score: float) -> float:
    """当該馬の脚質と当該レース想定ペースの整合度。

    race_pace_score:
      < 0 (ハイペース予想、逃げ多い) → 差し・追込 馬有利 → style_score > 0 で +
      > 0 (スローペース予想、逃げ少ない) → 先行・逃げ有利 → style_score < 0 で +
    fit = -style_score × race_pace_score (内積の符号反転 = 逆向きで +EV)
    """
    return -style_score * race_pace_score


def estimate_race_pace_score(all_style_scores: list[float]) -> float:
    """レース全馬の style_score 分布からペース傾向を推定。

    逃げ馬 (style < -0.5) 数を数える:
      0 頭 → スローペース (race_pace_score = +1)
      1 頭 → 平常 (0)
      2+ 頭 → ハイペース (-1)
    """
    n_runaways = sum(1 for s in all_style_scores if s < -0.5)
    if n_runaways == 0:
        return 1.0
    if n_runaways == 1:
        return 0.0
    return -1.0


_RE_FIRST_NUM = re.compile(r"^(\d+)")


# --- 末脚指数 (上がり 3F の距離標準化) ---
def normalize_last3f(seconds: float, distance: int, surface: str) -> float:
    """上がり 3F を距離で標準化して比較可能にする。

    Yurelu / Zenn の式 (距離標準化スピード):
      芝: 上がり3F / (0.94 + 距離/20000)
      ダート: 上がり3F / (1.01 + 距離/20000)
    """
    if seconds <= 0 or distance <= 0:
        return 0.0
    if surface == "ダート":
        denom = 1.01 + distance / 20000.0
    else:
        denom = 0.94 + distance / 20000.0
    return seconds / denom


def _normalize_going(s: str) -> str:
    """馬場状態文字列を 1 文字 (良/稍/重/不) に正規化。空文字は ""。

    変換例:
      "良" → "良"
      "稍重" / "稍" → "稍"
      "重" → "重"
      "不良" / "不" → "不"
    """
    if not s:
        return ""
    s = s.strip()
    if s.startswith("良"):
        return "良"
    if s.startswith("稍"):
        return "稍"
    if s.startswith("重"):
        return "重"
    if s.startswith("不"):
        return "不"
    return ""


def _parse_jp_date(s: str) -> tuple[int, int, int] | None:
    """`2026.04.11` → (2026, 4, 11) または None。"""
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _days_between(d1: tuple[int, int, int], d2: tuple[int, int, int]) -> int:
    """簡易日数差 (1 か月 30 日近似)。完全な暦は使わない (UTC ライブラリで OK だが不要)。"""
    from datetime import date
    try:
        return abs((date(*d1) - date(*d2)).days)
    except ValueError:
        return 0


def build_features(rd: RaceData) -> dict[int, FeatureVec]:
    """RaceData (Horse に past_runs が入っている前提) から特徴量を構築。"""
    race = rd.race
    today = None
    if race.start_at:
        from datetime import datetime
        d = datetime.fromtimestamp(race.start_at)
        today = (d.year, d.month, d.day)

    # 先に全馬の脚質スコアを取って race-level ペースを推定
    style_scores: dict[int, float] = {}
    for h in race.horses:
        if h.absent:
            continue
        style_scores[h.number] = estimate_style_score(h.past_runs or [])
    race_pace = estimate_race_pace_score(list(style_scores.values()))

    out: dict[int, FeatureVec] = {}
    for h in race.horses:
        runs = h.past_runs or []
        fv = FeatureVec(
            number=h.number,
            weight_kg=h.weight_kg,
            win_odds=h.win_odds,
            absent=h.absent,
            body_weight=h.body_weight,
            body_weight_diff=h.body_weight_diff,
            big_weight_change=abs(h.body_weight_diff) >= 10,
            style_score=style_scores.get(h.number, 0.0),
            pace_fit=estimate_pace_fit(style_scores.get(h.number, 0.0), race_pace),
        )

        # スピード指数 3 系統
        fv.speed_idx_weighted = weighted_recent_speed_index(runs)
        fv.speed_idx_best = best_speed_index(runs)
        fv.speed_idx_last = speed_index(runs[0]) if runs else 0.0

        # 条件付き shrinkage 勝率
        fv.shrunk_win_rate = conditional_shrunk_rate(
            runs, target_distance=race.distance, target_surface=race.surface, metric="win"
        )
        fv.shrunk_show_rate = conditional_shrunk_rate(
            runs, target_distance=race.distance, target_surface=race.surface, metric="show"
        )
        fv.effective_starts = effective_sample_size(
            runs, target_distance=race.distance, target_surface=race.surface
        )

        # 距離・コース適性 (経験数)
        fv.same_distance_count = sum(
            1 for r in runs if abs(r.distance - race.distance) <= 100 and r.surface == race.surface
        )
        fv.same_surface_count = sum(1 for r in runs if r.surface == race.surface)
        fv.same_venue_count = sum(1 for r in runs if r.venue == race.venue_name)

        # 馬場状態適性 (current race の going が分かる時のみ意味を持つ)
        # Race.weather.track_condition は "良"/"稍重"/"重"/"不良" の文字列。
        # 過去走の PastRun.going は "良"/"稍"/"重"/"不" のような短縮形が来る場合あり。
        # 部分一致で判定する。
        current_going = (race.weather.track_condition if race.weather else "") or ""
        # going を 1 文字に正規化 (良/稍/重/不)
        cg_norm = _normalize_going(current_going)
        if cg_norm:
            same_going_runs = [r for r in runs if _normalize_going(r.going) == cg_norm]
            fv.same_going_count = len(same_going_runs)
            shows = sum(1 for r in same_going_runs if r.finish_pos in (1, 2, 3))
            fv.same_going_show_rate = (
                shows / len(same_going_runs) if same_going_runs else 0.0
            )
        # 馬場多様性: 過去走で良/稍/重/不の何種類で 3 着以内に入ったか / 4
        diverse = {
            _normalize_going(r.going)
            for r in runs if r.finish_pos in (1, 2, 3) and _normalize_going(r.going)
        }
        fv.going_versatility = len(diverse) / 4.0 if diverse else 0.0

        # 末脚指数: 直近 3 走の標準化上がり 3F の平均
        last3f_vals: list[float] = []
        for r in runs[:3]:
            v = normalize_last3f(r.last_3f_sec, r.distance, r.surface)
            if v > 0:
                last3f_vals.append(v)
        fv.last3f_idx_recent = sum(last3f_vals) / len(last3f_vals) if last3f_vals else 0.0

        # 休み明け日数 (直近走と今日の差)
        if today and runs:
            d_last = _parse_jp_date(runs[0].date)
            if d_last:
                fv.days_since_last_run = _days_between(today, d_last)

        # 騎手乗り替わり
        if runs:
            fv.jockey_change = bool(runs[0].jockey) and runs[0].jockey != h.jockey_name

        out[h.number] = fv
    return out


def feature_summary(features: dict[int, FeatureVec]) -> str:
    """デバッグ用テキスト出力。"""
    lines = ["#  | SI_w  SI_b  SI_l | shrunk W% / SH% (n_eff) | last3f_idx | rest_d | change | abs"]
    for n in sorted(features):
        f = features[n]
        lines.append(
            f"{n:2d} | {f.speed_idx_weighted:5.1f} {f.speed_idx_best:5.1f} {f.speed_idx_last:5.1f}"
            f" | {f.shrunk_win_rate*100:5.2f}%  {f.shrunk_show_rate*100:5.2f}% ({f.effective_starts:4.2f})"
            f" | {f.last3f_idx_recent:5.2f}"
            f" | {f.days_since_last_run:4d}d"
            f" | {'J' if f.jockey_change else '-'}{'W' if f.big_weight_change else '-'}"
            f" | {'X' if f.absent else '-'}"
        )
    return "\n".join(lines)
