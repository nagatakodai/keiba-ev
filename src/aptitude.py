"""Layer 1.5: 各馬の「適性指数」(0-100 / 同レース内相対) を集約する。

役割:
  features.py の FeatureVec + past_runs の重賞実績 →
  AptitudeIndex { total, ability, distance_fit, last3f, surface_fit,
                  condition, jockey_fit, pace_fit, graded_record, reasons[] }

哲学:
  - 全 sub-score は **同レース内 max=100** で正規化 (相対指数)。
  - 因子間の重みは固定 (Phase 1)。Phase 4 で学習 / チューニング余地あり。
  - reasons[] は UI 上の「なぜ高いか」表示用の短いラベル。

注意:
  - これは「事前」適性指数 (検索 MCP の補強根拠は含まない)。
  - LLM 評価後の補正は別途 apply_evidence 系で乗じる。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .features import FeatureVec, build_features
from .models import Horse, PastRun, RaceData

# ---------- 重賞実績 ----------

# 過去走 race_class の重み (新馬 / 未勝利 は 0、平場勝ち上がり系は 0)
_GRADED_RE = re.compile(
    r"(JpnG?1|JpnG?2|JpnG?3|G1|G2|G3|GⅠ|GⅡ|GⅢ|L\b|Listed|オープン|OP)"
)


def _graded_weight(race_class: str) -> float:
    """race_class 文字列 → 重み (0 = 重賞でない)。"""
    if not race_class:
        return 0.0
    m = _GRADED_RE.search(race_class)
    if not m:
        return 0.0
    tag = m.group(1)
    # G1 / JpnG1 / GⅠ
    if "1" in tag or "Ⅰ" in tag:
        return 10.0
    if "2" in tag or "Ⅱ" in tag:
        return 5.0
    if "3" in tag or "Ⅲ" in tag:
        return 3.0
    if tag in ("L", "Listed"):
        return 2.0
    if tag in ("OP", "オープン"):
        return 1.0
    return 0.0


def graded_score(past_runs: list[PastRun]) -> float:
    """過去走の重賞実績を生スコア化。

    重み (race_class) × finish 倍率を加算:
      1着 ×2.0 / 2着 ×1.5 / 3着 ×1.2 / それ以下 ×0.5 (出走経験のみ)
    finish_pos が None (= 4着以下) は ×0.5。
    """
    total = 0.0
    for r in past_runs:
        w = _graded_weight(r.race_class)
        if w <= 0:
            continue
        if r.finish_pos == 1:
            mul = 2.0
        elif r.finish_pos == 2:
            mul = 1.5
        elif r.finish_pos == 3:
            mul = 1.2
        else:
            mul = 0.5
        total += w * mul
    return total


def graded_summary(past_runs: list[PastRun]) -> str:
    """重賞経験の人間可読サマリ。空文字なら経験なし。

    例: "G1 1着1 / G3 連2"。
    """
    buckets: dict[str, dict[str, int]] = {}
    for r in past_runs:
        w = _graded_weight(r.race_class)
        if w <= 0:
            continue
        tag = _normalize_grade_tag(r.race_class)
        b = buckets.setdefault(tag, {"win": 0, "place": 0, "show": 0, "run": 0})
        if r.finish_pos == 1:
            b["win"] += 1
        elif r.finish_pos == 2:
            b["place"] += 1
        elif r.finish_pos == 3:
            b["show"] += 1
        else:
            b["run"] += 1
    parts = []
    for tag in ("G1", "G2", "G3", "L", "OP"):
        b = buckets.get(tag)
        if not b:
            continue
        bits = []
        if b["win"]:
            bits.append(f"1着{b['win']}")
        if b["place"]:
            bits.append(f"連{b['place']}")
        if b["show"]:
            bits.append(f"3着{b['show']}")
        if not bits and b["run"]:
            bits.append(f"{b['run']}走")
        if bits:
            parts.append(f"{tag} {'/'.join(bits)}")
    return " ".join(parts)


def _normalize_grade_tag(race_class: str) -> str:
    m = _GRADED_RE.search(race_class)
    if not m:
        return ""
    tag = m.group(1)
    if "1" in tag or "Ⅰ" in tag:
        return "G1"
    if "2" in tag or "Ⅱ" in tag:
        return "G2"
    if "3" in tag or "Ⅲ" in tag:
        return "G3"
    if tag in ("L", "Listed"):
        return "L"
    return "OP"


# ---------- AptitudeIndex ----------


@dataclass
class AptitudeIndex:
    """1 馬の適性指数 (各 sub-score は 0-100 / 同レース内相対)。

    total は重み付け平均で 0-100 に正規化済み。
    reasons は表示用の短いラベル (上位入りした sub-score だけ収録)。
    """
    number: int
    total: float = 0.0
    ability: float = 0.0
    distance_fit: float = 0.0
    last3f: float = 0.0
    surface_fit: float = 0.0       # 同 surface/venue 経験 + show率 (コース適性)
    going_fit: float = 0.0          # 同馬場状態 (良/稍/重/不) での好走率 (馬場状態適性)
    condition: float = 0.0
    jockey_fit: float = 0.0
    pace_fit: float = 0.0
    graded_record: float = 0.0
    graded_text: str = ""
    reasons: list[str] = field(default_factory=list)


# sub-score 重み (合計が分母)
_WEIGHTS = {
    "ability": 1.5,
    "distance_fit": 1.2,
    "last3f": 1.0,
    "surface_fit": 0.8,
    "going_fit": 0.6,
    "condition": 0.6,
    "jockey_fit": 0.4,
    "pace_fit": 0.8,
    "graded_record": 0.7,
}
_W_SUM = sum(_WEIGHTS.values())


# 適性指数の表示レンジ。同レース内最弱が APTITUDE_FLOOR、最強が APTITUDE_CEIL になる
# min-max scaling を使う。旧実装は max のみで割っていたため ability や距離適性のように
# raw 値が拮抗するレースで全頭 99-100 に張り付き、刻みが効かなかった。FLOOR を上げる
# (例: 60) ことで平均が ~80 付近に来る読みやすい指数になる。
APTITUDE_FLOOR = 60.0
APTITUDE_CEIL = 100.0


def _normalize_to_100(raw: dict[int, float]) -> dict[int, float]:
    """同レース内で min-max scaling して [APTITUDE_FLOOR, APTITUDE_CEIL] に展開。

    raw=0 は **「情報無し」の signal** として 0 のまま残す (新馬・経験 0 走 etc.)。
    positive な raw 値 (>0) のみで min-max scaling し、最弱を FLOOR (60)、最強を CEIL
    (100)、間は線形に分布。これで:
    - 全頭情報無し (max<=0)            → 全 0 (はっきり「データ無し」を示す)
    - 1頭だけ positive (他は 0)        → その1頭 CEIL、他は 0
    - positive 同士が同値             → positive は CEIL、 0 は 0
    - それ以外                          → positive を [FLOOR, CEIL] に min-max 展開、 0 は 0

    旧 (max のみ割り) は raw 拮抗時に全頭 99-100 になり弁別不能だった。本実装は raw が
    僅差でも刻みを残しつつ、 raw=0 horse を 60 (「最弱だが情報あり」) と誤表示しない。
    """
    if not raw:
        return {}
    clipped = {k: max(v, 0.0) for k, v in raw.items()}
    pos_vals = [v for v in clipped.values() if v > 0]
    if not pos_vals:
        return {k: 0.0 for k in clipped}   # 全頭情報無し
    mn, mx = min(pos_vals), max(pos_vals)
    if mx - mn < 1e-9:
        # positive 同士全て同値: 等しく CEIL を付与 (情報なし horse は 0 を維持)
        return {k: (APTITUDE_CEIL if v > 0 else 0.0) for k, v in clipped.items()}
    spread = APTITUDE_CEIL - APTITUDE_FLOOR
    rng = mx - mn
    return {k: (APTITUDE_FLOOR + (v - mn) / rng * spread if v > 0 else 0.0)
            for k, v in clipped.items()}


def _ability_raw(fv: FeatureVec) -> float:
    """能力 raw: 重み付け SI を基準。SI=0 (走り無し) は 0。"""
    return float(fv.speed_idx_weighted)


def _distance_fit_raw(fv: FeatureVec) -> float:
    """距離適性 raw: 当該距離 × surface の shrinkage 勝率 × 経験ボーナス。"""
    bonus = 1.0 + 0.1 * min(fv.same_distance_count, 5)  # 経験 5 走で +50%
    return fv.shrunk_win_rate * bonus * 100.0  # スケール調整


def _last3f_raw(fv: FeatureVec) -> float:
    """末脚 raw: last3f_idx_recent は「秒/距離標準化」で **小さいほど速い**。

    そのまま使うと逆向きなので、(基準 - 値) で正方向に変換。
    基準は 35.5 秒相当の標準化値 (35.5 / 0.94+1500/20000 ≈ 36.0) — 試算で 0 が出ない範囲。
    """
    if fv.last3f_idx_recent <= 0:
        return 0.0
    # 35.0 を「速い末脚の閾」、40.0 を「遅い末脚」として線形化
    # 値が小さいほど速い → (40 - x) を返す。0 でクリップ。
    return max(40.0 - fv.last3f_idx_recent, 0.0)


def _surface_fit_raw(fv: FeatureVec) -> float:
    """コース適性 raw: 同 surface 経験 + 当該場経験 + show rate (芝/ダート/障害 × 場)。"""
    return (
        min(fv.same_surface_count, 8) * 1.0
        + min(fv.same_venue_count, 5) * 1.5
        + fv.shrunk_show_rate * 30.0
    )


def _going_fit_raw(fv: FeatureVec) -> float:
    """馬場状態適性 raw: 同馬場 (良/稍/重/不) での好走実績。

    same_going_count が 0 (未経験 or current going 不明) なら going_versatility で代用。
    versatility 高い = 馬場不問タイプ。
    """
    if fv.same_going_count > 0:
        # 同馬場で 1 走以上経験 → show 率 × 経験ボーナス
        bonus = 1.0 + 0.1 * min(fv.same_going_count, 5)
        return fv.same_going_show_rate * bonus * 100.0
    # 未経験 / current going 不明 → 多様性スコア
    return fv.going_versatility * 50.0


def _condition_raw(fv: FeatureVec) -> float:
    """状態 raw: 適度な間隔 + 大幅な馬体重変動でペナルティ。

    days_since_last_run:
      7-21 日 → ベスト (+10)
      22-49 日 → 良 (+8)
      50-90 日 → 普通 (+5)
      >90 日 (休み明け) → 減 (+2)
      <7 日 → 連闘 (+3)
    big_weight_change → -3
    """
    d = fv.days_since_last_run
    if d == 0:
        score = 4.0
    elif d < 7:
        score = 3.0
    elif d <= 21:
        score = 10.0
    elif d <= 49:
        score = 8.0
    elif d <= 90:
        score = 5.0
    else:
        score = 2.0
    if fv.big_weight_change:
        score -= 3.0
    return max(score, 0.0)


def _jockey_fit_raw(fv: FeatureVec) -> float:
    """騎手 raw: 継続騎乗で +、乗り替わりで - (Phase 1 は粗い)。

    Phase 2 で「騎手×当該場成績」を統計で外注する余地あり。
    """
    return 3.0 if not fv.jockey_change else 1.0


def _pace_fit_raw(fv: FeatureVec) -> float:
    """ペース合致 raw: features.pace_fit は -1..+1。正に振れているほど展開有利。

    0 を 50 にして -1=0, +1=100 のスケールに線形変換した raw。
    """
    return (fv.pace_fit + 1.0) * 50.0  # -1→0, +1→100


def compute_aptitudes(
    rd: RaceData,
    feats: dict[int, FeatureVec] | None = None,
) -> dict[int, AptitudeIndex]:
    """RaceData → { 馬番: AptitudeIndex } を返す。

    feats を渡さなければ build_features を呼ぶ。
    """
    if feats is None:
        feats = build_features(rd)

    horses = [h for h in rd.race.horses if not h.absent]
    if not horses:
        return {}

    # raw 値を全馬分集める
    raw_ability: dict[int, float] = {}
    raw_dist: dict[int, float] = {}
    raw_last3f: dict[int, float] = {}
    raw_surface: dict[int, float] = {}
    raw_going: dict[int, float] = {}
    raw_cond: dict[int, float] = {}
    raw_jockey: dict[int, float] = {}
    raw_pace: dict[int, float] = {}
    raw_graded: dict[int, float] = {}

    graded_texts: dict[int, str] = {}

    for h in horses:
        fv = feats.get(h.number)
        if fv is None:
            continue
        raw_ability[h.number] = _ability_raw(fv)
        raw_dist[h.number] = _distance_fit_raw(fv)
        raw_last3f[h.number] = _last3f_raw(fv)
        raw_surface[h.number] = _surface_fit_raw(fv)
        raw_going[h.number] = _going_fit_raw(fv)
        raw_cond[h.number] = _condition_raw(fv)
        raw_jockey[h.number] = _jockey_fit_raw(fv)
        raw_pace[h.number] = _pace_fit_raw(fv)
        raw_graded[h.number] = graded_score(h.past_runs or [])
        graded_texts[h.number] = graded_summary(h.past_runs or [])

    # 各 sub-score を 0-100 にレース内正規化
    n_ability = _normalize_to_100(raw_ability)
    n_dist = _normalize_to_100(raw_dist)
    n_last3f = _normalize_to_100(raw_last3f)
    n_surface = _normalize_to_100(raw_surface)
    n_going = _normalize_to_100(raw_going)
    n_cond = _normalize_to_100(raw_cond)
    n_jockey = _normalize_to_100(raw_jockey)
    n_pace = _normalize_to_100(raw_pace)
    n_graded = _normalize_to_100(raw_graded)

    # reasons 用の閾値: 同レース 75 パーセンタイル以上 ≒ 上位 25%
    def _top_quartile(d: dict[int, float]) -> float:
        if not d:
            return 100.0
        xs = sorted(d.values(), reverse=True)
        idx = max(len(xs) // 4 - 1, 0)
        return xs[idx]

    th_ability = _top_quartile(n_ability)
    th_dist = _top_quartile(n_dist)
    th_last3f = _top_quartile(n_last3f)
    th_surface = _top_quartile(n_surface)
    th_going = _top_quartile(n_going)
    th_cond = _top_quartile(n_cond)
    th_pace = _top_quartile(n_pace)

    out: dict[int, AptitudeIndex] = {}
    for h in horses:
        n = h.number
        if n not in n_ability:
            continue
        ai = AptitudeIndex(
            number=n,
            ability=n_ability[n],
            distance_fit=n_dist[n],
            last3f=n_last3f[n],
            surface_fit=n_surface[n],
            going_fit=n_going[n],
            condition=n_cond[n],
            jockey_fit=n_jockey[n],
            pace_fit=n_pace[n],
            graded_record=n_graded[n],
            graded_text=graded_texts[n],
        )
        # 総合 = 重み付け平均
        ai.total = (
            _WEIGHTS["ability"] * ai.ability
            + _WEIGHTS["distance_fit"] * ai.distance_fit
            + _WEIGHTS["last3f"] * ai.last3f
            + _WEIGHTS["surface_fit"] * ai.surface_fit
            + _WEIGHTS["going_fit"] * ai.going_fit
            + _WEIGHTS["condition"] * ai.condition
            + _WEIGHTS["jockey_fit"] * ai.jockey_fit
            + _WEIGHTS["pace_fit"] * ai.pace_fit
            + _WEIGHTS["graded_record"] * ai.graded_record
        ) / _W_SUM

        # reasons
        reasons: list[str] = []
        if ai.ability >= th_ability and ai.ability > 0:
            reasons.append("速力上位")
        if ai.distance_fit >= th_dist and ai.distance_fit > 0:
            reasons.append("距離適性◎")
        if ai.last3f >= th_last3f and ai.last3f > 0:
            reasons.append("末脚◎")
        if ai.surface_fit >= th_surface and ai.surface_fit > 0:
            reasons.append("コース◎")
        if ai.going_fit >= th_going and ai.going_fit > 0:
            reasons.append("馬場状態◎")
        if ai.condition >= th_cond and ai.condition > 0:
            reasons.append("間隔良好")
        if ai.pace_fit >= th_pace and ai.pace_fit > 50:
            reasons.append("ペース合致")
        if ai.graded_record >= 50:
            reasons.append(f"重賞 {ai.graded_text}".strip())
        fv = feats.get(n)
        if fv:
            if fv.jockey_change:
                reasons.append("乗替り")
            if fv.big_weight_change:
                reasons.append(f"馬体{fv.body_weight_diff:+d}kg")
        ai.reasons = reasons
        out[n] = ai

    return out


def aptitude_summary_line(h: Horse, ai: AptitudeIndex) -> str:
    """1 行のテキスト要約 (CLI / ログ用)。"""
    return (
        f"{h.number:2d} {h.name} 総{ai.total:5.1f} | "
        f"能{ai.ability:4.0f} 距{ai.distance_fit:4.0f} 末{ai.last3f:4.0f} "
        f"コ{ai.surface_fit:4.0f} 馬{ai.going_fit:4.0f} 状{ai.condition:4.0f} "
        f"騎{ai.jockey_fit:4.0f} ペ{ai.pace_fit:4.0f} 重{ai.graded_record:4.0f}"
        f"{' | ' + ', '.join(ai.reasons) if ai.reasons else ''}"
    )
