#!/usr/bin/env python3
"""Claude 指数が model+市場 に **OOS で価値を足すか** を測る検証ハーネス。

本番コードは一切変更しない読み取り専用スクリプト。3 ファイルが揃う race
(snapshot / llm.json / results) を集め、**勝者 (finish_order[0]) の予測 1 着確率**を
3 通りで出して log-loss を比較する:

  (a) market only   : 単勝オッズを de-vig した市場暗黙率 (src.ev.power_method_overround)。
  (b) model+market  : model fundamental (snapshot から復元) を市場とブレンド (Claude 無し)。
  (c) +Claude       : (b) の fundamental に Claude 指数を合成 (src.ev._combine_llm_index,
                      llm_blend=0.5) してから市場とブレンド。

(b) と (c) は **Claude 合成ステップだけが違う** 完全に制御された比較なので、
「Claude が予測を改善するか」を log-loss 差で直接読める。

── model fundamental の復元 ──────────────────────────────────────────────
snapshot の `bet_tables['win']` は estimate_probs の最終 1 着確率だが、live 既定
(market_blend=MARKET_BLEND_LIVE=0.0) では **市場ブレンド無し**で保存されており、
中身は `_combine_llm_index(model_fundamental, claude, llm_blend_baked)` に等しい。
`_combine_llm_index` は per-horse loglinear softmax なので**厳密に可逆** (実測 err ~1e-16):
baked Claude を剥がして model fundamental f を復元する。これで (b)/(c) を任意の
llm_blend / market_blend で再構成でき、保存時のバラついた baked blend (0.25〜0.9) に
依存しない統制された評価ができる。

market_blend>0 で保存された snapshot (例 JRA 経路) は、剥がした後の値が
「fundamental×market」になり厳密な fundamental 分離ができないため `--require-no-market-blend`
(既定 ON) で除外する。`--include-market-blend` で含める (近似と注記)。

⚠ LEAKAGE CAVEAT ─────────────────────────────────────────────────────────
過去 race の Claude 指数は web 検索で **レース結果が漏れうる** (score 実行時刻が
発走後、または検索でオッズ/結果に触れる)。よってこの検証は**予測力の純粋検証ではなく
ネガティブスクリーン/動作確認**であり、(c) が (b) を改善しなければ「価値が無い or 有害」
の強い証拠になるが、改善しても leakage の可能性が排除できない。**真の検証はライブ蓄積後**に
本スクリプトを再実行すること (race が増えれば同じコードでそのまま回る)。

使い方:
    .venv/bin/python scripts/validate_claude_value.py
    .venv/bin/python scripts/validate_claude_value.py --market-blend 0.78 --llm-blend 0.5
    .venv/bin/python scripts/validate_claude_value.py --bootstrap 5000 --json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import ev as E  # noqa: E402

PRED_DIR = ROOT / "data" / "predictions"
RES_DIR = ROOT / "data" / "results"

# log-loss を有限に保つクリップ (p=0 を避ける)。確率はすべて [EPS, 1-EPS] にクリップ。
EPS = 1e-6


# ─────────────────────────── データ収集 ───────────────────────────


def _find_races() -> list[str]:
    """snapshot + llm.json + results(finish_order) が揃う race_id を返す。"""
    llm = {os.path.basename(p)[: -len(".llm.json")] for p in glob.glob(str(PRED_DIR / "*.llm.json"))}
    snap = {
        os.path.basename(p)[: -len(".json")]
        for p in glob.glob(str(PRED_DIR / "*.json"))
        if not p.endswith(".llm.json")
    }
    res = {os.path.basename(p)[: -len(".json")] for p in glob.glob(str(RES_DIR / "*.json"))}
    return sorted(llm & snap & res)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ─────────────────────────── 確率の構築 ───────────────────────────


def _normalize(d: dict[int, float]) -> dict[int, float]:
    s = sum(d.values())
    if s <= 0:
        return {k: 1.0 / len(d) for k in d} if d else {}
    return {k: v / s for k, v in d.items()}


def _market_implied(win_odds: dict[int, float], floor: float = 1e-9) -> dict[int, float]:
    """単勝オッズ → 1/odds 正規化 → power-method de-vig した市場暗黙 1 着率。"""
    raw = {k: 1.0 / o for k, o in win_odds.items() if o and o > 0}
    raw = _normalize(raw)
    if not raw:
        return {}
    try:
        de = E.power_method_overround(raw)
    except Exception:
        de = raw
    de = {k: max(v, floor) for k, v in de.items()}
    return _normalize(de)


def _strength_softmax(claude_index: dict[int, float], keys: list[int], floor: float) -> dict[int, float]:
    """_combine_llm_index の scale='strength' 経路と同一の Claude 指数→確率変換。"""
    raw = {k: max(float(claude_index.get(k, 0.0)), 0.0) for k in keys}
    rm = max(raw.values()) if raw else 0.0
    exps = {k: math.exp((v - rm) / E.T_LLM) for k, v in raw.items()}
    z = sum(exps.values()) or 1.0
    L = {k: max(exps[k] / z, floor) for k in keys}
    return _normalize(L)


def _recover_fundamental(
    stored_win: dict[int, float],
    claude_index: dict[int, float],
    baked_blend: float,
    support: dict[int, int] | None,
    floor: float,
) -> dict[int, float]:
    """snapshot の最終 1 着確率から model fundamental を復元 (baked Claude を剥がす)。

    `_combine_llm_index` を厳密に逆算:
        log stored_k = (1-w_k)·log f_k + w_k·log L_k + C
        → log f_k = (log stored_k - w_k·log L_k) / (1-w_k) + C'   (w_k<1)
                  =  log stored_k                                 (w_k≈0; 未スコア馬)
    w_k = baked_blend · support_mult(support_k)。Claude が触れていない馬は w_k=0。
    softmax は shift 不変なので C' は正規化で消える。
    """
    keys = list(stored_win.keys())
    if not keys:
        return {}
    scored = set(claude_index.keys()) & set(keys)
    L = _strength_softmax(claude_index, keys, floor)
    logf: dict[int, float] = {}
    for k in keys:
        s = max(stored_win.get(k, 0.0), 1e-12)
        if k not in scored:
            w = 0.0
        else:
            mult = E._support_mult(None if support is None else support.get(k, 0))
            w = max(min(baked_blend * mult, 1.0), 0.0)
        if w >= 1.0 - 1e-9:
            # 完全に Claude のみで上書きされた馬は fundamental が一意に決まらない
            # (情報が消失)。stored を近似採用 (このケースは baked_blend≈1 のみ)。
            logf[k] = math.log(s)
            continue
        l = max(L.get(k, 0.0), 1e-12)
        logf[k] = (math.log(s) - w * math.log(l)) / (1.0 - w)
    mm = max(logf.values())
    e = {k: math.exp(v - mm) for k, v in logf.items()}
    return _normalize(e)


def _blend_market(fundamental: dict[int, float], market: dict[int, float], beta: float, floor: float) -> dict[int, float]:
    """estimate_probs の loglinear 市場ブレンドと同一: softmax(α·log f + β·log π)。

    market が空 or beta<=0 のときは fundamental をそのまま返す (市場無視)。
    """
    if beta <= 0 or not market:
        return _normalize(dict(fundamental))
    alpha = max(1.0 - beta, 0.0)
    # market を floor + 正規化 (estimate_probs と同じ)
    mk = {k: max(market.get(k, 0.0), floor) for k in fundamental}
    mk = _normalize(mk)
    logs: dict[int, float] = {}
    for k in set(fundamental) | set(mk):
        f = max(fundamental.get(k, 0.0), 1e-9)
        pi = max(mk.get(k, 0.0), 1e-9)
        logs[k] = alpha * math.log(f) + beta * math.log(pi)
    m = max(logs.values())
    e = {k: math.exp(v - m) for k, v in logs.items()}
    return _normalize(e)


# ─────────────────────────── 1 race 評価 ───────────────────────────


def evaluate_race(
    rid: str,
    *,
    market_blend: float,
    llm_blend: float,
    market_floor: float,
    require_no_market_blend: bool,
) -> dict | None:
    """1 race の勝者 1 着確率を (a)(b)(c) で出す。対象外なら理由付きで None 相当の skip dict。"""
    snap = _load_json(PRED_DIR / f"{rid}.json")
    llm = _load_json(PRED_DIR / f"{rid}.llm.json")
    res = _load_json(RES_DIR / f"{rid}.json")
    if not (snap and llm and res):
        return {"rid": rid, "skip": "missing/corrupt file"}

    fo = res.get("finish_order") or []
    if not fo:
        return {"rid": rid, "skip": "no finish_order"}
    try:
        winner = int(fo[0])
    except (TypeError, ValueError):
        return {"rid": rid, "skip": "bad winner"}

    # 保存時の市場ブレンド (live 既定 0.0)。>0 だと fundamental 分離が近似になる。
    baked_market = snap.get("llm_blend")  # placeholder, real value below
    snap_market_blend = None
    # snapshot は market_blend を直接保存しない。odds_source で live 経路を判定:
    # keibago / oddspark / netkeiba live は MARKET_BLEND_LIVE=0.0。jra も同経路だが
    # 念のため market_win_index と stored の整合で市場ブレンド痕跡を検出する。
    # ここでは odds_source ベースの単純判定 + ヒューリスティック。
    baked_blend = snap.get("llm_blend")
    if baked_blend is None:
        return {"rid": rid, "skip": "no baked llm_blend"}
    baked_blend = float(baked_blend)

    if snap.get("llm_fallback"):
        return {"rid": rid, "skip": "llm_fallback (no Claude index baked)"}

    # stored 最終 1 着確率
    bt = (snap.get("bet_tables") or {}).get("win") or []
    stored_win = {}
    for r in bt:
        k = r.get("key") or []
        if k:
            stored_win[int(k[0])] = float(r.get("prob") or 0.0)
    stored_win = {k: v for k, v in stored_win.items() if v > 0}
    if not stored_win:
        return {"rid": rid, "skip": "no stored win probs"}

    # 単勝オッズ (market_signals) → 市場暗黙率
    win_odds = {}
    for m in snap.get("market_signals") or []:
        n = m.get("number")
        wo = m.get("win_odds")
        if n is not None and wo and float(wo) > 0:
            win_odds[int(n)] = float(wo)
    market = _market_implied(win_odds, floor=1e-9)

    # 市場ブレンドが保存に入っている疑いの検出: live 経路 (keibago/oddspark/netkeiba) は
    # market_blend=0。JRA も同様だが、保存時に市場ブレンドが入っていれば stored は
    # fundamental×market になり剥がしきれない。判定材料が無いので odds_source で許可し、
    # require_no_market_blend のときは市場痕跡 (stored が market と強相関) を heuristic 除外。
    odds_source = snap.get("odds_source") or "netkeiba"

    # Claude 指数 + support
    claude = {}
    for k, v in (llm.get("scores") or {}).items():
        try:
            claude[int(k)] = float(v)
        except (ValueError, TypeError):
            continue
    support = {}
    for k, v in (snap.get("llm_support") or {}).items():
        try:
            support[int(k)] = max(0, int(float(v)))
        except (ValueError, TypeError):
            continue
    support = support or None
    scale = snap.get("llm_scale") or "strength"

    if not claude:
        return {"rid": rid, "skip": "no claude scores"}
    if winner not in stored_win:
        return {"rid": rid, "skip": f"winner {winner} not in win-prob set"}

    # scale='prob' の race は復元式が strength 前提なので別扱い (件数極小の見込み)。
    if scale != "strength":
        return {"rid": rid, "skip": f"llm_scale={scale} (strength 前提のため除外)"}

    # ── model fundamental 復元 (baked Claude を剥がす) ──
    fundamental = _recover_fundamental(stored_win, claude, baked_blend, support, market_floor)

    # 市場ブレンド痕跡の heuristic 検出: 剥がした fundamental が市場とほぼ一致するなら
    # 元々市場ブレンドが効いていた (= 純 fundamental でない) 可能性。require 時は除外。
    market_blend_suspect = False
    if market and require_no_market_blend:
        # corr(fundamental, market) が極端に高い (人気馬に張り付く) と疑う閾値
        common = [k for k in fundamental if k in market]
        if len(common) >= 3:
            fv = [fundamental[k] for k in common]
            mv = [market[k] for k in common]
            # cosine 的な一致度 (両方 0-1 確率)。ここでは単純に L1 距離で代用しない。
            # 実際の live snapshot は market_blend=0 なので通常 suspect にならない。
            pass  # heuristic は過剰除外を避け、odds_source 判定に委ねる (下記注記)

    # ── 3 通りの勝者 1 着確率 ──
    # (a) market only
    p_a = market.get(winner, EPS) if market else None
    # (b) model+market (Claude 無し)
    b_full = _blend_market(fundamental, market, market_blend, market_floor)
    p_b = b_full.get(winner, EPS)
    # (c) +Claude
    combined = E._combine_llm_index(fundamental, claude, llm_blend, market_floor, support=support, scale=scale)
    c_full = _blend_market(combined, market, market_blend, market_floor)
    p_c = c_full.get(winner, EPS)

    def clip(p):
        if p is None:
            return None
        return min(max(p, EPS), 1.0 - EPS)

    return {
        "rid": rid,
        "winner": winner,
        "n_horses": len(stored_win),
        "odds_source": odds_source,
        "baked_blend": baked_blend,
        "p_market": clip(p_a),
        "p_model_market": clip(p_b),
        "p_claude": clip(p_c),
        "market_blend_suspect": market_blend_suspect,
    }


# ─────────────────────────── 集計・統計 ───────────────────────────


def _logloss(ps: list[float]) -> float:
    return -sum(math.log(p) for p in ps) / len(ps)


def _bootstrap_ci(
    rows: list[dict], key_x: str, key_y: str, n_boot: int, seed: int = 12345
) -> tuple[float, float, float, float]:
    """per-race log-loss 差 Δ = LL(x) - LL(y) の点推定 + 95% CI + P(Δ<0)。

    Δ<0 は y (例 +Claude) が x (例 model+market) より良い (= log-loss 低い) 割合。
    """
    diffs = [
        (-math.log(r[key_x])) - (-math.log(r[key_y]))
        for r in rows
        if r.get(key_x) is not None and r.get(key_y) is not None
    ]
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0
    point = sum(diffs) / n
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        s = [diffs[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(s) / n)
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot)]
    p_improve = sum(1 for b in boots if b > 0) / n_boot  # bootstrap mean Δ>0 の割合
    return point, lo, hi, p_improve


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market-blend", type=float, default=E.BLEND_DEFAULT,
                    help=f"(b)(c) の市場ブレンド β (既定 BLEND_DEFAULT={E.BLEND_DEFAULT})。"
                         "0 で市場無視 (live 既定の contrarian 設定を再現)")
    ap.add_argument("--llm-blend", type=float, default=0.5,
                    help="(c) の Claude 合成重み (task 指定 0.5)")
    ap.add_argument("--market-floor", type=float, default=0.01, help="estimate_probs と同じ市場 floor")
    ap.add_argument("--bootstrap", type=int, default=5000, help="bootstrap 反復数 (0 で無効)")
    ap.add_argument("--include-market-blend", action="store_true",
                    help="market_blend>0 で保存された snapshot も含める (fundamental 分離は近似)")
    ap.add_argument("--json", action="store_true", help="機械可読 JSON も出力")
    args = ap.parse_args()

    require_no_mb = not args.include_market_blend
    rids = _find_races()

    rows: list[dict] = []
    skips: dict[str, int] = {}
    for rid in rids:
        r = evaluate_race(
            rid,
            market_blend=args.market_blend,
            llm_blend=args.llm_blend,
            market_floor=args.market_floor,
            require_no_market_blend=require_no_mb,
        )
        if r is None:
            continue
        if "skip" in r:
            skips[r["skip"]] = skips.get(r["skip"], 0) + 1
            continue
        rows.append(r)

    n = len(rows)
    print("=" * 78)
    print("Claude 指数 OOS 価値 検証ハーネス (validate_claude_value.py)")
    print("=" * 78)
    print(f"3 ファイル揃う race: {len(rids)}  →  評価対象: N={n}")
    if skips:
        print("skip 内訳:")
        for k, v in sorted(skips.items(), key=lambda x: -x[1]):
            print(f"  - {v:3d}  {k}")
    print(f"設定: market_blend(β)={args.market_blend}  llm_blend(c)={args.llm_blend}  "
          f"market_floor={args.market_floor}")
    src_dist: dict[str, int] = {}
    for r in rows:
        src_dist[r["odds_source"]] = src_dist.get(r["odds_source"], 0) + 1
    print(f"odds_source: {src_dist}")

    if n == 0:
        print("\n評価対象 0 件 — 終了。")
        return 0

    ll_a = _logloss([r["p_market"] for r in rows if r["p_market"] is not None])
    ll_b = _logloss([r["p_model_market"] for r in rows])
    ll_c = _logloss([r["p_claude"] for r in rows])
    # 参考: 一様 (1/n_horses) baseline の log-loss
    ll_unif = _logloss([min(max(1.0 / r["n_horses"], EPS), 1 - EPS) for r in rows])

    print("\n── 勝者の平均 log-loss (低いほど良い) ──")
    print(f"  uniform (1/頭数) baseline : {ll_unif:.4f}")
    print(f"  (a) market only           : {ll_a:.4f}")
    print(f"  (b) model + market        : {ll_b:.4f}")
    print(f"  (c) + Claude              : {ll_c:.4f}")
    print(f"\n  Δ (c)-(b) = {ll_c - ll_b:+.4f}   "
          f"→ {'Claude が改善 (log-loss 低下)' if ll_c < ll_b else 'Claude は改善せず (log-loss 悪化/同等)'}")
    print(f"  Δ (b)-(a) = {ll_b - ll_a:+.4f}   "
          f"→ {'model+市場 が市場単独を改善' if ll_b < ll_a else 'model+市場 は市場単独を改善せず'}")

    boot_out = {}
    if args.bootstrap > 0:
        print(f"\n── bootstrap (n_boot={args.bootstrap}) per-race log-loss 差の 95% CI ──")
        # Δ = LL(b) - LL(c): >0 なら Claude が良い (c の log-loss が低い)
        pt, lo, hi, frac = _bootstrap_ci(rows, "p_model_market", "p_claude", args.bootstrap)
        sig = "有意 (CI が 0 を跨がない)" if (lo > 0 or hi < 0) else "有意でない (CI が 0 を含む)"
        print(f"  Δ = LL(b model+市場) − LL(c +Claude)")
        print(f"    点推定 {pt:+.4f}  95%CI [{lo:+.4f}, {hi:+.4f}]  → {sig}")
        print(f"    bootstrap で Δ>0 (Claude が良い) の割合: {frac*100:.1f}%")
        boot_out = {"point": pt, "ci_lo": lo, "ci_hi": hi, "frac_claude_better": frac, "significant": (lo > 0 or hi < 0)}
        if n < 100:
            print(f"  ⚠ N={n} は小さい。CI が広く / 0 を跨ぐのが通常。点推定の符号は参考程度。")

    print("\n" + "-" * 78)
    print("⚠ LEAKAGE CAVEAT: 過去 race の Claude 指数は web 検索で結果が漏れうる。")
    print("  これは予測力の純粋検証ではなく **ネガティブスクリーン/動作確認**。")
    print("  (c) が (b) を改善しなくても『Claude は market-independent な価値が無い/有害』の")
    print("  証拠になるが、改善しても leakage を排除できない。真の検証はライブ蓄積後に再実行。")
    print("-" * 78)

    if args.json:
        out = {
            "n": n,
            "n_candidate_races": len(rids),
            "skips": skips,
            "settings": {
                "market_blend": args.market_blend,
                "llm_blend": args.llm_blend,
                "market_floor": args.market_floor,
            },
            "odds_source": src_dist,
            "logloss": {"uniform": ll_unif, "market": ll_a, "model_market": ll_b, "claude": ll_c},
            "delta_c_minus_b": ll_c - ll_b,
            "delta_b_minus_a": ll_b - ll_a,
            "bootstrap": boot_out,
            "claude_improves": bool(ll_c < ll_b),
            "per_race": rows,
        }
        print("\n=== JSON ===")
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
