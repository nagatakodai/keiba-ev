"""確率推定 → P×O 計算 → Plan 推奨 (競馬版)。

確率モデル: **Rank-position-specific Plackett-Luce (line bonus なし)**
  - 各順位 (1/2/3) に固有の strength ベクトルを持つ。
  - P(a=1着, b=2着, c=3着) = (s1[a]/Σ_all s1)
                            × (s2[b]/Σ_{k≠a} s2[k])
                            × (s3[c]/Σ_{k≠a,b} s3[k])
  - 競馬には「並び・ライン連携」概念がないため、KEIRIN 版にあった
    `_line_bonus` / `_line_strength` / pair_factor は **削除**。

Phase A 改修 (2026-05): **生涯 1 着率を完全廃止**。代わりに features.py の
  Layer 1 特徴量 (西田式スピード指数 + 距離・サーフェス条件付き shrinkage 勝率
  + 末脚指数) を softmax で fundamental probability に変換。
  Bolton-Chapman / Benter / Yurelu の知見に準拠 — 詳細は research log 参照。
"""
from __future__ import annotations

from typing import Iterable

import yaml

from .models import BetEvRow, BetOdds, EvRow, Horse, Probabilities, RaceData, TrifectaOdds

# 競馬市場の平均控除率 ≒ 20% (中央競馬の場合 / WIN 単勝)。3 連単は 22.5%。
# `P × O = 1.0` が理論上の +EV ライン。確率モデルの楽観バイアスを差し引いて
# 1.02 をフロアにする (KEIRIN 版と同様の運用)。
PXO_FLOOR = 1.02
PXO_HONSEN = (PXO_FLOOR, 1.4)
PXO_CHUANA = (1.5, 3.0)
PXO_OANA = (3.0, float("inf"))

# Henery / Discounted Harville の λ (Lo-Bacon-Shone 1995 の経験値)。
# 1.0 = 素の Harville (人気馬の 2/3 着を過大評価)。0.65-0.85 ≒ 文献標準。
# λ < 1 で 2-3 着分布が平坦化 → 大穴に +EV が残る (favorite-longshot bias の構造補正)。
DEFAULT_LAMBDA_2 = 0.81
DEFAULT_LAMBDA_3 = 0.65

# bet-type-specific market_blend (Phase 19/20: holdout 291 races real-odds eval 由来)。
# 各 bet type で最適な β が異なる。eval_holdout の結果:
#   - 単勝 ROI peak:        β=0.75-0.80 (95.9%, 市場 88.5% 比 +7pt)
#   - 3 連単 PL hit rate:   β=0.70-0.80 で並ぶ (mean rank 86)
#   - Plan A/B/C ROI:       N=291 では結論不能 (β=0.78 で安全側)
#   - Plan H1 ROI:          β=0 が peak (109.6%, +EV) ← Phase 19
#   - Plan G ROI:           β=1.0 が peak (108.1%, +EV, hit 5/291) ← Phase 20
#                            β=0→1 で単調 0% → 108% に上昇
BLEND_DEFAULT = 0.78
BLEND_HIT_PURE = 0.0
BLEND_APTITUDE_GATE = 1.0

# LightGBM softmax の temperature (Phase 21: holdout 291 races log loss 最小化由来)。
# T < 1 で sharpening (model 過小自信を補正)、T > 1 で flattening。
# holdout で T=0.4 が log loss 最小 (T=1 比 -0.089)、Plan H2 が 2 → 11 hits に安定化。
LGBM_TEMPERATURE = 0.4


# ---------- 確率推定 ----------


def estimate_probs(
    rd: RaceData,
    *,
    market_blend: float = BLEND_DEFAULT,
    market_floor: float = 0.01,
    blend_method: str = "loglinear",   # "loglinear" (Benter 2-step) | "linear" (旧)
    lambda_2: float = DEFAULT_LAMBDA_2,
    lambda_3: float = DEFAULT_LAMBDA_3,
    use_show_bias: bool = True,
) -> Probabilities:
    """Layer 1 特徴量 (features.py) + 市場ブレンド + Discounted Harville で Probabilities を作る。

    1 着強度 s_i:
      LightGBM 学習済モデルがあれば → softmax(model_score)
      無ければ → softmax(W_speed·z_speed + W_win·z_shrunk_win + W_show·z_shrunk_show − W_last3f·z_last3f)

    市場ブレンド (`blend_method`):
      "loglinear" (Benter 2-step、推奨):
          c_i = softmax(α·log f_i + β·log π_i)
          α = 1 - market_blend、β = market_blend
          π_i は power-method de-overround された市場暗黙率
      "linear" (旧、後方互換用):
          c_i = (1-β)·f_i + β·π_i

    2 着・3 着強度 (Discounted Harville):
      place2[i] = win[i]^λ_2   (λ_2 ≈ 0.81)
      place3[i] = win[i]^λ_3   (λ_3 ≈ 0.65)
      これにより人気馬の 2/3 着過大評価 (素の Harville の構造的欠陥) を平坦化する。
      `use_show_bias=True` でさらに shrunk_show_rate を乗じて 3 着スペシャリストを優遇。

    past_runs が空の場合は Layer 1 特徴量が全 0 → 市場ブレンドのみが動く後方互換動作。
    """
    import math

    from .features import build_features

    horses = [h for h in rd.race.horses if not h.absent]
    n = len(horses)
    if n == 0:
        return Probabilities(win={}, place2={}, place3={})

    feats = build_features(rd)
    fundamental_win = _fundamental_win_probs(horses, feats)

    # 市場ブレンド
    win = fundamental_win
    if market_blend > 0 and rd.trifecta:
        market_raw = market_win_probs(rd.trifecta)
        if market_raw:
            market = market_raw
            try:
                market = power_method_overround(market_raw)
            except Exception:
                pass
            # floor 適用
            for k in fundamental_win:
                market[k] = max(market.get(k, 0.0), market_floor)
            ms = sum(market.values())
            if ms > 0:
                market = {k: v / ms for k, v in market.items()}

            if blend_method == "loglinear":
                alpha = max(1.0 - market_blend, 0.0)
                beta = max(market_blend, 0.0)
                logs: dict[int, float] = {}
                for k in set(fundamental_win) | set(market):
                    f = max(fundamental_win.get(k, 0.0), 1e-9)
                    pi = max(market.get(k, 0.0), 1e-9)
                    logs[k] = alpha * math.log(f) + beta * math.log(pi)
                m = max(logs.values())
                exps = {k: math.exp(v - m) for k, v in logs.items()}
                z = sum(exps.values())
                if z > 0:
                    win = {k: v / z for k, v in exps.items()}
            else:  # linear
                blended = {
                    k: (1.0 - market_blend) * fundamental_win.get(k, 0.0)
                    + market_blend * market.get(k, 0.0)
                    for k in set(fundamental_win) | set(market)
                }
                bs = sum(blended.values())
                if bs > 0:
                    win = {k: v / bs for k, v in blended.items()}

    # Discounted Harville: place2/place3 = win^λ (relative なので正規化不要)
    place2: dict[int, float] = {}
    place3: dict[int, float] = {}
    for h in horses:
        n_ = h.number
        w = max(win.get(n_, 0.0), 1e-9)
        p2 = w ** lambda_2
        p3 = w ** lambda_3
        if use_show_bias:
            # 3 着スペシャリスト効果: shrunk_show_rate を相対重みとして乗じる
            show = feats[n_].shrunk_show_rate
            avg_show = (
                sum(feats[h2.number].shrunk_show_rate for h2 in horses) / n
                if n > 0 else 0.0
            )
            if avg_show > 0:
                bias = show / avg_show  # 平均 1 になる
                p2 *= max(bias, 0.1)
                p3 *= max(bias, 0.1)
        place2[n_] = p2
        place3[n_] = p3

    return Probabilities(win=win, place2=place2, place3=place3)


def power_method_overround(raw_probs: dict[int, float], *, tol: float = 1e-6, max_iter: int = 60) -> dict[int, float]:
    """Power-method de-overround (Clarke 2017): Σ p_i^(1/k) = 1 を満たす k を Brent 法で解く。

    raw_probs は「1/odds を正規化しただけの暗黙率」(= Σ=1)。これを power 変換で
    favorite-longshot bias を補正する。k > 1 で人気馬寄り、k < 1 で大穴寄りに歪む。
    JRA / NAR は overround が大きいため k は通常 1.0-1.2 の間に収まる。
    """
    if not raw_probs:
        return raw_probs

    # Σ p_i^(1/k) = 1 を k について解く。f(k) = Σ p_i^(1/k) - 1
    # 単調 (k 大きいほど Σ 小、k 小さいほど Σ 大) なので bisection で十分。
    items = list(raw_probs.values())

    def f(k: float) -> float:
        try:
            return sum(p ** (1.0 / k) for p in items if p > 0) - 1.0
        except (ValueError, ZeroDivisionError, OverflowError):
            return float("inf")

    # bracket
    lo, hi = 0.5, 3.0
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == f_hi or f_lo * f_hi > 0:
        return raw_probs  # bracket できなかったらそのまま返す
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if abs(fm) < tol:
            break
        if fm * f_lo < 0:
            hi, f_hi = mid, fm
        else:
            lo, f_lo = mid, fm
    k = (lo + hi) / 2.0
    return {key: (max(p, 0.0) ** (1.0 / k)) for key, p in raw_probs.items()}


def _z_score(values: dict[int, float]) -> dict[int, float]:
    """値を z-score 化。SD が小さい場合 (全て同値) は 0 を返す。"""
    if not values:
        return {}
    xs = list(values.values())
    n = len(xs)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / max(n - 1, 1)
    sd = var ** 0.5
    if sd < 1e-6:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / sd for k, v in values.items()}


def _fundamental_win_probs(horses, feats) -> dict[int, float]:
    """Layer 1 特徴量 → softmax で 1 着確率。

    学習済 LightGBM lambdarank モデル (`data/models/lgbm_lambdarank.txt`) があれば
    それを使う。無ければ z-score 線形和を softmax する Phase A 初期版にフォールバック。
    """
    lgb_probs = _lgbm_predict(horses, feats)
    if lgb_probs is not None:
        return lgb_probs
    return _linear_softmax_fallback(horses, feats)


def _linear_softmax_fallback(horses, feats) -> dict[int, float]:
    """LightGBM モデル無いとき: 各特徴量を z-score 化 → 重み付け softmax。

    重みは Bolton-Chapman / Benter / Yurelu の文献値を踏まえた粗い初期値。
    符号:
      speed_idx_weighted: + (大きいほど速い)
      shrunk_win_rate: + (大きいほど勝つ)
      shrunk_show_rate: + (大きいほど上位安定)
      last3f_idx_recent: - (小さいほど速い末脚)
    """
    import math

    si_w = _z_score({h.number: feats[h.number].speed_idx_weighted for h in horses})
    sh_w = _z_score({h.number: feats[h.number].shrunk_win_rate for h in horses})
    sh_s = _z_score({h.number: feats[h.number].shrunk_show_rate for h in horses})
    l3f = _z_score({h.number: feats[h.number].last3f_idx_recent for h in horses})

    W_SPEED = 1.0
    W_WIN = 0.6
    W_SHOW = 0.3
    W_LAST3F = 0.4

    raw: dict[int, float] = {}
    for h in horses:
        n = h.number
        z = (
            W_SPEED * si_w.get(n, 0.0)
            + W_WIN * sh_w.get(n, 0.0)
            + W_SHOW * sh_s.get(n, 0.0)
            - W_LAST3F * l3f.get(n, 0.0)
        )
        raw[n] = math.exp(z)
    s = sum(raw.values())
    if s <= 0:
        return {h.number: 1.0 / len(horses) for h in horses}
    return {k: v / s for k, v in raw.items()}


# --- LightGBM lambdarank integration ---

_LGBM_MODEL = None  # lazy-loaded
_LGBM_META = None
_LGBM_LOAD_TRIED = False


def lgbm_status() -> dict:
    """LightGBM 学習済モデルがロード可能かを返す。

    フィールド:
      available: True なら estimate_probs が lgbm を使う、False なら linear softmax fallback
      feature_cols: 使用する FeatureVec フィールド名
      n_features: feature_cols の長さ
      model_path: モデルファイルパス (存在する場合)
      load_error: 失敗時のエラーメッセージ
    """
    global _LGBM_MODEL, _LGBM_META, _LGBM_LOAD_TRIED
    out: dict = {"available": False, "feature_cols": [], "n_features": 0}
    if _LGBM_MODEL is None and not _LGBM_LOAD_TRIED:
        _LGBM_LOAD_TRIED = True
        try:
            import json as _json
            from pathlib import Path as _Path
            import lightgbm as _lgb
            root = _Path(__file__).resolve().parents[1]
            mp = root / "data" / "models" / "lgbm_lambdarank.txt"
            meta = root / "data" / "models" / "lgbm_metadata.json"
            if not mp.exists() or not meta.exists():
                out["load_error"] = f"model files missing: {mp.name} / {meta.name}"
                return out
            _LGBM_MODEL = _lgb.Booster(model_file=str(mp))
            _LGBM_META = _json.loads(meta.read_text(encoding="utf-8"))
            out["model_path"] = str(mp.relative_to(root))
        except Exception as ex:  # noqa: BLE001
            out["load_error"] = str(ex)[:200]
            return out
    if _LGBM_MODEL is not None and _LGBM_META is not None:
        out["available"] = True
        cols = _LGBM_META.get("feature_cols", [])
        out["feature_cols"] = cols
        out["n_features"] = len(cols)
        from pathlib import Path as _Path
        root = _Path(__file__).resolve().parents[1]
        out["model_path"] = "data/models/lgbm_lambdarank.txt"
        # trained_at / num_iters があれば付ける
        out["trained_at"] = _LGBM_META.get("trained_at")
        out["n_iterations"] = _LGBM_META.get("num_iterations") or _LGBM_META.get("n_iterations")
    return out


def _lgbm_predict(horses, feats) -> dict[int, float] | None:
    """LightGBM 学習済モデルがあれば feats 行列で score 予測 → softmax で 1 着確率。

    返り値 None: モデルなし or 予測失敗 (呼び出し側がフォールバック)。
    """
    global _LGBM_MODEL, _LGBM_META, _LGBM_LOAD_TRIED
    if _LGBM_MODEL is None and not _LGBM_LOAD_TRIED:
        _LGBM_LOAD_TRIED = True
        try:
            import json as _json
            from pathlib import Path as _Path
            import lightgbm as _lgb
            root = _Path(__file__).resolve().parents[1]
            mp = root / "data" / "models" / "lgbm_lambdarank.txt"
            meta = root / "data" / "models" / "lgbm_metadata.json"
            if not mp.exists() or not meta.exists():
                return None
            _LGBM_MODEL = _lgb.Booster(model_file=str(mp))
            _LGBM_META = _json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            return None
    if _LGBM_MODEL is None or _LGBM_META is None:
        return None

    try:
        import math
        from dataclasses import asdict as _asdict
        feature_cols = _LGBM_META.get("feature_cols", [])
        if not feature_cols:
            return None
        rows = []
        nums = []
        for h in horses:
            fv = feats.get(h.number)
            if fv is None:
                continue
            d = _asdict(fv)
            # 文字列キーで欠落フィールドは 0.0
            row = [float(d.get(c, 0.0) or 0.0) for c in feature_cols]
            rows.append(row)
            nums.append(h.number)
        if not rows:
            return None
        scores = _LGBM_MODEL.predict(rows)
        # 温度スケーリング付き softmax: probs = softmax(score / T)
        # T < 1 で sharpening (Phase 21)。holdout で T=0.4 が log loss 最小。
        T = max(LGBM_TEMPERATURE, 1e-3)
        scaled = [s / T for s in scores]
        m = max(scaled)
        exps = [math.exp(s - m) for s in scaled]
        z = sum(exps)
        if z <= 0:
            return None
        return {n: e / z for n, e in zip(nums, exps)}
    except Exception:
        return None


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


# ---------- 馬連 / ワイド / 馬単 / 3 連複 確率 ----------


def win_prob(key: tuple[int, ...], probs: Probabilities) -> float:
    """単勝 (i 1着) の的中確率。"""
    if len(key) != 1:
        return 0.0
    return probs.win.get(key[0], 0.0)


def place_prob(key: tuple[int, ...], probs: Probabilities) -> float:
    """複勝 (i が 3 着以内) の的中確率。

    Plackett-Luce 連鎖を 1-2-3 着の全順序組み合わせで marginalize:
      P(i in top3) = Σ_{(a,b,c) where i ∈ {a,b,c}} P(a=1, b=2, c=3)
    """
    if len(key) != 1:
        return 0.0
    i = key[0]
    if probs.win.get(i, 0.0) <= 0:
        return 0.0
    horse_set = list(probs.win.keys())
    total = 0.0
    for a in horse_set:
        for b in horse_set:
            if b == a:
                continue
            for c in horse_set:
                if c == a or c == b:
                    continue
                if i == a or i == b or i == c:
                    total += trifecta_prob((a, b, c), probs)
    return total


def _exacta_prob_pair(i: int, j: int, probs: Probabilities) -> float:
    """P(i=1着, j=2着) を Plackett-Luce で。3 着以下は marginalize 済 (PL の連鎖は独立)。"""
    if i == j:
        return 0.0
    p1 = probs.win.get(i, 0.0)
    if p1 <= 0:
        return 0.0
    raw_b = probs.place2.get(j, 0.0)
    denom_b = sum(probs.place2.get(k, 0.0) for k in probs.win if k != i)
    if denom_b <= 0:
        return 0.0
    return p1 * raw_b / denom_b


def exacta_prob(key: tuple[int, ...], probs: Probabilities) -> float:
    """馬単 (1 着 i, 2 着 j) の的中確率。"""
    if len(key) != 2:
        return 0.0
    return _exacta_prob_pair(key[0], key[1], probs)


def quinella_prob(key: tuple[int, ...], probs: Probabilities) -> float:
    """馬連 (i, j 順不同) の的中確率 = P(i=1着, j=2着) + P(j=1着, i=2着)。"""
    if len(key) != 2:
        return 0.0
    i, j = key
    return _exacta_prob_pair(i, j, probs) + _exacta_prob_pair(j, i, probs)


def trio_prob(key: tuple[int, ...], probs: Probabilities) -> float:
    """3 連複 (i, j, k 順不同) の的中確率 = Σ_{perm of key} trifecta_prob(perm)。"""
    if len(key) != 3:
        return 0.0
    from itertools import permutations
    total = 0.0
    for perm in permutations(key):
        total += trifecta_prob(perm, probs)
    return total


def wide_prob(key: tuple[int, ...], probs: Probabilities) -> float:
    """ワイド (両馬 i, j とも 3 着以内) の的中確率。

    = Σ_{k ∉ {i,j}} P(3 連複 {i, j, k} 的中)
    """
    if len(key) != 2:
        return 0.0
    i, j = key
    total = 0.0
    for k in probs.win:
        if k == i or k == j:
            continue
        total += trio_prob((i, j, k), probs)
    return total


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


# bet_type → 確率関数の対応表
_BET_PROB_FN = {
    "win": win_prob,
    "place": place_prob,
    "quinella": quinella_prob,
    "wide": wide_prob,
    "exacta": exacta_prob,
    "trio": trio_prob,
}


def build_bet_table(
    bets: list[BetOdds],
    probs: Probabilities,
    bet_type: str | None = None,
) -> list[BetEvRow]:
    """汎用 bet オッズリスト → BetEvRow リスト (P×O 降順)。

    bet_type を渡すと確率関数を上書き。省略時は各 BetOdds.bet_type を見て分岐。
    """
    rows: list[BetEvRow] = []
    for b in bets:
        if b.absent or b.odds <= 0:
            continue
        bt = bet_type or b.bet_type
        prob_fn = _BET_PROB_FN.get(bt)
        if prob_fn is None:
            continue
        p = prob_fn(b.key, probs)
        pxo = p * b.odds
        rows.append(
            BetEvRow(
                bet_type=bt,
                key=b.key,
                odds=b.odds,
                popularity=b.popularity,
                prob=p,
                px_o=pxo,
                tier=_tier(pxo),
            )
        )
    rows.sort(key=lambda r: r.px_o, reverse=True)
    return rows


def build_all_bet_tables(
    rd: RaceData, probs: Probabilities
) -> dict[str, list[BetEvRow]]:
    """RaceData.other_bets に格納された全 bet type の EV table。空の bet type は省く。"""
    out: dict[str, list[BetEvRow]] = {}
    for bt, bets in (rd.other_bets or {}).items():
        if not bets:
            continue
        table = build_bet_table(bets, probs, bet_type=bt)
        if table:
            out[bt] = table
    return out


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


def plan_max_ev(rows: list[EvRow], min_prob: float = 0.0) -> list[EvRow]:
    """最高 EV (Plan B): 上位 1-3 点。

    `min_prob`: 確率下限 (例: 0.01 = 1%)。0 で従来挙動。
    holdout n=291 で min_prob=0 だと Plan B が 0/291 hit (extreme outsider 中心)
    だったため、UI 側で warning + 別途 prob floor を活用したい時はここを 0.01-0.02
    にする。eval は `eval_holdout` の Plan B variant test を参照。
    """
    pos = [r for r in rows if r.px_o >= PXO_FLOOR and r.prob >= min_prob]
    return pos[:3]


# ---------- Plan G: 適性ゲート → EV 足切り ----------

PLAN_G_DEFAULT_TOP_HORSES = 6
PLAN_G_MAX_POINTS = 10


def plan_aptitude_ev(
    rows: list[EvRow],
    aptitude_top_horses: list[int],
    *,
    pxo_floor: float = PXO_FLOOR,
    max_picks: int = PLAN_G_MAX_POINTS,
) -> list[EvRow]:
    """適性指数 top N 頭の集合内で生成される 3 連単のみ候補 → P×O >= floor で足切り。

    aptitude_top_horses: 適性総合上位の馬番リスト (上位順)。長さで対象集合を決める。
    EV (P×O) を「最終判断」として使うパイプライン。Plan A/B/C のような EV-first ではなく、
    まず適性で候補を絞り込んでから EV 足切りで「市場と整合する目」だけ残す。
    """
    if not aptitude_top_horses:
        return []
    top_set = set(aptitude_top_horses)
    out = [
        r for r in rows
        if r.key[0] in top_set
        and r.key[1] in top_set
        and r.key[2] in top_set
        and r.px_o >= pxo_floor
    ]
    # rows は既に px_o 降順で並んでいるので順序は保たれる
    return out[:max_picks]


def plan_aptitude_ev_bet(
    rows: list,  # list[BetEvRow]
    aptitude_top_horses: list[int],
    *,
    pxo_floor: float = PXO_FLOOR,
    max_picks: int = PLAN_G_MAX_POINTS,
) -> list:
    """BetEvRow 版 (馬連 / ワイド / 馬単 / 3 連複) の適性ゲート→EV足切り。

    key 全要素が aptitude_top_horses に入っているかでフィルタ。
    """
    if not aptitude_top_horses:
        return []
    top_set = set(aptitude_top_horses)
    out = [
        r for r in rows
        if all(k in top_set for k in r.key) and r.px_o >= pxo_floor
    ]
    return out[:max_picks]


# ---------- 既存 Plan ----------

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
