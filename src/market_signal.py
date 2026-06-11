"""市場乖離アノマリ検出: 単勝オッズと複勝オッズの implied prob 比率から
「1 着型」「3 着型」「標準」「極端」を per-horse で判定する。

哲学:
  - 市場が「この馬は 1 着になる」と「この馬は top 3 に入る」を別々にオッズ付けする。
  - 両者の implied prob 比率 (place / win) が異常に高い → 市場が「3 着までは堅い
    が 1 着は無い」と見ている = **3 着スペシャリスト 候補** (Plan G の格好の餌食)。
  - 比率が異常に低い → 市場が「1 着 or 着外」と見ている = ハイリスク馬。

EV 計算には直接組み込まず、horse table の overlay として表示する。
LLM や手動判断で「3 着型」を 2/3 着スロットに置く判断材料として活用。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Horse, RaceData

# 市場の控除率を粗く吸収するための係数
# 単勝控除率 ≒ 20%, 複勝控除率 ≒ 20% (中央)。同じ係数で打ち消す。
_TAKEOUT_WIN = 0.80
_TAKEOUT_PLACE = 0.80


@dataclass
class MarketSignal:
    """1 馬の市場乖離指標。

    win_implied / place_implied は **takeout を粗く吸収した** 暗黙確率。
    place_to_win_ratio = place_implied / win_implied (top 3 / win の倍率)。
    interpretation: "1着型" / "標準" / "3着型" / "極端" / "不明"。
    """
    number: int
    win_odds: float = 0.0
    place_odds_min: float = 0.0          # 複勝下限 (fuku_min)
    win_implied: float = 0.0             # 1 着 暗黙確率 (de-overround 後の概算)
    place_implied: float = 0.0           # 3 着以内 暗黙確率
    place_to_win_ratio: float = 0.0      # 倍率
    interpretation: str = "不明"

    @property
    def is_third_special(self) -> bool:
        """3 着スペシャリスト 候補 (place / win 比が異常に高い)。"""
        return self.interpretation == "3着型"

    @property
    def is_win_prone(self) -> bool:
        """1 着 prone (place / win 比が異常に低い)。"""
        return self.interpretation == "1着型"


def _interpret(ratio: float, n_horses: int, n_pos: int = 3) -> str:
    """place/win 比率を「型」に分類。

    閾値の根拠 (16 頭 race のとき):
      - place top 3 / win top 1 の理論比 = 3 (各馬同確率なら)
      - 強い馬では比 ≒ 1.5-2 (win が支配的、place も近い)
      - 中位馬では比 ≒ 2.5-3.5 (順当)
      - 弱小馬では比 ≒ 5-10+ (place の方が大きく見える = 3 着候補ありえる)
    field_size で動的に閾値調整。n_pos (払戻ポジション数: 8頭以上=3 / 7頭以下=2) で
    閾値をスケール (2026-06-11 第5R: 7頭以下は理論比 2 なので 3 基準の閾値が歪む)。
    """
    if ratio <= 0:
        return "不明"
    # 各馬等確率の理論比 = n_pos (ポジション数 / 1 ポジション)
    # ただし n_horses 大きいほど分散が大きい
    scale = n_pos / 3.0
    extreme_low = 1.5 * scale
    typical_low = 2.0 * scale
    typical_high = 4.0 * scale
    extreme_high = 7.0 * scale
    if ratio < extreme_low:
        return "極端"  # 1 着のみで place の伸びしろが小 (= 1 着取らないと終わり)
    if ratio < typical_low:
        return "1着型"
    if ratio < typical_high:
        return "標準"
    if ratio < extreme_high:
        return "3着型"
    return "極端"  # ratio 極大 = 市場が「3 着までならあり得るが 1 着は超薄」


def _power_method_overround(probs: list[float], *, target: float = 1.0,
                            max_iter: int = 30, tol: float = 1e-6) -> list[float]:
    """raw 暗黙確率 (合計 > target) を power-method で de-overround。

    Σ p_i^(1/k) = target となる k を bisection で解いて p_i^(1/k) を返す。
    ev.py:power_method_overround と同じロジック (簡易版)。複勝は target=ポジション数
    (3 ないし 2) で解くこと — 「Σ=1 で解いてから ×3」は本命/穴の比率を大きく潰す
    (2026-06-11 bughunt 第5R: 単勝用の目標和 1 で解くと k が過大になり過平坦化)。
    """
    if not probs:
        return probs
    items = [p for p in probs if p > 0]
    if not items:
        return probs

    def f(k: float) -> float:
        try:
            return sum(p ** (1.0 / k) for p in items) - target
        except (ValueError, OverflowError):
            return float("inf")

    lo, hi = 0.5, 3.0
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == f_hi or f_lo * f_hi > 0:
        # bracket できなかったら overround を比例配分で target に揃える
        s = sum(items)
        return [p / s * target if p > 0 else 0.0 for p in probs]
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
    return [(max(p, 0.0)) ** (1.0 / k) for p in probs]


def compute_market_signals(rd: RaceData) -> dict[int, MarketSignal]:
    """RaceData から全馬の市場乖離指標を計算。

    win_odds は Horse.win_odds を採用。place は rd.other_bets["place"] の odds (fuku_min) を使う。
    どちらかが欠けてる馬は interpretation="不明"。
    """
    horses = [h for h in rd.race.horses if not h.absent]
    if not horses:
        return {}
    n = len(horses)

    # place odds をマップ化
    place_odds_by_num: dict[int, float] = {}
    for b in rd.other_bets.get("place", []):
        if len(b.key) == 1 and b.odds > 0:
            place_odds_by_num[b.key[0]] = b.odds

    # 単勝・複勝 raw implied
    raw_wins: dict[int, float] = {}
    raw_places: dict[int, float] = {}
    for h in horses:
        if h.win_odds > 0:
            raw_wins[h.number] = 1.0 / h.win_odds
        po = place_odds_by_num.get(h.number)
        if po and po > 0:
            raw_places[h.number] = 1.0 / po

    # de-overround (合計を 1 ないし 3 に揃える)
    win_nums = list(raw_wins)
    win_vals = [raw_wins[n_] for n_ in win_nums]
    win_normed = _power_method_overround(win_vals)
    # win の合計は 1 にしたい
    s = sum(win_normed)
    if s > 0:
        win_normed = [v / s for v in win_normed]
    win_implied = dict(zip(win_nums, win_normed))

    # 複勝の払戻ポジション数 (頭数ルール: 8頭以上=3 / 5-7頭=2 / 4頭以下=発売なし)
    n_pos = 3 if n > 7 else (2 if n > 4 else 0)
    place_nums = list(raw_places)
    place_vals = [raw_places[n_] for n_ in place_nums]
    if n_pos:
        # 目標和 = ポジション数 で直接 de-vig する (Σ=1 で解いてから ×3 は過平坦化)
        place_normed = _power_method_overround(place_vals, target=float(n_pos))
        s = sum(place_normed)
        if s > 0:
            place_normed = [v / s * n_pos for v in place_normed]
    else:
        place_normed = [0.0] * len(place_vals)   # 発売なし → place implied は作らない
    place_implied = dict(zip(place_nums, place_normed))

    out: dict[int, MarketSignal] = {}
    for h in horses:
        wi = win_implied.get(h.number, 0.0)
        pi = place_implied.get(h.number, 0.0)
        po_min = place_odds_by_num.get(h.number, 0.0)
        ratio = pi / wi if wi > 0 else 0.0
        sig = MarketSignal(
            number=h.number,
            win_odds=h.win_odds,
            place_odds_min=po_min,
            win_implied=wi,
            place_implied=pi,
            place_to_win_ratio=ratio,
            interpretation=_interpret(ratio, n, n_pos) if wi > 0 and pi > 0 and n_pos else "不明",
        )
        out[h.number] = sig
    return out
