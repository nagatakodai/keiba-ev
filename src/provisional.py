"""仮指数 (provisional index) — 公式出走表だけから決定論的に付ける 0-100 の順位付け。

二段パイプラインの第一段 (ユーザ指示 2026-07-01):
  ①公式出走表(NAR keibago / JRA公式 / netkeiba 馬柱)の past_runs + 出走条件だけから、
    **固定ロジック**で各馬の「仮指数」を出す (= Claude が調整する叩き台)。
  ②Claude が出走表以外の情報(検索: パドック/直前/軟情報/騎手)で ± 調整して最終指数にする。

## 絶対制約 — 市場人気に一切触れない
本モジュールは win_odds / 単勝オッズ / 人気 / PastRun.popularity(過去走の人気=過去市場) を
**一切参照しない**。使う入力は全て市場非依存: 走破タイム(own_time_sec)・着差(time_diff_sec)・
上がり(last_3f_sec)・着順(finish_pos)・距離/馬場/クラス/頭数/斤量。aptitude.py と同じ規律で、
LightGBM fundamental (past popularity_outperformance を含む) は使わない。

## 因子 (7・適性フィット厚め, ユーザ選択 2026-07-01)
  similar_race(0.26) 似た条件で勝っていたか / distance(0.16) 長短距離適性 /
  going(0.14) 良/重/不 馬場適性 / form_momentum(0.16) 連勝・上昇度(調子の波) /
  base_speed(0.14) 絶対能力(西田式SI) / performance_quality(0.08) 着差×相手強度の質 /
  class_context(0.06) クラス昇降・格の通用。
着順(finish_pos は 1/2/3 のみ)より連続量の時計(SI/着差/上がり)を主軸にし、少頭数NARでも
1-2走から効く。過去走の薄い馬はベイズ縮小し、**過去走ゼロの馬は中立50** (最弱に落とさない)。

## 正規化
各因子の raw を「レース内ロバスト正規化」(median/MAD → logistic squash) で 0-100 化。
仮指数 = 計算できた因子だけの加重平均 (欠損因子は分母から除外し薄めない)。過去走が全く
無い馬は 50 (中立 anchor)。
"""
from __future__ import annotations

import math
from statistics import median
from typing import Optional

from .models import PastRun, RaceData
from .speed_index import class_index, speed_index

# ── 因子の重み (合計 1.00) ────────────────────────────────────────────
# 初期は「適性フィット厚め」(ユーザ選択) だったが、実結果バックテスト (scripts/provisional_validity.py,
# 700R) の因子別 AUC で **performance_quality(着差の質)=0.728 / form_momentum(連勝)=0.700 が最強、
# class_context=0.502≒ランダム、similar_race=0.605/distance=0.563 は中位** と判明。適性フィット
# (similar/distance/going=0.45) を残しつつ強因子へ寄せ・class をノイズ床(0.03)に落とした
# 「現行×AUC比例」の 50/50 ブレンド (2026-07-01)。in-sample top1 は 24.4%→~26%。要 OOS 再検証。
WEIGHTS: dict[str, float] = {
    "similar_race": 0.19,        # 似レース好走 (AUC 0.605)
    "form_momentum": 0.20,       # 連勝・上昇度 (AUC 0.700)
    "performance_quality": 0.17,  # 着差×相手強度の質 (AUC 0.728 = 最強)
    "going": 0.15,               # 馬場適性 (AUC 0.632)
    "base_speed": 0.15,          # 絶対能力=西田式SI (AUC 0.630)
    "distance": 0.11,            # 距離適性 (AUC 0.563)
    "class_context": 0.03,       # クラス昇降 (AUC 0.502≒ノイズ → 昇降の希少ケース用に床のみ)
}

_RECENT = 6            # 各因子で見る過去走の上限 (直近6走)
_RECENCY = 0.72        # index ベース減衰 (runs[0]=最新)。日付非依存で決定論。
NEUTRAL = 50.0         # 過去走ゼロ / 情報無し馬の中立 anchor


def _going_sev(going: str) -> Optional[int]:
    """馬場状態 → 重さ 良=0/稍=1/重=2/不=3。判定不能は None。

    "稍重" は "稍" を先に判定して 1 にする (順序が重要: "重" を先に見ると稍重が 2 になる)。
    """
    if not going:
        return None
    if "不" in going:      # 不良
        return 3
    if "稍" in going:      # 稍重
        return 1
    if "重" in going:      # 重
        return 2
    if "良" in going:      # 良
        return 0
    return None


def _today_going(rd: RaceData) -> str:
    """当該レースの馬場状態文字列 (weather.track_condition 優先, 無ければ weather_text の '/ 良')。"""
    w = getattr(rd.race, "weather", None)
    if w is not None and getattr(w, "track_condition", ""):
        return w.track_condition
    txt = getattr(rd.race, "weather_text", "") or ""
    if "/" in txt:
        return txt.split("/")[-1].strip()
    return txt.strip()


def _recency(i: int) -> float:
    return _RECENCY ** i


def _si_runs(h) -> list[tuple[int, PastRun, float]]:
    """(index, run, SI) で SI>0 の直近走のみ (時計が取れた走)。"""
    out = []
    for i, r in enumerate((h.past_runs or [])[:_RECENT]):
        si = speed_index(r)
        if si > 0:
            out.append((i, r, si))
    return out


def _run_goodness(r: PastRun) -> float:
    """1走の『どれだけ走ったか』を [0,1] 連続で。着順(1/2/3)優先、4着以下は着差で近似。"""
    fp = r.finish_pos
    if fp == 1:
        return 1.0
    if fp in (2, 3):
        return 0.7
    td = r.time_diff_sec
    if td and td > 0:
        return max(0.0, 0.5 - td / 3.0)   # 勝ち馬から 0.5s 差で ~0.33, 1.5s で 0
    return 0.3   # 着順・着差とも不明


# ── 各因子の raw (市場非依存)。データが無い馬は None を返す ──────────────

def _base_speed_raw(h, ctx) -> Optional[float]:
    """絶対能力: 西田式SI の近走減衰加重(65%) + 自己最高(35%)。"""
    runs = _si_runs(h)
    if not runs:
        return None
    num = den = 0.0
    for i, _r, si in runs:
        w = _recency(i)
        num += w * si
        den += w
    mean_form = num / den
    peak = max(si for _i, _r, si in runs)
    return 0.65 * mean_form + 0.35 * peak


def _similar_race_raw(h, ctx) -> Optional[float]:
    """似レース好走: 今日と似た条件(芝ダ/距離/格/馬場)の走を相似度加重し、勝ち/好走を上乗せ。"""
    runs = _si_runs(h)
    if not runs:
        return None
    d0, surf0, ci0, sev0 = ctx["dist"], ctx["surf"], ctx["class"], ctx["sev"]
    num = den = 0.0
    for i, r, si in runs:
        surf_k = 1.0 if (surf0 and r.surface == surf0) else (
            0.0 if ("障" in (r.surface or "")) != ("障" in (surf0 or "")) else 0.15)
        dist_k = math.exp(-((r.distance - d0) / 300.0) ** 2) if (d0 and r.distance) else 0.5
        class_k = math.exp(-abs(class_index(r.race_class) - ci0) / 4.0)
        sev_i = _going_sev(r.going)
        going_k = math.exp(-abs(sev_i - sev0) / 1.5) if (sev_i is not None and sev0 is not None) else 0.8
        sim = surf_k * dist_k * class_k * going_k
        bonus = 1.0 + 0.5 * (r.finish_pos == 1) + 0.25 * (r.finish_pos in (2, 3))
        w = sim * _recency(i)
        num += w * si * bonus
        den += w
    return num / (den + 0.8)   # 加法平滑 k0=0.8: 有効走が薄い馬は控えめに


def _distance_raw(h, ctx) -> Optional[float]:
    """距離適性: 今日の距離帯で出せる時計(距離ガウス加重SI) × 経験外距離ペナルティ。"""
    runs = _si_runs(h)
    if not runs:
        return None
    d0, surf0 = ctx["dist"], ctx["surf"]
    if not d0:
        return None
    num = den = 0.0
    raced = []
    for i, r, si in runs:
        if not r.distance:
            continue
        p = math.exp(-((r.distance - d0) / 300.0) ** 2)
        p *= 1.0 if (surf0 and r.surface == surf0) else 0.3   # 別surfaceは割引
        num += p * si
        den += p
        raced.append(r.distance)
    if den <= 0 or not raced:
        return None
    core = num / (den + 0.8)
    out = max(0.0, d0 - max(raced), min(raced) - d0)   # 経験レンジ外の延長/短縮
    range_pen = 1.0 - min(0.30, out / 1000.0)
    return core * range_pen


def _going_raw(h, ctx) -> Optional[float]:
    """馬場適性: 今日の馬場区分で見込めるSIを、同馬場走→個体平均へベイズ縮小 + 道悪巧者。"""
    runs = _si_runs(h)
    if not runs:
        return None
    sev0 = ctx["sev"]
    own_base = median([si for _i, _r, si in runs])
    if sev0 is None:
        return own_base
    same = [si for _i, r, si in runs if _going_sev(r.going) == sev0]
    K = 1.5
    raw = ((sum(same) + K * own_base) / (len(same) + K)) if same else own_base
    # 道悪 (稍重以上) + 過去に道悪勝ちがあれば『道悪巧者』ボーナス
    if sev0 >= 2:
        for _i, r, _si in runs:
            s = _going_sev(r.going)
            if s is not None and s >= 2 and r.finish_pos == 1:
                raw += 4.0
                break
    return raw


def _form_momentum_raw(h, ctx) -> Optional[float]:
    """連勝・上昇度(調子の波): 直近好走 level + 連勝/連対ストリーク + 着差の時系列勾配。"""
    runs = (h.past_runs or [])[:_RECENT]
    if not runs:
        return None
    # (A) level: 直近3走の減衰加重 goodness
    num = den = 0.0
    for i, r in enumerate(runs[:3]):
        w = _recency(i)
        num += w * _run_goodness(r)
        den += w
    level = num / den if den else 0.0
    # (B) streak: 最新から連続の 1着数 / 3着内数
    sw = ss = 0
    for r in runs:
        if r.finish_pos == 1:
            sw += 1
        else:
            break
    for r in runs:
        if r.finish_pos in (1, 2, 3):
            ss += 1
        else:
            break
    streak = min(sw, 3) * 0.12 + min(ss, 3) * 0.05
    # (C) trend: 着差の時系列勾配 (小さくなる=上昇=正)。時系列昇順で OLS。
    pts = [(idx, r.time_diff_sec) for idx, r in enumerate(reversed(runs))
           if r.time_diff_sec is not None and (r.time_diff_sec > 0 or r.finish_pos == 1)]
    trend = 0.0
    if len(pts) >= 3:
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
        if sxx > 0:
            slope = sxy / sxx
            trend = max(-0.3, min(0.3, -slope))   # slope<0 (着差縮小) → trend>0
    return 0.55 * level + streak + (trend + 0.3) / 0.6 * 0.15


def _perf_quality_raw(h, ctx) -> Optional[float]:
    """着差×相手強度の質: 勝ち馬からの僅差 × 多頭数/格上補正 × par比の上がり。"""
    runs = (h.past_runs or [])[:_RECENT]
    ci0 = ctx["class"]
    num = den = 0.0
    for i, r in enumerate(runs):
        w = _recency(i)
        terms = []
        wts = []
        td = r.time_diff_sec
        if td is not None and (td > 0 or r.finish_pos == 1):
            terms.append(math.exp(-max(0.0, td) / 0.8)); wts.append(0.5)
        if r.field_size and r.field_size > 0:
            field_q = (math.log(max(r.field_size, 2)) / math.log(12.0)) * \
                max(0.0, min(1.0, (class_index(r.race_class) - ci0 + 10) / 20.0))
            terms.append(field_q); wts.append(0.35)
        if r.last_3f_sec and r.last_3f_sec > 0 and r.surface:
            par = 34.5 if "芝" in r.surface else 37.0
            terms.append(max(-1.0, min(1.0, (par - r.last_3f_sec) / 3.0)) * 0.5 + 0.5)
            wts.append(0.15)
        if not terms:
            continue
        q = sum(t * wt for t, wt in zip(terms, wts)) / sum(wts)
        num += w * q
        den += w
    if den <= 0:
        return None
    return num / den


def _class_context_raw(h, ctx) -> Optional[float]:
    """クラス昇降・格の通用: 連対以内で通用した最高格 vs 今日の格 + 今の格帯への馴染み。"""
    runs = (h.past_runs or [])[:_RECENT]
    if not runs:
        return None
    ci0 = ctx["class"]
    placed = [class_index(r.race_class) for r in runs if r.finish_pos in (1, 2, 3)]
    allc = [class_index(r.race_class) for r in runs]
    proven = max(placed) if placed else (max(allc) if allc else ci0)
    move = max(-8.0, min(8.0, proven - ci0))
    fit_frac = sum(1 for c in allc if abs(c - ci0) <= 3) / len(allc) if allc else 0.0
    return 10.0 + move + 4.0 * fit_frac


_FACTORS = {
    "base_speed": _base_speed_raw,
    "similar_race": _similar_race_raw,
    "distance": _distance_raw,
    "going": _going_raw,
    "form_momentum": _form_momentum_raw,
    "performance_quality": _perf_quality_raw,
    "class_context": _class_context_raw,
}


def _robust_scores(raws: dict[int, Optional[float]]) -> dict[int, Optional[float]]:
    """1因子の raw{馬番→値|None} をレース内ロバスト正規化 (median/MAD → logistic 0-100)。

    None (その因子が計算できない馬) は None のまま (加重平均から除外される)。有効値が
    2頭未満なら差が付かないので有効馬を 60・None を None にする (最弱に落とさない)。
    """
    vals = [v for v in raws.values() if v is not None]
    if len(vals) < 2:
        return {k: (60.0 if v is not None else None) for k, v in raws.items()}
    med = median(vals)
    mad = median([abs(v - med) for v in vals]) * 1.4826
    scale = mad if mad > 1e-9 else (max(vals) - min(vals) or 1.0)
    out: dict[int, Optional[float]] = {}
    for k, v in raws.items():
        if v is None:
            out[k] = None
        else:
            z = (v - med) / scale
            out[k] = 100.0 / (1.0 + math.exp(-z / 1.3))
    return out


def provisional_index(rd: RaceData, feats=None) -> dict[int, float]:
    """公式出走表だけから決定論的な「仮指数」(0-100) を各馬に付ける。市場人気に一切触れない。

    返り値: {馬番: 仮指数 0-100}。過去走が全く無い/情報が取れない馬は NEUTRAL(50)。
    absent 馬は除外。feats は互換のため受けるが未使用 (past_runs から直接計算)。
    """
    horses = [h for h in rd.race.horses if not getattr(h, "absent", False)]
    if not horses:
        return {}
    r = rd.race
    ctx = {
        "dist": getattr(r, "distance", 0) or 0,
        "surf": getattr(r, "surface", "") or "",
        "class": class_index(getattr(r, "race_class", "") or ""),
        "sev": _going_sev(_today_going(rd)),
    }
    # 因子ごとに raw → ロバスト正規化 (レース内)。
    factor_scores: dict[str, dict[int, Optional[float]]] = {}
    for name, fn in _FACTORS.items():
        raws = {h.number: fn(h, ctx) for h in horses}
        factor_scores[name] = _robust_scores(raws)
    # 各馬 = 計算できた因子だけの加重平均 (欠損因子は分母から除外)。全欠損は中立50。
    out: dict[int, float] = {}
    for h in horses:
        num = den = 0.0
        for name, w in WEIGHTS.items():
            s = factor_scores[name].get(h.number)
            if s is not None:
                num += w * s
                den += w
        out[h.number] = round(num / den, 1) if den > 0 else NEUTRAL
    return out


def provisional_breakdown(rd: RaceData) -> dict[int, dict[str, Optional[float]]]:
    """各馬の因子別スコア (0-100, None=情報無し) を返す (デバッグ/表示/検証用)。"""
    horses = [h for h in rd.race.horses if not getattr(h, "absent", False)]
    if not horses:
        return {}
    r = rd.race
    ctx = {
        "dist": getattr(r, "distance", 0) or 0,
        "surf": getattr(r, "surface", "") or "",
        "class": class_index(getattr(r, "race_class", "") or ""),
        "sev": _going_sev(_today_going(rd)),
    }
    factor_scores: dict[str, dict[int, Optional[float]]] = {}
    for name, fn in _FACTORS.items():
        raws = {h.number: fn(h, ctx) for h in horses}
        factor_scores[name] = _robust_scores(raws)
    return {h.number: {name: factor_scores[name].get(h.number) for name in WEIGHTS}
            for h in horses}
