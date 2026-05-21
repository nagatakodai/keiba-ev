"""確率推定 → P×O 計算 → Plan 推奨 (競馬版)。

確率モデル: **Rank-position-specific Plackett-Luce (line bonus なし)**
  - 各順位 (1/2/3) に固有の strength ベクトルを持つ。
  - P(a=1着, b=2着, c=3着) = (s1[a]/Σ_all s1)
                            × (s2[b]/Σ_{k≠a} s2[k])
                            × (s3[c]/Σ_{k≠a,b} s3[k])
  - 競馬には「並び・ライン連携」概念がないため、KEIRIN 版にあった
    `_line_bonus` / `_line_strength` / pair_factor は **削除**。
  - 1 着確率は (1着率 × レーティング補正) を正規化し、市場暗黙率と blend。
"""
from __future__ import annotations

from typing import Iterable

import yaml

from .models import EvRow, Horse, Probabilities, RaceData, TrifectaOdds

# 競馬市場の平均控除率 ≒ 20% (中央競馬の場合 / WIN 単勝)。3 連単は 22.5%。
# `P × O = 1.0` が理論上の +EV ライン。確率モデルの楽観バイアスを差し引いて
# 1.02 をフロアにする (KEIRIN 版と同様の運用)。
PXO_FLOOR = 1.02
PXO_HONSEN = (PXO_FLOOR, 1.4)
PXO_CHUANA = (1.5, 3.0)
PXO_OANA = (3.0, float("inf"))


# ---------- 確率推定 ----------


def estimate_probs(
    rd: RaceData,
    *,
    market_blend: float = 0.4,
    market_floor: float = 0.01,
) -> Probabilities:
    """馬の rate / レーティングから素朴な事前確率を作る。

    市場ブレンディング:
      市場の 3 連単オッズを 1 着で marginalize した暗黙確率と、モデル 1 着確率を
      `market_blend` の比率で混合する。market_blend=0.4 ならモデル 6 / 市場 4。
      これでモデル側の楽観バイアス (レーティング線形補正等) を市場の集合知が
      機械的に打ち消す。`market_floor` は大穴を 0 で潰さないための下限。

    あくまでデフォルト。本格的な分析では YAML で上書きすること (`--probs`)。
    """
    horses = [h for h in rd.race.horses if not h.absent]
    n = len(horses)
    if n == 0:
        return Probabilities(win={}, place2={}, place3={})

    rating_mean = sum(h.rating for h in horses) / n if n > 0 else 0.0

    # 1 着確率: 1着率 × レーティング補正 を正規化
    raw_win: dict[int, float] = {}
    for h in horses:
        # レーティング補正: 平均との差を 12 で割って 0.5 〜 1.5 倍に。
        # rating が 0 の場合 (取得できなかった等) は補正なし。
        if rating_mean > 0 and h.rating > 0:
            rp_factor = max(0.3, 1.0 + (h.rating - rating_mean) / max(rating_mean, 1.0) * 0.6)
        else:
            rp_factor = 1.0
        # 1 着率は %。0 を許容しない (フロア 0.5%)。
        score = max(h.win_rate, 0.5) * rp_factor
        raw_win[h.number] = max(score, 1e-6)
    s = sum(raw_win.values())
    model_win = {k: v / s for k, v in raw_win.items()}

    # 市場ブレンディング (1 着のみ)
    win = model_win
    if market_blend > 0 and rd.trifecta:
        market = market_win_probs(rd.trifecta)
        if market:
            for k in model_win:
                market[k] = max(market.get(k, 0.0), market_floor)
            ms = sum(market.values())
            if ms > 0:
                market = {k: v / ms for k, v in market.items()}
            blended = {
                k: (1.0 - market_blend) * model_win.get(k, 0.0)
                + market_blend * market.get(k, 0.0)
                for k in set(model_win) | set(market)
            }
            bs = sum(blended.values())
            if bs > 0:
                win = {k: v / bs for k, v in blended.items()}

    # 純 2 着率・純 3 着率を重みに (0 は 0.5 でフロア)
    place2 = {h.number: max(h.pure_second, 0.5) for h in horses}
    place3 = {h.number: max(h.pure_third, 0.5) for h in horses}

    return Probabilities(win=win, place2=place2, place3=place3)


def market_win_probs(trifecta: Iterable[TrifectaOdds]) -> dict[int, float]:
    """3 連単オッズを 1 着で marginalize した market-implied 1 着率。

    `sum_{b,c} 1/odds(a,b,c)` を計算し、全体で正規化。控除率は正規化で吸収される。
    """
    raw: dict[int, float] = {}
    for t in trifecta:
        if t.absent or t.odds <= 0:
            continue
        a = t.key[0]
        raw[a] = raw.get(a, 0.0) + 1.0 / t.odds
    s = sum(raw.values())
    if s <= 0:
        return {}
    return {k: v / s for k, v in raw.items()}


def load_probs(path: str | None, fallback: Probabilities) -> Probabilities:
    if not path:
        return fallback
    raw = yaml.safe_load(open(path, "r", encoding="utf-8"))
    win = {int(k): float(v) for k, v in (raw.get("win_prob") or {}).items()}
    place2 = {int(k): float(v) for k, v in (raw.get("place2_prob") or {}).items()}
    place3 = {int(k): float(v) for k, v in (raw.get("place3_prob") or {}).items()}
    for k, v in fallback.win.items():
        win.setdefault(k, v)
    for k, v in fallback.place2.items():
        place2.setdefault(k, v)
    for k, v in fallback.place3.items():
        place3.setdefault(k, v)
    s = sum(win.values())
    if s > 0:
        win = {k: v / s for k, v in win.items()}
    return Probabilities(win=win, place2=place2, place3=place3)


# ---------- 3 連単確率 ----------


def trifecta_prob(key: tuple[int, int, int], probs: Probabilities) -> float:
    """Plackett-Luce 連鎖で 3 連単 (a, b, c) の的中確率を計算する。

    P(a=1着, b=2着, c=3着)
        = P(a=1着)              ← probs.win[a] (合計 1 に正規化済)
        × P(b=2着 | a=1着)      ← s2[b] / Σ_{k≠a} s2[k]
        × P(c=3着 | a=1着,b=2着) ← s3[c] / Σ_{k≠a,b} s3[k]

    競馬は line bonus がないので KEIRIN 版にあった pair_factor は削除。
    """
    a, b, c = key
    p1 = probs.win.get(a, 0.0)
    if p1 <= 0:
        return 0.0

    raw_b = probs.place2.get(b, 0.0)
    denom_b = sum(probs.place2.get(k, 0.0) for k in probs.win if k != a)
    p2 = raw_b / denom_b if denom_b > 0 else 0.0

    raw_c = probs.place3.get(c, 0.0)
    denom_c = sum(probs.place3.get(k, 0.0) for k in probs.win if k != a and k != b)
    p3 = raw_c / denom_c if denom_c > 0 else 0.0

    return p1 * p2 * p3


# ---------- EV テーブル ----------


def build_table(rd: RaceData, probs: Probabilities) -> list[EvRow]:
    rows: list[EvRow] = []
    for t in rd.trifecta:
        if t.absent or t.odds <= 0:
            continue
        p = trifecta_prob(t.key, probs)
        pxo = p * t.odds
        rows.append(
            EvRow(
                key=t.key,
                odds=t.odds,
                popularity=t.popularity,
                prob=p,
                px_o=pxo,
                tier=_tier(pxo),
            )
        )
    rows.sort(key=lambda r: r.px_o, reverse=True)
    return rows


def _tier(pxo: float) -> str:
    if pxo < PXO_FLOOR:
        return "minus"
    if pxo <= PXO_HONSEN[1]:
        return "honsen"
    if pxo <= PXO_CHUANA[1]:
        return "chuana"
    return "oana"


# ---------- Plan 推奨 ----------


def apply_evidence(
    rows: list[EvRow],
    evidence_by_key: dict[str, dict],
    cuts: set[str] | list[str] | None = None,
) -> list[EvRow]:
    """検索補強根拠で prob/px_o を再計算し、cuts に該当する目を完全除外。

    補強根拠数による係数:
      - 3 件以上: ×1.20 (コア候補)
      - 2 件:     ×1.10 (採用候補)
      - 1 件:     ×1.00 (保留)
      - 0 件:     ×0.85 (補強なし: ペナルティ)
      - cuts に列挙された目: 完全除外 (取消・並び破綻 — 競馬では取消 / 競走除外)
    """
    cuts_set = {c.strip() for c in (cuts or [])}
    out: list[EvRow] = []
    for r in rows:
        key_str = f"{r.key[0]}-{r.key[1]}-{r.key[2]}"
        if key_str in cuts_set:
            continue
        info = evidence_by_key.get(key_str, {})
        try:
            count = int(info.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        if count >= 3:
            mul = 1.20
        elif count >= 2:
            mul = 1.10
        elif count >= 1:
            mul = 1.00
        else:
            mul = 0.85
        new_prob = r.prob * mul
        new_px_o = new_prob * r.odds
        out.append(
            EvRow(
                key=r.key,
                odds=r.odds,
                popularity=r.popularity,
                prob=new_prob,
                px_o=new_px_o,
                tier=_tier(new_px_o),
            )
        )
    out.sort(key=lambda r: r.px_o, reverse=True)
    return out


def apply_caps(
    rows: list[EvRow],
    *,
    ev_max: float | None = None,
    min_prob: float | None = None,
) -> list[EvRow]:
    out = rows
    if ev_max is not None:
        out = [r for r in out if r.px_o <= ev_max]
    if min_prob is not None:
        out = [r for r in out if r.prob >= min_prob]
    return out


def plan_balanced(
    rows: list[EvRow], budget: int = 10_000, target: int = 5
) -> list[EvRow]:
    """5 点バランス (Plan A): 本線 2 / 中穴 2 / 大穴 1。"""
    honsen = _take(rows, "honsen", 2)
    chuana = _take(rows, "chuana", 2, exclude=honsen)
    oana = _take(rows, "oana", 1, exclude=honsen + chuana)
    picks = honsen + chuana + oana
    if len(picks) < target:
        seen = {tuple(r.key) for r in picks}
        for r in rows:
            if r.tier == "minus" or tuple(r.key) in seen:
                continue
            picks.append(r)
            seen.add(tuple(r.key))
            if len(picks) >= target:
                break
    return picks


def plan_max_ev(rows: list[EvRow]) -> list[EvRow]:
    """最高 EV (Plan B): 上位 1-3 点。"""
    pos = [r for r in rows if r.px_o >= PXO_FLOOR]
    return pos[:3]


PLAN_C_MAX_POINTS = 12


def plan_wide(rows: list[EvRow], target: int = PLAN_C_MAX_POINTS) -> list[EvRow]:
    """広め (Plan C): +EV 上位 target 点まで。"""
    pos = [r for r in rows if r.px_o >= PXO_FLOOR]
    return pos[:target]


def plan_hit_pure(rows: list[EvRow], target: int = 3) -> list[EvRow]:
    """Plan H1: 推定 P 上位 N 点。EV 不問。"""
    return sorted(rows, key=lambda r: r.prob, reverse=True)[:target]


def plan_hit_safe(rows: list[EvRow], target: int = 3) -> list[EvRow]:
    """Plan H2: 推定 P 上位 N 点のうち P×O >= 1.0 のみ。"""
    cand = [r for r in rows if r.px_o >= 1.0]
    return sorted(cand, key=lambda r: r.prob, reverse=True)[:target]


def plan_final(*plans: list[EvRow]) -> list[EvRow]:
    """Plan F: 全 Plan の union (key 重複除去) — いいとこどり最終買い目。"""
    seen: set[tuple[int, int, int]] = set()
    out: list[EvRow] = []
    for plan in plans:
        for r in plan:
            k = tuple(r.key)
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
    out.sort(key=lambda r: r.px_o, reverse=True)
    return out


def _take(rows: list[EvRow], tier: str, n: int, exclude: list[EvRow] | None = None) -> list[EvRow]:
    seen = {tuple(r.key) for r in (exclude or [])}
    picked: list[EvRow] = []
    for r in rows:
        if r.tier != tier:
            continue
        if tuple(r.key) in seen:
            continue
        picked.append(r)
        if len(picked) >= n:
            break
    return picked


# ---------- 補助 ----------


def horse_table(rd: RaceData) -> list[dict]:
    """馬一覧 (表示用)。"""
    out: list[dict] = []
    for h in rd.race.horses:
        out.append(
            {
                "no": h.number,
                "bracket": h.bracket,
                "name": h.name,
                "sex_age": h.sex_age,
                "weight": h.weight_kg,
                "body_weight": h.body_weight,
                "body_weight_diff": h.body_weight_diff,
                "jockey": h.jockey_name,
                "trainer": h.trainer_name,
                "rating": h.rating,
                "win": h.win_rate,
                "qn": h.quinella_rate,
                "tr": h.trio_rate,
                "pure2": h.pure_second,
                "pure3": h.pure_third,
                "style": h.style,
                "win_odds": h.win_odds,
                "absent": h.absent,
            }
        )
    return out
