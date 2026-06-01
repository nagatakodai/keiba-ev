"""ev._combine_llm_index (Claude 勝率% + support 重み合成) の単体テスト。

カバー:
  - prob スケール: 温度なしで Claude 勝率を直接使う
  - support 重み: 根拠 0 の馬は動かさない / 根拠多いほど厚く採用
  - 未スコア馬は blend されない (floor へ抑制されない) — regression
  - マイナス材料で favorite を下げられる
  - 旧 strength スケールの後方互換
"""
from __future__ import annotations

from src.ev import _combine_llm_index


def _argmax(d):
    return max(d, key=d.get)


def test_prob_scale_sums_to_one():
    f = {1: 0.5, 2: 0.3, 3: 0.2}
    g = _combine_llm_index(f, {1: 30.0, 2: 30.0, 3: 40.0}, 0.5, 0.01, scale="prob")
    assert abs(sum(g.values()) - 1.0) < 1e-9


def test_prob_scale_no_temperature_uses_values_directly():
    # blend=1.0 (Claude 完全採用), support 満額 → Claude 勝率の順位がそのまま出る
    f = {1: 0.5, 2: 0.3, 3: 0.2}
    g = _combine_llm_index(
        f, {1: 10.0, 2: 30.0, 3: 60.0}, 1.0, 0.01,
        support={1: 3, 2: 3, 3: 3}, scale="prob",
    )
    # Claude は 3 を最強としたので合成も 3 が最大
    assert _argmax(g) == 3
    assert g[3] > g[2] > g[1]


def test_support_zero_keeps_fundamental():
    # 根拠 0 の馬は Claude の勝率を無視して fundamental のまま
    f = {1: 0.5, 2: 0.3, 3: 0.2}
    # Claude は 3 を持ち上げるが support=0 → 動かないはず
    g = _combine_llm_index(
        f, {1: 20.0, 2: 20.0, 3: 60.0}, 0.5, 0.01,
        support={1: 0, 2: 0, 3: 0}, scale="prob",
    )
    # 全馬 support 0 → fundamental と同じ順位・ほぼ同値
    assert _argmax(g) == 1
    assert abs(g[1] - 0.5) < 1e-6
    assert abs(g[3] - 0.2) < 1e-6


def test_support_weight_monotonic():
    # 同じ Claude 勝率でも support が多い馬ほど fundamental から強く動く
    f = {1: 0.34, 2: 0.33, 3: 0.33}
    llm = {1: 60.0, 2: 20.0, 3: 20.0}   # Claude は 1 を持ち上げ
    g_lo = _combine_llm_index(f, llm, 0.5, 0.01, support={1: 1, 2: 0, 3: 0}, scale="prob")
    g_hi = _combine_llm_index(f, llm, 0.5, 0.01, support={1: 3, 2: 0, 3: 0}, scale="prob")
    # support 3 のほうが 1 の確率がより上がる
    assert g_hi[1] > g_lo[1] > f[1]


def test_unscored_horse_not_suppressed():
    # regression: Claude が触れていない馬 (llm_index に無い) は floor へ引っ張られない。
    # support=None (空 support→None) の経路でも未スコア馬は w=0 で fundamental を保つ。
    f = {1: 0.5, 2: 0.3, 3: 0.2}
    g = _combine_llm_index(f, {2: 40.0, 3: 60.0}, 0.3, 0.01, support=None, scale="prob")
    # 未スコアの 1 は依然として最大 (floor 抑制が起きると 1 が最小になる)
    assert _argmax(g) == 1
    assert g[1] > 0.4


def test_negative_evidence_lowers_favorite():
    # 検索で favorite を下げる材料 → support 厚め → 合成で favorite が下がる
    f = {1: 0.5, 2: 0.3, 3: 0.2}
    g = _combine_llm_index(
        f, {1: 15.0, 2: 35.0, 3: 50.0}, 0.5, 0.01,
        support={1: 3, 2: 1, 3: 3}, scale="prob",
    )
    assert g[1] < f[1]            # favorite が下がった
    assert _argmax(g) != 1        # もう最強ではない


def test_strength_scale_backward_compat():
    # 旧 0-100 強さ指数は温度パスで確率化されても動く (順位保存)
    f = {1: 0.34, 2: 0.33, 3: 0.33}
    g = _combine_llm_index(f, {1: 90.0, 2: 58.0, 3: 40.0}, 0.5, 0.01, scale="strength")
    assert _argmax(g) == 1
    assert abs(sum(g.values()) - 1.0) < 1e-9


def test_empty_index_returns_fundamental():
    f = {1: 0.5, 2: 0.5}
    assert _combine_llm_index(f, {}, 0.5, 0.01, scale="prob") == f


def test_market_win_index_temperature_shape():
    """市場指数: 1.0倍→100、単調減少、T=1.5 のべき乗形 (analyze._market_win_index)。"""
    from types import SimpleNamespace
    from src.analyze import _market_win_index, MARKET_INDEX_T

    def H(number, odds, absent=False):
        return SimpleNamespace(number=number, win_odds=odds, absent=absent)

    rd = SimpleNamespace(race=SimpleNamespace(horses=[
        H(1, 1.0), H(2, 2.0), H(3, 5.0), H(4, 10.0), H(5, 0.0), H(6, 3.0, absent=True),
    ]))
    idx = _market_win_index(rd)
    assert idx[1] == 100.0                       # 1.0倍は必ず 100 (圧倒的)
    assert 5 not in idx and 6 not in idx          # オッズ0 / 取消は除外
    assert idx[1] > idx[2] > idx[3] > idx[4]      # オッズ大ほど低い (単調)
    # T=1.5 のべき乗: 2.0倍 = 100*0.5^(1/1.5) ≈ 63
    assert abs(idx[2] - 63.0) < 0.5
    assert MARKET_INDEX_T == 1.5
