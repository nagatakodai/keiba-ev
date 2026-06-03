"""同時 (joint) Kelly による「まとめ買い」最適ポートフォリオ。

単一レースの bet 群は相関する (同じ着順結果に賭けている)。独立 Kelly を
足し合わせると over-bet になり、束全体の的中率も過大評価する。本モジュールは
レースの **完全な top-3 結果分布** (全 ordered triple, Σp=1) の上で束全体の
E[log(資金)] を最大化する、成長率最適 (growth-optimal) な配分を解く。

outcome space:
    Ω = {(a, b, c) : a,b,c は相異なる馬番}   (1着=a, 2着=b, 3着=c)
    P(a,b,c) = trifecta_prob((a,b,c), probs)  ← Plackett-Luce 連鎖
    Σ_Ω P = 1  (win が 1 に正規化済 + PL 連鎖の性質より厳密に 1)

この (a,b,c) から全 bet type の payoff が一意に決まる (truncation なし):
    単勝   hit ⟺ key == (a,)
    複勝   hit ⟺ key[0] ∈ {a, b, c}
    馬連   hit ⟺ {key} == {a, b}
    ワイド hit ⟺ {key} ⊆ {a, b, c}
    馬単   hit ⟺ key == (a, b)
    3連複  hit ⟺ {key} == {a, b, c}
    3連単  hit ⟺ key == (a, b, c)

最大化問題 (f_b = 資金に対する bet b の配分率):
    maximize  g(f) = Σ_ω p_ω · log( 1 − Σ_b f_b + Σ_{b: b hits ω} f_b · O_b )
    s.t.      f_b ≥ 0,  Σ_b f_b ≤ 1

g は concave (log ∘ affine の非負結合) なので projected gradient ascent +
backtracking line search で大域最適に収束する。
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .ev import PXO_FLOOR, _tier, trifecta_prob
from .models import Probabilities

# 1 − Σf の予備をわずかに残し、全外し outcome で log(0) = −∞ になるのを防ぐ。
_MAX_TOTAL_FRACTION = 0.9999

# トリガミ防止の安全マージン。束を組んだ時点のオッズは実払戻 (締切直前 / レンジ型
# bet の確定値) から下振れし得る。各脚の payout が「投資総額 × このマージン」以上で
# あることを要求すると、オッズが ~(1−1/margin) ぶん下振れしても収支マイナスにならない。
# 1.10 → オッズ 9% 下振れまで吸収。複勝/ワイドのレンジ下限採用 (parse 側) と二段構え。
TORIGAMI_MARGIN = 1.10


def enumerate_outcomes(
    probs: Probabilities, *, eps: float = 1e-9
) -> tuple[list[tuple[int, int, int]], np.ndarray]:
    """全 ordered triple (a,b,c) と確率 P(a,b,c) を返す (Σp ≈ 1)。

    trifecta_prob を素朴に呼ぶと denom 計算が O(N) で全体 O(N^4) になるため、
    分母を precompute して O(N^3) で展開する。
    """
    horses = [h for h, p in probs.win.items() if p > 0]
    win = probs.win
    pl2 = probs.place2
    pl3 = probs.place3
    total_p2 = sum(pl2.get(k, 0.0) for k in horses)
    total_p3 = sum(pl3.get(k, 0.0) for k in horses)

    outcomes: list[tuple[int, int, int]] = []
    ps: list[float] = []
    for a in horses:
        p1 = win[a]
        denom_b = total_p2 - pl2.get(a, 0.0)
        if denom_b <= 0:
            continue
        for b in horses:
            if b == a:
                continue
            p2 = pl2.get(b, 0.0) / denom_b
            if p2 <= 0:
                continue
            denom_c = total_p3 - pl3.get(a, 0.0) - pl3.get(b, 0.0)
            if denom_c <= 0:
                continue
            p12 = p1 * p2
            for c in horses:
                if c == a or c == b:
                    continue
                p3 = pl3.get(c, 0.0) / denom_c
                p = p12 * p3
                if p > eps:
                    outcomes.append((a, b, c))
                    ps.append(p)
    arr = np.asarray(ps, dtype=np.float64)
    s = arr.sum()
    if s > 0:
        arr /= s  # 数値ドリフトを 1 に正規化
    return outcomes, arr


def _bet_hits(bet_type: str, key: Sequence[int], a: int, b: int, c: int) -> bool:
    if bet_type == "win":
        return len(key) == 1 and key[0] == a
    if bet_type == "place":
        return len(key) == 1 and key[0] in (a, b, c)
    if bet_type == "exacta":
        return len(key) == 2 and key[0] == a and key[1] == b
    if bet_type == "quinella":
        return len(key) == 2 and {key[0], key[1]} == {a, b}
    if bet_type == "wide":
        return len(key) == 2 and {key[0], key[1]} <= {a, b, c}
    if bet_type == "trio":
        return len(key) == 3 and {key[0], key[1], key[2]} == {a, b, c}
    if bet_type == "trifecta":
        return len(key) == 3 and (key[0], key[1], key[2]) == (a, b, c)
    return False


def _project(f: np.ndarray) -> np.ndarray:
    """{f ≥ 0, Σf ≤ _MAX_TOTAL_FRACTION} への Euclidean 射影。"""
    f = np.maximum(f, 0.0)
    if f.sum() <= _MAX_TOTAL_FRACTION:
        return f
    # Σf = cap の simplex への射影 (Duchi et al. 2008)
    cap = _MAX_TOTAL_FRACTION
    u = np.sort(f)[::-1]
    css = np.cumsum(u) - cap
    rho = np.nonzero(u - css / (np.arange(len(u)) + 1) > 0)[0][-1]
    theta = css[rho] / (rho + 1.0)
    return np.maximum(f - theta, 0.0)


def _optimize(
    p: np.ndarray, H: np.ndarray, *, iters: int = 500, tol: float = 1e-10
) -> np.ndarray:
    """maximize g(f)=Σ p_ω log(1−Σf+ (f·H)_ω) over {f≥0, Σf≤cap}.

    H[b, ω] = O_b (b が ω で当たる) または 0。projected gradient + backtracking。
    """
    n_bets = H.shape[0]
    f = np.zeros(n_bets, dtype=np.float64)

    def g(fv: np.ndarray) -> float:
        W = 1.0 - fv.sum() + fv @ H
        if np.any(W <= 0):
            return -np.inf
        return float(p @ np.log(W))

    g_cur = g(f)  # f=0 → g=0
    lr = 1.0
    for _ in range(iters):
        W = 1.0 - f.sum() + f @ H
        q = p / W
        grad = H @ q - q.sum()  # ∂g/∂f_b = −Σp/W + O_b Σ_{hit}p/W
        # backtracking line search
        step = lr
        improved = False
        for _bt in range(40):
            f_new = _project(f + step * grad)
            g_new = g(f_new)
            if g_new > g_cur + 1e-12:
                improved = True
                break
            step *= 0.5
        if not improved:
            break
        if g_new - g_cur < tol:
            f = f_new
            break
        # 成功したら次回はやや大きめの step から
        lr = min(step * 1.5, 1e6)
        f, g_cur = f_new, g_new
    return f


def build_bundle(
    candidates: Sequence[dict],
    probs: Probabilities,
    *,
    bankroll: int = 10_000,
    kelly_fraction: float = 1.0,
    pxo_floor: float = PXO_FLOOR,
    max_legs: int = 12,
    hit_max_legs: int = 20,
    min_stake: int = 100,
    stake_unit: int = 100,
    avoid_torigami: bool = True,
    torigami_margin: float = TORIGAMI_MARGIN,
    prioritize: str = "yield",
) -> dict:
    """候補 bet 群から joint Kelly 最適「まとめ買い」束を構築。

    candidates: [{"bet_type","key"(list/tuple),"odds","prob","px_o","tier"}, ...]
    返り値は snapshot 直列化済 dict (frontend がそのまま描画)。

    prioritize="yield" (既定): 回収優先 = pool を個別 Kelly fraction (`_kelly_ind`) 降順で
        絞る。長期 E[log W] 最適 = ROI 最大化志向。
    prioritize="hit" : 的中優先 = **EV 関係なく全券種から想定P降順で上位 hit_max_legs(=20) 点**を選び
        (px_o floor 撤廃 = -EV でも P が高ければ採用)、**想定P比例**で予算配分する
        (**Kelly 不使用** — Kelly は -EV 脚に賭け金 0 を割り当てるので「当たりやすい物を
        買う」と両立しない)。ただし下のトリガミ防止ループは残すので、どの脚が当たっても
        払戻 ≥ 投資総額×margin = **収支プラス**を保証する (= 当たりやすさ優先だが損はしない
        範囲で、profit を出せない低オッズ脚は drop される)。実戦では「回収優先 を主軸 +
        的中優先 をおまけ計測」が定石。

    avoid_torigami=True のとき「トリガミ防止」フィルタを適用する。束を丸ごと買った
    ときの投資総額 S に対し、payout(= odds×stake) < S × torigami_margin の脚を除去
    → 残った脚で再最適化、を収束まで繰り返す。payout は加算なので「全脚が単独で
    S × margin を回収できる」なら **どの的中 outcome でも payout ≥ S × margin** が
    保証される。margin>1 はオッズ下振れ (締切直前のドリフト / 複勝・ワイドのレンジ幅) に
    対する緩衝で、保存オッズから ~(1−1/margin) 下振れしても収支マイナスにならない。
    """
    # prioritize で候補プールの作り方が根本的に違う:
    #   hit  : **EV 関係なく** 想定P降順で上位 hit_max_legs 点 (px_o floor 撤廃)。
    #          配分は後段で「想定P比例 + トリガミ防止」(Kelly 不使用)。
    #   yield: +EV (px_o≥floor) のみ → 個別 Kelly 効率降順 → joint Kelly 配分。
    if prioritize == "hit":
        pool = [
            c for c in candidates
            if c.get("odds", 0) > 1.0 and c.get("prob", 0.0) > 0.0
        ]
        pool.sort(key=lambda c: c.get("prob", 0.0), reverse=True)
        pool = pool[:hit_max_legs]
    else:
        pool = [
            c for c in candidates
            if c.get("odds", 0) > 1.0 and c.get("px_o", 0.0) >= pxo_floor
        ]
        for c in pool:
            o = c["odds"]
            c["_kelly_ind"] = (c["px_o"] - 1.0) / (o - 1.0)
        pool.sort(key=lambda c: c["_kelly_ind"], reverse=True)
        pool = pool[:max_legs]

    base = {
        "objective": "joint_kelly",
        "bankroll": bankroll,
        "kelly_fraction": kelly_fraction,
        "pxo_floor": pxo_floor,
        "legs": [],
        "total_stake": 0,
        "total_fraction": 0.0,
        "bundle_hit_prob": 0.0,
        "expected_return": 0.0,
        "expected_log_growth": 0.0,
    }
    if not pool:
        base["n_candidates"] = 0
        base["n_outcomes"] = 0
        return base

    outcomes, p = enumerate_outcomes(probs)
    base["n_candidates"] = len(pool)
    base["n_outcomes"] = len(outcomes)
    if len(outcomes) == 0:
        return base

    # pool 全体の hit 行列 H_full[b, ω] = odds_b (当たり) / 0 を 1 度だけ構築。
    # トリガミ除去ループは active 部分集合の行を抜き出して再最適化する。
    H_full = np.zeros((len(pool), len(outcomes)), dtype=np.float64)
    for bi, c in enumerate(pool):
        bt, key, odds = c["bet_type"], tuple(c["key"]), c["odds"]
        for wi, (a, b, cc) in enumerate(outcomes):
            if _bet_hits(bt, key, a, b, cc):
                H_full[bi, wi] = odds

    active = list(range(len(pool)))           # pool への index
    f_active = np.zeros(0)
    stakes_active = np.zeros(0)
    n_dropped_torigami = 0
    while active:
        H = H_full[active]
        if prioritize == "hit":
            # 的中優先: Kelly 不使用。想定P比例で配分 (-EV 脚も P が高ければ残す)。
            # トリガミ防止ループ (下記) は yield と共通で回るので、profit を出せない
            # 低オッズ脚は drop され、残った脚で「収支プラス」が保証される。
            w = np.array(
                [max(pool[a].get("prob", 0.0), 0.0) for a in active],
                dtype=np.float64,
            )
            f_opt = w / w.sum() if w.sum() > 0 else w
        else:
            f_opt = _optimize(p, H)
        # kelly_fraction>1 でスケール後に Σf>cap になり得るので必ず再射影する
        # (再射影しないと floor 丸めでも総額が bankroll を超える)。≤1 では no-op。
        f = _project(f_opt * float(kelly_fraction))
        # floor で stake_unit に丸める。round だと per-leg 切り上げの累積で総額が
        # bankroll を超える (¥10,100 等) ことがあるため、floor で Σstake ≤ Σf×bankroll
        # ≤ bankroll を保証する。
        stakes = np.floor(f * bankroll / stake_unit) * stake_unit
        stakes = np.where(stakes >= min_stake, stakes, 0.0)
        kept_local = [i for i in range(len(active)) if stakes[i] > 0]
        S = float(stakes.sum())
        if not avoid_torigami or not kept_local:
            f_active, stakes_active = f_opt, stakes
            break
        # トリガミ脚: payout (odds×stake) < 投資総額 S × margin → 下振れで収支マイナス化
        thresh = S * torigami_margin
        offenders = [
            i for i in kept_local
            if pool[active[i]]["odds"] * stakes[i] < thresh - 1e-9
        ]
        if not offenders:
            f_active, stakes_active = f_opt, stakes
            break
        # 最も payout カバレッジの低い脚を 1 本落として再最適化 (S が減り残りは楽になる)
        worst = min(offenders, key=lambda i: pool[active[i]]["odds"] * stakes[i])
        n_dropped_torigami += 1
        active = [a for k, a in enumerate(active) if k != worst]

    H = H_full[active] if active else np.zeros((0, len(outcomes)))
    frac_round = stakes_active / bankroll if len(stakes_active) else np.zeros(0)

    legs = []
    for li, pool_idx in enumerate(active):
        if stakes_active[li] <= 0:
            continue
        c = pool[pool_idx]
        legs.append({
            "bet_type": c["bet_type"],
            "key": list(c["key"]),
            "odds": round(float(c["odds"]), 1),
            "prob": float(c["prob"]),
            "px_o": float(c["px_o"]),
            "tier": c["tier"],
            "kelly": float(f_active[li]),       # full-Kelly fraction
            "fraction": float(frac_round[li]),  # 丸め後の実配分
            "stake": int(stakes_active[li]),
            "payout_if_hit": int(round(float(c["odds"]) * stakes_active[li])),
        })
    legs.sort(key=lambda l: l["stake"], reverse=True)

    total_stake = float(stakes_active.sum()) if len(stakes_active) else 0.0
    # 丸め後の実 stake で束全体の指標を計算
    if active and total_stake > 0:
        W = 1.0 - frac_round.sum() + frac_round @ H
        W = np.clip(W, 1e-12, None)
        hit_mask = (H > 0).T @ (stakes_active > 0) > 0  # 各 outcome で 1 leg 以上当たるか
        base["bundle_hit_prob"] = float(p[hit_mask].sum())
        base["expected_return"] = float(p @ W)
        base["expected_log_growth"] = float(p @ np.log(W))
        # 最小 payout カバレッジ比 (≥1.0 ならトリガミ無し)
        base["min_payout_ratio"] = min(l["payout_if_hit"] / total_stake for l in legs)
    base["legs"] = legs
    base["total_stake"] = int(total_stake)
    base["total_fraction"] = float(frac_round.sum()) if len(frac_round) else 0.0
    base["dropped_torigami"] = n_dropped_torigami
    base["torigami_margin"] = float(torigami_margin)
    return base


def build_trifecta_hitmax(
    probs: Probabilities,
    trifecta: Sequence,                  # rd.trifecta (BetOdds 列; odds 源)
    *,
    rank_index: dict[int, float] | None = None,   # Claude 指数 (無ければ model win prob で代替)
    bankroll: int = 10_000,
    head_max: int = 2,                   # 1着列 最大頭数 (絞る)
    head_gap: float = 0.12,              # 指数 top2 の相対差がこれ以下なら 1着を 2 頭に (開き判定)
    mid_count: int = 4,                  # 2着列 頭数 (中くらい)
    tail_count: int = 7,                 # 3着列 頭数 (広げる)
    avoid_torigami: bool = True,         # トリガミ防止 (ユーザ指示で既定 ON)
    torigami_margin: float = TORIGAMI_MARGIN,
    min_stake: int = 100,
    stake_unit: int = 100,
) -> dict:
    """Plan T「全力的中モード」: Claude 指数ドリブンの 3連単フォーメーション。

    要件 (ユーザ指示 2026-06-02):
    - **Claude 指数の上位を本命**にしてフォーメーションを組む (rank_index が Claude 指数。無ければ
      model win prob で代替)。**市場 (オッズ) はランキングに一切使わない**。
    - **指数の開きを見て 1着を 1〜2 頭に**変える (top2 の相対差が head_gap 以下なら 2 頭=絞りつつ
      接戦は厚く)。点数はフォーメーションの幅で自動的に変わる。
    - **1着は絞る (1〜2) / 2着は中くらい (mid_count) / 3着は広げる (tail_count)**。head⊆mid⊆tail
      (同一ランキングの top-n) で ordered triple を全展開。
    - **トリガミ防止する** (avoid_torigami=True): build_bundle の的中優先 (prioritize="hit") 配分 +
      トリガミ除去ループを再利用し、どの脚が当たっても払戻 ≥ 投資総額×margin を保証 (= 当たれば
      投資総額以上を回収)。-EV は許容 (当たらなければマイナス) だが当たり時のトリガミは防ぐ。
    - 3連単のみ。確率は **market_blend=0 の model-only probs** (Claude指数 ⊗ GBM ⊗ speed_v2) を
      渡すこと (呼び出し側が保証)。返り値は build_bundle と同 schema (objective="trifecta_hitmax")。

    odds が取れない triple は買えないので除外。covered_prob は最終 (トリガミ除去後) の脚の
    model 的中確率の和 (= 互いに排他なので束の理論的中率)。
    """
    base = {
        "objective": "trifecta_hitmax",
        "bankroll": bankroll,
        "legs": [],
        "total_stake": 0,
        "total_fraction": 0.0,
        "bundle_hit_prob": 0.0,
        "covered_prob": 0.0,
        "expected_return": 0.0,
        "n_points": 0,
        "n_candidates": 0,
        "n_formation": 0,
        "rank_source": None,
        "head_horses": [],
        "mid_horses": [],
        "tail_horses": [],
        "head_n": 0,
        "formation": None,
        "odds_summary": None,
    }

    # odds 源: absent / odds<=0 を除外して key→odds lookup (build_table と同じガード)。
    odds_by_key: dict[tuple, float] = {}
    for t in (trifecta or []):
        if getattr(t, "absent", False) or getattr(t, "odds", 0) <= 0:
            continue
        odds_by_key[tuple(t.key)] = float(t.odds)

    win = probs.win or {}
    horses = [h for h, pw in win.items() if pw > 0]
    if not horses or not odds_by_key:
        return base

    # ランキング: Claude 指数優先 (rank_index)、無ければ model win prob (市場は使わない)。
    if rank_index:
        rank = sorted(horses, key=lambda h: (rank_index.get(h, float("-inf")), win.get(h, 0.0)),
                      reverse=True)
        idx = {h: float(rank_index.get(h, 0.0)) for h in rank}
        base["rank_source"] = "claude"
    else:
        rank = sorted(horses, key=lambda h: win.get(h, 0.0), reverse=True)
        idx = {h: float(win.get(h, 0.0)) for h in rank}
        base["rank_source"] = "model"
    n = len(rank)

    # 1着列の頭数: 既定 1。指数 top2 が接戦 (idx[1] ≥ idx[0]·(1−head_gap)) なら 2 頭に (絞りつつ厚く)。
    head_n = 1
    if n >= 2 and head_max >= 2:
        top, second = idx[rank[0]], idx[rank[1]]
        if top > 0 and second >= top * (1.0 - head_gap):
            head_n = 2
    head_n = max(1, min(head_n, head_max, n))
    mid_n = min(max(mid_count, head_n), n)         # 2着 中くらい (≥ head)
    tail_n = min(max(tail_count, mid_n), n)        # 3着 広い (≥ mid)
    head, mid, tail = rank[:head_n], rank[:mid_n], rank[:tail_n]
    base.update(head_horses=list(head), mid_horses=list(mid), tail_horses=list(tail),
                head_n=head_n, formation=f"{head_n}×{mid_n}×{tail_n}")

    # フォーメーション展開: a∈head, b∈mid, c∈tail (相異なる)。買える (odds 有る) triple のみ候補化。
    cands: list[dict] = []
    seen: set[tuple] = set()
    for a in head:
        for b in mid:
            if b == a:
                continue
            for c in tail:
                if c == a or c == b:
                    continue
                key = (a, b, c)
                if key in seen:
                    continue
                seen.add(key)
                odds = odds_by_key.get(key)
                if odds is None:
                    continue
                pr = trifecta_prob(key, probs)
                cands.append({"bet_type": "trifecta", "key": list(key), "odds": odds,
                              "prob": pr, "px_o": pr * odds, "tier": _tier(pr * odds)})
    base["n_formation"] = len(seen)
    base["n_candidates"] = len(cands)
    if not cands:
        return base

    # 配分 + トリガミ防止は build_bundle の的中優先経路を再利用 (テスト済の除去ループ)。
    # prioritize="hit": EV floor なし・想定P比例配分。avoid_torigami: 払戻 < 投資総額×margin の
    # 脚を収束まで除去 → どの的中でも払戻 ≥ 投資総額×margin。上限を外して formation 全体を渡す。
    bundle = build_bundle(
        cands, probs, bankroll=bankroll, prioritize="hit",
        avoid_torigami=avoid_torigami, torigami_margin=torigami_margin,
        hit_max_legs=len(cands), max_legs=len(cands),
        min_stake=min_stake, stake_unit=stake_unit,
    )
    # build_bundle の汎用フィールドを base にマージしつつ Plan T 固有を上書き。
    base.update({k: bundle[k] for k in bundle if k in base or k in (
        "min_payout_ratio", "dropped_torigami", "torigami_margin",
        "expected_log_growth", "total_fraction", "n_outcomes")})
    base["objective"] = "trifecta_hitmax"
    base["legs"] = bundle.get("legs", [])
    base["total_stake"] = bundle.get("total_stake", 0)
    base["expected_return"] = bundle.get("expected_return", 0.0)
    # covered_prob = 最終 (トリガミ除去後) 脚の model 的中確率の和 (排他 ⇒ 束の理論的中率)。
    covered = float(sum(trifecta_prob(tuple(l["key"]), probs) for l in base["legs"]))
    base["covered_prob"] = covered
    base["bundle_hit_prob"] = covered
    base["n_points"] = len(base["legs"])

    if base["legs"]:
        payouts = sorted(l["payout_if_hit"] for l in base["legs"])
        wsum = sum(l["prob"] for l in base["legs"]) or 1.0
        base["odds_summary"] = {
            "min_payout": payouts[0],
            "median_payout": payouts[len(payouts) // 2],
            "max_payout": payouts[-1],
            "weighted_avg_odds": float(sum(l["prob"] * l["odds"] for l in base["legs"]) / wsum),
        }
    return base


def build_trifecta_from_keys(
    probs: Probabilities,
    trifecta: Sequence,
    keys: Sequence[Sequence[int]],
    *,
    bankroll: int = 10_000,
    avoid_torigami: bool = True,
    torigami_margin: float = TORIGAMI_MARGIN,
    min_stake: int = 100,
    stake_unit: int = 100,
    max_points: int = 60,
) -> dict:
    """Claude が選んだ 3連単 買い目 (keys) からトリガミ防止つき束を組む (Plan T の Claude 選定版)。

    build_trifecta_hitmax の **機械フォーメーション展開を Claude 選定 keys に差し替えた**版。配分・
    トリガミ除去は同じ build_bundle(prioritize="hit") 経路を再利用するので、束の schema・トリガミ
    保証は build_trifecta_hitmax と同一。買えない (オッズ無し) triple・重複・非相異馬は除外し、
    最大 max_points 点で打ち切る。返り値 objective="trifecta_claude_select" / rank_source="claude" /
    selection_source="claude" (それ以外のフィールドは build_trifecta_hitmax と同形)。
    """
    base = {
        "objective": "trifecta_claude_select",
        "bankroll": bankroll, "legs": [], "total_stake": 0, "total_fraction": 0.0,
        "bundle_hit_prob": 0.0, "covered_prob": 0.0, "expected_return": 0.0,
        "n_points": 0, "n_candidates": 0, "n_formation": 0,
        "rank_source": "claude", "selection_source": "claude",
        "head_horses": [], "mid_horses": [], "tail_horses": [],
        "head_n": 0, "formation": None, "odds_summary": None,
    }
    odds_by_key: dict[tuple, float] = {}
    for t in (trifecta or []):
        if getattr(t, "absent", False) or getattr(t, "odds", 0) <= 0:
            continue
        odds_by_key[tuple(t.key)] = float(t.odds)
    win = probs.win or {}
    if not odds_by_key or not keys:
        return base

    cands: list[dict] = []
    seen: set[tuple] = set()
    heads: list[int] = []
    mids: list[int] = []
    tails: list[int] = []
    for k in keys:
        if not k or len(k) != 3:
            continue
        try:
            a, b, c = int(k[0]), int(k[1]), int(k[2])
        except (TypeError, ValueError):
            continue
        if len({a, b, c}) != 3:                # 相異3頭でない
            continue
        if a not in win or b not in win or c not in win:   # 出走馬でない
            continue
        key = (a, b, c)
        if key in seen:
            continue
        odds = odds_by_key.get(key)
        if odds is None:                       # 買えない (オッズ無し) → 除外
            continue
        seen.add(key)
        if a not in heads:
            heads.append(a)
        if b not in mids:
            mids.append(b)
        if c not in tails:
            tails.append(c)
        pr = trifecta_prob(key, probs)
        cands.append({"bet_type": "trifecta", "key": [a, b, c], "odds": odds,
                      "prob": pr, "px_o": pr * odds, "tier": _tier(pr * odds)})
        if len(cands) >= max_points:
            break
    base.update(n_formation=len(seen), n_candidates=len(cands),
                head_horses=list(heads), mid_horses=list(mids), tail_horses=list(tails),
                head_n=len(heads),
                formation=f"{len(heads)}×{len(mids)}×{len(tails)} (Claude選定)")
    if not cands:
        return base

    bundle = build_bundle(
        cands, probs, bankroll=bankroll, prioritize="hit",
        avoid_torigami=avoid_torigami, torigami_margin=torigami_margin,
        hit_max_legs=len(cands), max_legs=len(cands),
        min_stake=min_stake, stake_unit=stake_unit,
    )
    base.update({k: bundle[k] for k in bundle if k in base or k in (
        "min_payout_ratio", "dropped_torigami", "torigami_margin",
        "expected_log_growth", "total_fraction", "n_outcomes")})
    base["objective"] = "trifecta_claude_select"
    base["rank_source"] = "claude"
    base["selection_source"] = "claude"
    base["legs"] = bundle.get("legs", [])
    base["total_stake"] = bundle.get("total_stake", 0)
    base["expected_return"] = bundle.get("expected_return", 0.0)
    covered = float(sum(trifecta_prob(tuple(l["key"]), probs) for l in base["legs"]))
    base["covered_prob"] = covered
    base["bundle_hit_prob"] = covered
    base["n_points"] = len(base["legs"])
    if base["legs"]:
        payouts = sorted(l["payout_if_hit"] for l in base["legs"])
        wsum = sum(l["prob"] for l in base["legs"]) or 1.0
        base["odds_summary"] = {
            "min_payout": payouts[0],
            "median_payout": payouts[len(payouts) // 2],
            "max_payout": payouts[-1],
            "weighted_avg_odds": float(sum(l["prob"] * l["odds"] for l in base["legs"]) / wsum),
        }
    return base


def candidates_from_ev_rows(rows, bet_tables) -> list[dict]:
    """analyze.py の EvRow (3連単) + BetEvRow テーブル (他 bet type) を候補に変換。"""
    cands: list[dict] = []
    for r in rows:
        cands.append({
            "bet_type": "trifecta",
            "key": list(r.key),
            "odds": r.odds,
            "prob": r.prob,
            "px_o": r.px_o,
            "tier": r.tier,
        })
    for bt, brows in (bet_tables or {}).items():
        for r in brows:
            cands.append({
                "bet_type": bt,
                "key": list(r.key),
                "odds": r.odds,
                "prob": r.prob,
                "px_o": r.px_o,
                "tier": r.tier,
            })
    return cands


def candidates_from_snapshot_rows(
    rows: Sequence[dict], bet_tables: dict[str, Sequence[dict]] | None
) -> list[dict]:
    """snapshot の rows (3連単) + bet_tables (他 bet type) を候補リストに変換。

    analyze.py / backfill 双方から使う共通アダプタ。
    """
    cands: list[dict] = []
    for r in rows:
        cands.append({
            "bet_type": "trifecta",
            "key": list(r["key"]),
            "odds": r["odds"],
            "prob": r["prob"],
            "px_o": r["px_o"],
            "tier": r["tier"],
        })
    for bt, brows in (bet_tables or {}).items():
        for r in brows:
            cands.append({
                "bet_type": bt,
                "key": list(r["key"]),
                "odds": r["odds"],
                "prob": r["prob"],
                "px_o": r["px_o"],
                "tier": r["tier"],
            })
    return cands
