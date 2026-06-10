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

# 券種別の払戻率 (= 1 − 控除率)。市場効率なら P×O ≈ この値に張り付く。
# JRA は公式公表値 (2014-06 改定以降)。NAR は主催者により 70-80% で設定可だが
# 代表値 (船橋/高知等の現行スケジュール) を置く: 馬連/ワイドが 75% と JRA (77.5%)
# より重く、3連複も 72.5% と重い点に注意 (= NAR の pair 系は構造的に不利)。
# ※従来この箇所に「3連単は控除 22.5%」と書かれていたが誤り — 22.5% は馬連/ワイド系
#   (JRA) の控除率で、3連単の控除は JRA/NAR とも 27.5% (払戻率 72.5%)。
PAYOUT_RATE: dict[tuple[str, str], float] = {
    ("jra", "win"): 0.800, ("jra", "place"): 0.800,
    ("jra", "quinella"): 0.775, ("jra", "wide"): 0.775,
    ("jra", "exacta"): 0.750, ("jra", "trio"): 0.750,
    ("jra", "trifecta"): 0.725,
    ("nar", "win"): 0.800, ("nar", "place"): 0.800,
    ("nar", "quinella"): 0.750, ("nar", "wide"): 0.750,
    ("nar", "exacta"): 0.750, ("nar", "trio"): 0.725,
    ("nar", "trifecta"): 0.725,
}


def payout_rate(bet_type: str, segment: str = "jra") -> float:
    """券種×セグメント (jra/nar) の払戻率。未知の組合せは保守的に 0.725。"""
    return PAYOUT_RATE.get((segment, bet_type), 0.725)


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

# Claude 考察由来の「各馬の強さ指数」(0-100, 高い=強い) を model fundamental に合成する設定。
# 2段パイプライン: Claude が score ステージで指数を出力 → bet ステージで estimate_probs が
# `_combine_llm_index` で model fundamental と loglinear 合成 → さらに市場とブレンドする。
#   - LLM_BLEND_DEFAULT: 合成重み w (0=モデルのみ, 1=指数のみ)。
#   - T_LLM: raw 指数 (0-100) を softmax(v / T_LLM) で 1着率分布に変換する温度。
#            生値 (T=1) だと exp(100) で過尖鋭化し1頭に集中するため大きめの温度で平坦化する。
#            T=25 なら 100 vs 50 の差が win 比 ~7倍 (model の spread と同程度)。
# 【market_blend=0 contrarian 実験の終了 (2026-06-10)】
# 2026-06-01〜の「市場無視 (MARKET_BLEND_LIVE=0.0)」live 実験は実測で失敗が確定した:
#   - live 蓄積 N=146 の勝者 log-loss: market単独 1.595 < +Claude 1.629 < model+market 1.634
#     (scripts/validate_claude_value.py) — モデルは市場から価値を**引いて**いた。
#   - β=0 では past_runs 欠損時に fundamental が一様分布に縮退し、EV=odds/n で
#     「最長オッズを自動購入」する事故が実発生 (snapshot 2026650608-608-5: 全馬 prob=0.1000
#     のまま 単勝39.9倍/ワイド47.4倍 を購入)。
#   - 実弾系列の実測 ROI: EV束 69.7% (n=284) / 3連単束 claude 53.7% (n=99) /
#     recovery モード 14.9% (n=26) — 全系列が大幅マイナス。
# → live を市場アンカー (β=BLEND_DEFAULT=0.78) に復帰。Claude 指数・速度図表は
#   fundamental 側に残り「市場とズレる所」は px_o (P×O) として引き続き観測される。
# β の再推定は scripts/fit_blend_mle.py (Benter 2-step, α+β 自由) で live 蓄積データから
# 定期的に行うこと (N=146 時点の示唆は β≈1.0 = 市場ほぼ支配)。
#   - LLM_BLEND_DEFAULT=0.5: fundamental 段での model×Claude 合成重み (per-horse は support で
#     スケール = Claude が根拠を持つ馬だけ最大0.5 まで動かす)。
LLM_BLEND_DEFAULT = 0.5
MARKET_BLEND_LIVE = BLEND_DEFAULT
T_LLM = 25.0

# v2 速度図表 (実データ par+pace+trip, src/speed_chart.py) を LightGBM fundamental と
# **並列**に合成する設定 (live 実験, ユーザ指示 2026-06-02)。LightGBM の代替ではなく、
# fundamental 段で speed_v2_best の field 内分布と幾何 (loglinear) 平均を取る。
#   - SPEED_V2_BLEND_LIVE: 合成重み w (0=LGBM のみ, 1=図表のみ, 0.5=幾何平均=並列)。
#   - SPEED_V2_TEMP: speed_v2_best の field 内 z-score を softmax(z/T) で確率化する温度。
# 注意: test_speed_v2.py で v2 図表は β-MLE≈1.0 (= 市場超え不可・市場に織り込み済) と出ており、
# OOS の +EV 根拠はまだ無い (要レース蓄積後 sweep)。これは live 検証用の配線。backtest 経路は
# estimate_probs(speed_v2_blend=0.0 既定) のままなので holdout 参照値 (β=0.78 等) は不変。
SPEED_V2_BLEND_LIVE = 0.5
SPEED_V2_TEMP = 1.0


# ---------- 確率推定 ----------


def estimate_probs(
    rd: RaceData,
    *,
    market_blend: float = BLEND_DEFAULT,
    market_floor: float = 0.01,
    speed_v2_blend: float = 0.0,   # >0 で v2 速度図表を LightGBM fundamental と並列合成 (live)
    blend_method: str = "loglinear",   # "loglinear" (Benter 2-step) | "linear" (旧)
    lambda_2: float | None = None,   # None = segment metadata の lambda_2_mle (無ければ DEFAULT)
    lambda_3: float | None = None,
    use_show_bias: bool = True,
    market_win_override: dict[int, float] | None = None,
    llm_win_index: dict[int, float] | None = None,
    llm_blend: float = LLM_BLEND_DEFAULT,
    llm_support: dict[int, int] | None = None,
    llm_scale: str = "strength",
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

    # JRA / NAR で別モデル + 別 (T, λ) を使う。segment metadata から λ を解決
    # (lambda_2/3=None のとき。自前 MLE 校正値 lambda_*_mle、無ければ literature DEFAULT)。
    segment = segment_of_rd(rd)
    _seg_meta = _segment_booster_meta(segment)[1] or {}
    if lambda_2 is None:
        lambda_2 = float(_seg_meta.get("lambda_2_mle") or DEFAULT_LAMBDA_2)
    if lambda_3 is None:
        lambda_3 = float(_seg_meta.get("lambda_3_mle") or DEFAULT_LAMBDA_3)

    feats = build_features(rd)
    fundamental_win = _fundamental_win_probs(horses, feats, segment, speed_v2_blend=speed_v2_blend)

    # Claude 指数を model fundamental に合成 (市場ブレンドの前)。指数が無ければ no-op。
    if llm_win_index and llm_blend > 0:
        fundamental_win = _combine_llm_index(
            fundamental_win, llm_win_index, llm_blend, market_floor,
            support=llm_support, scale=llm_scale)

    # 市場ブレンド。market_win_override があれば trifecta より優先 (oddspark 単勝
    # フォールバック等、3連単オッズが無い経路で単勝オッズから市場率を渡す用途)。
    # de-vig (power method) には **未正規化** の暗黙率 (Σ=overround>1) が必要。
    # 正規化済みを渡すと k=1 (恒等写像) に縮退して favorite-longshot bias 補正が
    # 一切効かない (2026-06-10 修正)。override も呼び出し元が未正規化 1/odds を渡す。
    win = fundamental_win
    if market_blend > 0 and (market_win_override or rd.trifecta):
        market_raw = market_win_override or market_win_probs(rd.trifecta, normalize=False)
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
    """Power-method de-overround (Clarke 2017): Σ p_i^(1/k) = 1 を満たす k を bisection で解く。

    raw_probs は **未正規化の暗黙率 1/odds** (= Σ=overround>1) を渡すこと。
    Σ=1 に正規化済みの入力では解が厳密に k=1 (恒等写像) になり、de-vig も
    favorite-longshot bias 補正も一切働かない (2026-06-10 に発覚した no-op バグ)。
    未正規化入力なら 1/k>1 の power 縮小が「低確率ほど相対的に大きく削る」形で
    働き、本命寄せ (FLB の古典補正) になる。
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


def _loglinear_blend(f: dict[int, float], g: dict[int, float], w: float) -> dict[int, float]:
    """2 つの確率分布を幾何 (loglinear) 合成: c_i ∝ f_i^(1-w) · g_i^w。Σ=1 に正規化。

    g に無い馬は w_eff=0 (f のまま) で扱い、不当な抑制を避ける。
    """
    import math

    w = max(min(w, 1.0), 0.0)
    logs: dict[int, float] = {}
    for k in set(f) | set(g):
        a = max(f.get(k, 0.0), 1e-9)
        if k in g:
            b = max(g[k], 1e-9)
            logs[k] = (1.0 - w) * math.log(a) + w * math.log(b)
        else:
            logs[k] = math.log(a)
    m = max(logs.values())
    exps = {k: math.exp(v - m) for k, v in logs.items()}
    s = sum(exps.values()) or 1.0
    return {k: v / s for k, v in exps.items()}


def _fundamental_win_probs(horses, feats, segment: str | None = None,
                           *, speed_v2_blend: float = 0.0) -> dict[int, float]:
    """Layer 1 特徴量 → softmax で 1 着確率。

    segment ("jra"/"nar") があれば該当 LightGBM モデルを使う (無ければ global)。
    モデルが無ければ z-score 線形和を softmax する Phase A 初期版にフォールバック。

    speed_v2_blend>0 のとき、馬柱から算出した v2 速度図表 (speed_chart.speed_v2_win_probs)
    を上記 fundamental と並列に幾何合成する (live 実験)。図表データが薄ければ no-op。
    """
    lgb_probs = _lgbm_predict(horses, feats, segment)
    base = lgb_probs if lgb_probs is not None else _linear_softmax_fallback(horses, feats)
    if speed_v2_blend and speed_v2_blend > 0:
        try:
            from .speed_chart import speed_v2_win_probs
            sv2 = speed_v2_win_probs(horses, temperature=SPEED_V2_TEMP)
        except Exception:
            sv2 = None
        if sv2:
            base = _loglinear_blend(base, sv2, speed_v2_blend)
    return base


def fundamental_no_info(rd, *, speed_v2_blend: float = 0.0) -> bool:
    """fundamental (市場・Claude 指数を入れる前のモデル確率) が無情報 (一様縮退) かを判定。

    past_runs が無い等で特徴量が全馬同値だと fundamental は厳密に一様分布になる。
    一様 fundamental を市場とブレンドすると「市場の平坦化 = 大穴の確率を機械的に
    持ち上げる」ことになり、大穴側に偽の +EV (px_o>1) が出る (β=0 では EV=odds/n で
    最長オッズ自動購入になっていた)。EV束はこの状態では組んではいけない — 呼び出し側
    (analyze) が True のとき recommended_bundle を見送りにする。
    """
    horses = [h for h in rd.race.horses if not h.absent]
    if not horses:
        return True
    from .features import build_features

    feats = build_features(rd)
    f = _fundamental_win_probs(horses, feats, segment_of_rd(rd), speed_v2_blend=speed_v2_blend)
    if not f:
        return True
    vals = list(f.values())
    return (max(vals) - min(vals)) < 1e-9


# 補強根拠件数 (support) → llm_blend に掛ける per-horse 係数。根拠の無い馬は 0 (= モデル/市場に
# 委ねる)、根拠が増えるほど Claude の指数を厚く採用する。3 件以上で満額。
_SUPPORT_WEIGHT = {0: 0.0, 1: 0.5, 2: 0.8}


def _support_mult(support: int | None) -> float:
    if support is None:
        return 1.0   # support 情報が無い (旧形式/未指定) → 一律 llm_blend
    return _SUPPORT_WEIGHT.get(max(0, support), 1.0)


def _combine_llm_index(
    fundamental: dict[int, float],
    llm_index: dict[int, float],
    llm_blend: float,
    floor: float,
    *,
    support: dict[int, int] | None = None,
    scale: str = "strength",
) -> dict[int, float]:
    """Claude の per-horse 指数/勝率を model fundamental と loglinear 合成する。

    scale="strength" (正規): llm_index は 0-100 強さ指数 (市場独立の相対評価)。
        softmax(v/T_LLM) で確率化する。
    scale="prob" (後方互換): llm_index は推定勝率 (%, Σ≈100)。正規化して L_i にそのまま使う
        (温度なし)。

    合成は per-horse 重み付き loglinear:
        g_i = softmax((1-w_i)·log f_i + w_i·log L_i)
        w_i = clamp(llm_blend · support_mult(support_i))   (support 無し → w_i=llm_blend)

    補強根拠 (support) が多い馬ほど w_i が大きく Claude の指数を厚く採る。根拠 0 の馬は
    w_i=0 で fundamental のまま (= 検索で動かした馬だけ反映)。空なら fundamental を返す。
    """
    import math

    keys = list(fundamental.keys())
    if not llm_index or not keys:
        return fundamental

    # Claude が実際にスコアした馬の集合。未スコア馬は blend しない (w=0 で fundamental のまま)
    # — そうしないと L=floor に引っ張られて未スコア馬が不当に抑制される。
    scored = {k for k in keys if k in llm_index}
    raw = {k: max(float(llm_index.get(k, 0.0)), 0.0) for k in keys}
    if scale == "strength":
        # 旧 0-100 指数 → 温度付き softmax で確率化
        rm = max(raw.values()) if raw else 0.0
        exps = {k: math.exp((v - rm) / T_LLM) for k, v in raw.items()}
        z = sum(exps.values()) or 1.0
        L = {k: max(exps[k] / z, floor) for k in keys}
    else:
        # 新: 勝率 % をそのまま正規化 (欠落/0 は floor)
        L = {k: max(raw.get(k, 0.0), floor) for k in keys}
    ls = sum(L.values()) or 1.0
    L = {k: v / ls for k, v in L.items()}

    base_w = max(min(llm_blend, 1.0), 0.0)
    logs: dict[int, float] = {}
    for k in keys:
        if k not in scored:
            w = 0.0   # Claude が触れていない馬は動かさない
        else:
            w = max(min(base_w * _support_mult(None if support is None else support.get(k, 0)), 1.0), 0.0)
        f = max(fundamental.get(k, 0.0), 1e-9)
        l = max(L.get(k, 0.0), 1e-9)
        logs[k] = (1.0 - w) * math.log(f) + w * math.log(l)
    mm = max(logs.values())
    e = {k: math.exp(v - mm) for k, v in logs.items()}
    s = sum(e.values()) or 1.0
    return {k: v / s for k, v in e.items()}


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


# セグメント別モデル (JRA / NAR)。scripts/train_segment_models.py が
# data/models/lgbm_<seg>.txt + lgbm_<seg>_metadata.json を作る。metadata に
# softmax_temperature / lambda_2_mle / lambda_3_mle / market_blend_mle (別 partition で MLE 凍結) を持つ。
# 無い segment は global モデルにフォールバック。
_SEG_CACHE: dict[str, tuple] = {}   # segment -> (booster, meta) | (None, None)


def segment_of_rd(rd) -> str:
    """race の venue から JRA(中央, venue 01-10) / NAR(地方) を判定。"""
    try:
        vid = int(getattr(rd.race, "venue_id", 0) or 0)
    except (TypeError, ValueError):
        return "nar"
    return "jra" if 1 <= vid <= 10 else "nar"


def _segment_booster_meta(segment: str | None):
    """segment 別 (booster, meta) を返す。無ければ global にフォールバック。"""
    # global を必ずロード (フォールバック先)
    lgbm_status()
    if not segment:
        return _LGBM_MODEL, _LGBM_META
    if segment not in _SEG_CACHE:
        try:
            import json as _json
            from pathlib import Path as _Path
            import lightgbm as _lgb
            root = _Path(__file__).resolve().parents[1]
            mp = root / "data" / "models" / f"lgbm_{segment}.txt"
            meta = root / "data" / "models" / f"lgbm_{segment}_metadata.json"
            if mp.exists() and meta.exists():
                _SEG_CACHE[segment] = (
                    _lgb.Booster(model_file=str(mp)),
                    _json.loads(meta.read_text(encoding="utf-8")),
                )
            else:
                _SEG_CACHE[segment] = (None, None)
        except Exception:
            _SEG_CACHE[segment] = (None, None)
    b, m = _SEG_CACHE[segment]
    if b is not None and m is not None:
        return b, m
    return _LGBM_MODEL, _LGBM_META


def _lgbm_predict(horses, feats, segment: str | None = None) -> dict[int, float] | None:
    """LightGBM 学習済モデルがあれば feats 行列で score 予測 → softmax で 1 着確率。

    segment ("jra"/"nar") が与えられ該当モデルがあればそれを使う (無ければ global)。
    返り値 None: モデルなし or 予測失敗 (呼び出し側がフォールバック)。
    """
    booster, meta = _segment_booster_meta(segment)
    if booster is None or meta is None:
        return None

    try:
        import math
        from dataclasses import asdict as _asdict
        feature_cols = meta.get("feature_cols", [])
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
        scores = booster.predict(rows)
        # 温度スケーリング付き softmax: probs = softmax(score / T)。T は model-specific で
        # 各 metadata の softmax_temperature を優先、無ければ LGBM_TEMPERATURE にフォールバック。
        T = float(
            (meta or {}).get("softmax_temperature")
            or LGBM_TEMPERATURE
        )
        T = max(T, 1e-3)
        scaled = [s / T for s in scores]
        m = max(scaled)
        exps = [math.exp(s - m) for s in scaled]
        z = sum(exps)
        if z <= 0:
            return None
        return {n: e / z for n, e in zip(nums, exps)}
    except Exception:
        return None


def market_win_probs(
    trifecta: Iterable[TrifectaOdds], *, normalize: bool = True
) -> dict[int, float]:
    """3 連単オッズを 1 着で marginalize した market-implied 1 着率。

    `sum_{b,c} 1/odds(a,b,c)` を計算する。normalize=True で全体を Σ=1 に正規化
    (控除率は正規化で吸収)。**de-vig (power_method_overround) に渡す場合は
    normalize=False の未正規化値 (Σ=overround>1) を使うこと** — 正規化済みを
    渡すと power method が k=1 の恒等写像に縮退し FLB 補正が効かない。
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
    if not normalize:
        return raw
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


def build_all_bet_tables_hit(
    rd: RaceData, probs: Probabilities, *, min_pxo: float = 0.0
) -> dict[str, list[BetEvRow]]:
    """**的中優先版** EV table: 各 bet type を **prob 降順** に並べ替える。

    回収優先版 (build_all_bet_tables) は P×O 降順なので、当て率が低い大穴が優位になりがち。
    的中優先は **EV 関係なく確率の高い順**に並べることで「賭けが当たりやすい」picks を
    抽出する用途 (2026-05-30 ユーザ指示: 的中優先は EV を見ない)。既定 min_pxo=0.0 = 足切りなし。
    """
    out: dict[str, list[BetEvRow]] = {}
    for bt, bets in (rd.other_bets or {}).items():
        if not bets:
            continue
        table = build_bet_table(bets, probs, bet_type=bt)
        # px_o フィルタ (既定 0.0 = 足切りなし。的中優先は EV を見ない)
        filtered = [r for r in table if r.px_o >= min_pxo]
        # prob 降順に並べ替え
        filtered.sort(key=lambda r: r.prob, reverse=True)
        if filtered:
            out[bt] = filtered
    return out


def trifecta_to_bet_evrow(rows: list[EvRow]) -> list[BetEvRow]:
    """3連単 EvRow → BetEvRow (bet_type='trifecta') 変換。他券種と並べて表示する用。"""
    return [
        BetEvRow(
            bet_type="trifecta",
            key=tuple(r.key),
            odds=r.odds,
            popularity=r.popularity,
            prob=r.prob,
            px_o=r.px_o,
            tier=r.tier,
        )
        for r in rows
    ]


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
    """Plan H1 (旧): 推定 P 上位 N 点。EV 不問。
    新スキーマでは `plan_b_trifecta` (新 Plan B / 的中優先) として参照される。"""
    return sorted(rows, key=lambda r: r.prob, reverse=True)[:target]


def plan_hit_safe(rows: list[EvRow], target: int = 3) -> list[EvRow]:
    """Plan H2 (旧, 廃止予定): 推定 P 上位 N 点のうち P×O >= 1.0 のみ。"""
    cand = [r for r in rows if r.px_o >= 1.0]
    return sorted(cand, key=lambda r: r.prob, reverse=True)[:target]


# ---------- 新スキーマ (3連単 Plan A/B) -----------------------------------
# 旧 Plan A/B/C/F/G/H1/H2 は廃止。3連単を「他の券種と同じ EV 解析結果」として
# 扱い、回収優先 (P×O 降順) / 的中優先 (確率降順) の 2 plan に集約する。

def plan_a_trifecta(rows: list[EvRow], target: int = 5) -> list[EvRow]:
    """**新 Plan A** (3連単・回収優先): 3連単 EV table を P×O 降順で上位 target 点。

    他の券種 (単複/馬連/ワイド/馬単/3連複) の EV table と同じく
    「+EV の上位 N」を素直に取る。`rows` は既に px_o 降順想定 (build_table の出力)。
    PXO_FLOOR (= 1.02) で楽観バイアス分の足切りをして、生 EV カラム順に N 点。
    """
    pos = [r for r in rows if r.px_o >= PXO_FLOOR]
    return pos[:target]


def plan_b_trifecta(rows: list[EvRow], target: int = 3) -> list[EvRow]:
    """**新 Plan B** (3連単・的中優先): 推定 P 上位 target 点。EV 不問。

    旧 plan_hit_pure (Plan H1) と同じロジック (リネーム)。確率が最も高い目を
    抑え、当てに行く戦略。実機 holdout で 4/7 hits (60-70% ROI) の実績あり。
    """
    return sorted(rows, key=lambda r: r.prob, reverse=True)[:target]


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
