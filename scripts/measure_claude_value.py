#!/usr/bin/env python3
"""Claude 指数/alerts が model+市場 に対し OOS で価値を足すかを測る harness.

過去レースを Claude の web 検索で再評価すると結果が漏れる (leakage) ので、本 harness は
**ライブ蓄積された snapshot を再計算なし・再スクレイプなしで**評価する (leakage ゼロ・決定的)。

対象 = `llm_win_index` 非 null かつ result ありの race。各 race で「実1着馬」を当てる top-1 を
3 つの 1着確率分布で比較:

  (a) 市場のみ      … win_odds を de-vig (1/odds 正規化 → power_method_overround)。
  (b) model+市場    … snapshot から **モデル fundamental を逆算**し、market と β=0.78 で loglinear 合成
                      (Claude を除外)。
  (c) (b)+Claude    … production の最終 win 周辺確率 (= snapshot `rows` の3連単 joint から
                      P(i が1着)=Σ_{key[0]=i} を周辺化)。これは _combine_llm_index で Claude 指数を
                      合成済み → さらに market 合成済みの本番分布。

なぜ snapshot だけで (a)(b)(c) が出せるか:
  production 分布 (c) = loglinear( market^β , fundamental_combined^(1-β) ) であり、
  fundamental_combined = loglinear( fundamental_model^(1-w) , softmax(llm/T_LLM)^w )。
  market は win_odds から、llm は llm_win_index から、β=BLEND_DEFAULT・w=llm_blend・T=T_LLM は既知。
  → 2 段の loglinear を解析的に逆算して fundamental_model を復元できる (実測 max_err ~1e-5)。
  これで (b) を Claude 抜きで前向き再合成できる。再スクレイプ不要・本番コードの定数をそのまま使う。

指標:
  - log loss (1着 top-1)  … Σ -log p(実1着馬)。低いほど良い。確率 calibration の純粋指標。
  - hit@1                 … argmax p が実1着馬と一致した率。
  - 単勝 ROI              … argmax p の馬に 100 円 → 的中で win_odds×100 回収。100% 超で +EV。

N が小さい (Claude 指数つき snapshot は現状 ~40race) ので結論は不確実。N が増えたら再実行。

usage:
  .venv/bin/python scripts/measure_claude_value.py
  .venv/bin/python scripts/measure_claude_value.py --json    # 機械可読出力
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ev as E  # noqa: E402  本番の定数/関数 (BLEND_DEFAULT, T_LLM, power_method_overround)

PRED_DIR = "data/predictions"
RES_DIR = "data/results"
FLOOR = 1e-9


def _norm(d: dict[int, float]) -> dict[int, float]:
    s = sum(d.values()) or 1.0
    return {k: v / s for k, v in d.items()}


def _softmax_loglinear(terms: dict[int, float]) -> dict[int, float]:
    """terms[k] = 合成済 log 値 → softmax で正規化."""
    if not terms:
        return {}
    mx = max(terms.values())
    e = {k: math.exp(v - mx) for k, v in terms.items()}
    return _norm(e)


def market_devig(snap: dict) -> dict[int, float]:
    """(a) 市場のみ: win_odds → 1/odds 正規化 → power_method_overround (estimate_probs と同手順)."""
    odds = {
        int(m["number"]): float(m["win_odds"])
        for m in snap.get("market_signals", [])
        if m.get("win_odds")
    }
    if not odds:
        return {}
    raw = _norm({k: 1.0 / v for k, v in odds.items()})
    try:
        pm = E.power_method_overround(raw)
    except Exception:
        pm = raw
    return _norm(pm)


def production_win(snap: dict) -> dict[int, float]:
    """(c) 本番 win 周辺確率: rows の3連単 joint から P(i が1着) を周辺化."""
    win: dict[int, float] = defaultdict(float)
    for r in snap.get("rows", []):
        key = r.get("key")
        if key:
            win[int(key[0])] += float(r.get("prob", 0.0))
    return _norm(dict(win)) if win else {}


def recover_fundamental(snap: dict, c_win: dict[int, float], mk: dict[int, float]) -> dict[int, float]:
    """snapshot の最終 win (c) と market (a) から モデル fundamental を逆算.

    (c) = loglinear(market^β, fc^(1-β)),  fc = loglinear(fund^(1-w_k), L^w_k)
    L = softmax(llm/T_LLM),  w_k = llm_blend (Claude がスコアした馬のみ, 他は 0)。
    2 段とも単純な loglinear なので log 空間で線形に解ける。
    """
    beta = E.BLEND_DEFAULT
    t_llm = E.T_LLM
    keys = sorted(c_win)
    llm = {int(k): float(v) for k, v in (snap.get("llm_win_index") or {}).items()}
    w_llm = float(snap.get("llm_blend") or 0.0)
    scored = set(llm)

    # step1: market 合成を逆算 → fundamental_combined (fc)
    # log c = β·log mk + (1-β)·log fc + const  =>  log fc = (log c - β·log mk)/(1-β)
    if beta >= 1.0:
        fc = dict(c_win)
    else:
        logfc = {
            k: (math.log(max(c_win.get(k, FLOOR), FLOOR)) - beta * math.log(max(mk.get(k, FLOOR), FLOOR)))
            / (1.0 - beta)
            for k in keys
        }
        fc = _softmax_loglinear(logfc)

    # step2: Claude 合成を逆算 → fundamental_model
    # L = softmax(llm/T)
    if llm:
        rm = max(llm.values())
        L = _norm({k: math.exp((llm.get(k, 0.0) - rm) / t_llm) for k in keys})
    else:
        L = {k: 1.0 / len(keys) for k in keys}
    logfund = {}
    for k in keys:
        w = w_llm if k in scored else 0.0
        lfc = math.log(max(fc.get(k, FLOOR), FLOOR))
        if w >= 1.0:
            logfund[k] = lfc
        else:
            lL = math.log(max(L.get(k, FLOOR), FLOOR))
            logfund[k] = (lfc - w * lL) / (1.0 - w)
    return _softmax_loglinear(logfund)


def model_plus_market(snap: dict, fund: dict[int, float], mk: dict[int, float]) -> dict[int, float]:
    """(b) model+市場 (Claude 抜き): loglinear(market^β, fundamental_model^(1-β))."""
    beta = E.BLEND_DEFAULT
    keys = sorted(set(fund) | set(mk))
    logs = {
        k: beta * math.log(max(mk.get(k, FLOOR), FLOOR))
        + (1.0 - beta) * math.log(max(fund.get(k, FLOOR), FLOOR))
        for k in keys
    }
    return _softmax_loglinear(logs)


def reconstruct_c(snap: dict, fund: dict[int, float], mk: dict[int, float]) -> dict[int, float]:
    """復元した fundamental から本番 (c) を前向きに再合成 (逆算の自己検証用)。

    fc = loglinear(fund^(1-w_k), L^w_k);  c = loglinear(market^β, fc^(1-β))。
    これが snapshot の rows 周辺確率と一致するなら、復元 fundamental → (b) が信頼できる
    (floor が binding でない)。一致しなければ floor/高 llm_blend で逆算が荒れている合図。
    """
    beta = E.BLEND_DEFAULT
    t_llm = E.T_LLM
    keys = sorted(set(fund) | set(mk))
    llm = {int(k): float(v) for k, v in (snap.get("llm_win_index") or {}).items()}
    w_llm = float(snap.get("llm_blend") or 0.0)
    scored = set(llm)
    if llm:
        rm = max(llm.values())
        L = _norm({k: math.exp((llm.get(k, 0.0) - rm) / t_llm) for k in keys})
    else:
        L = {k: 1.0 / len(keys) for k in keys}
    fc_logs = {}
    for k in keys:
        w = w_llm if k in scored else 0.0
        fc_logs[k] = (1.0 - w) * math.log(max(fund.get(k, FLOOR), FLOOR)) + w * math.log(max(L.get(k, FLOOR), FLOOR))
    fc = _softmax_loglinear(fc_logs)
    c_logs = {
        k: beta * math.log(max(mk.get(k, FLOOR), FLOOR)) + (1.0 - beta) * math.log(max(fc.get(k, FLOOR), FLOOR))
        for k in keys
    }
    return _softmax_loglinear(c_logs)


# (b) を信頼する逆算誤差の上限。これを超えた race は floor/高 llm_blend で fundamental 復元が
# 荒れるので (b) の集計から除外 (a/c は厳密なので常に残す)。
RECON_TOL = 5e-3


def load_cohort() -> list[dict]:
    """llm_win_index 非 null かつ result あり の race を集めて評価素材を組む."""
    cohort = []
    for fn in sorted(os.listdir(PRED_DIR)):
        if not fn.endswith(".json") or fn.endswith(".llm.json"):
            continue
        rid = fn[:-5]
        try:
            snap = json.load(open(os.path.join(PRED_DIR, fn)))
        except Exception:
            continue
        if not snap.get("llm_win_index"):
            continue
        if snap.get("llm_fallback"):
            continue  # 念のため: fallback=Claude 未反映なので除外
        res_path = os.path.join(RES_DIR, f"{rid}.json")
        if not os.path.exists(res_path):
            continue
        try:
            res = json.load(open(res_path))
        except Exception:
            continue
        order = res.get("finish_order") or []
        if not order:
            continue
        winner = int(order[0])

        mk = market_devig(snap)
        c_win = production_win(snap)
        if not mk or not c_win:
            continue
        fund = recover_fundamental(snap, c_win, mk)
        b_win = model_plus_market(snap, fund, mk)
        # 逆算の自己検証: 復元 fundamental から (c) を前向き再合成 → rows 周辺と一致するか。
        c_recon = reconstruct_c(snap, fund, mk)
        recon_err = max(abs(c_recon.get(k, 0.0) - c_win.get(k, 0.0)) for k in c_win)

        odds = {
            int(m["number"]): float(m["win_odds"])
            for m in snap.get("market_signals", [])
            if m.get("win_odds")
        }
        cohort.append(
            {
                "rid": rid,
                "winner": winner,
                "order": [int(x) for x in order],
                "odds": odds,
                "a": mk,
                "b": b_win,
                "c": c_win,
                "fund": fund,
                "recon_err": recon_err,
                "b_ok": recon_err <= RECON_TOL,  # (b) の fundamental 復元が信頼できるか
                "llm_blend": float(snap.get("llm_blend") or 0.0),
                "n_horses": len(c_win),
                "venue": snap.get("venue_name", ""),
                "odds_source": snap.get("odds_source", ""),
            }
        )
    return cohort


def argmax(d: dict[int, float]) -> int:
    return max(d.items(), key=lambda kv: kv[1])[0]


def evaluate(cohort: list[dict], variant: str) -> dict:
    """variant ('a'|'b'|'c') の top-1 log loss / hit@1 / 単勝 ROI を集計.

    variant=='b' のときは逆算が信頼できる race (b_ok) のみで集計する (高 llm_blend/floor で
    fundamental 復元が荒れた race を除外)。a/c は厳密なので全 race。
    """
    ll = 0.0
    hits = 0
    staked = 0.0
    returned = 0.0
    n = 0
    for r in cohort:
        if variant == "b" and not r["b_ok"]:
            continue
        dist = r[variant]
        w = r["winner"]
        if w not in dist:
            continue  # 取消等で winner が分布に無い (極稀) → skip
        n += 1
        p = max(dist.get(w, FLOOR), 1e-15)
        ll += -math.log(p)
        pick = argmax(dist)
        staked += 100.0
        if pick == w:
            hits += 1
            returned += 100.0 * r["odds"].get(w, 0.0)
    return {
        "n": n,
        "logloss": ll / n if n else float("nan"),
        "hit": hits / n if n else float("nan"),
        "hits": hits,
        "roi": returned / staked if staked else float("nan"),
        "returned": returned,
        "staked": staked,
    }


def paired_loglik_advantage(cohort: list[dict], v1: str, v2: str) -> dict:
    """v1 と v2 の per-race log p(winner) 差 (v1 - v2) の平均/SE/レース毎勝率。
    符号: 正なら v1 が winner により高い確率を割り当てた = 良い。"""
    needs_b = "b" in (v1, v2)
    diffs = []
    for r in cohort:
        if needs_b and not r["b_ok"]:
            continue  # 逆算が荒れた race は (b) 絡みの対比較から除外
        w = r["winner"]
        if w not in r[v1] or w not in r[v2]:
            continue
        p1 = max(r[v1].get(w, FLOOR), 1e-15)
        p2 = max(r[v2].get(w, FLOOR), 1e-15)
        diffs.append(math.log(p1) - math.log(p2))
    if not diffs:
        return {"n": 0}
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((x - mean) ** 2 for x in diffs) / max(n - 1, 1)
    se = (var / n) ** 0.5 if n > 1 else float("nan")
    wins = sum(1 for x in diffs if x > 1e-9)
    ties = sum(1 for x in diffs if abs(x) <= 1e-9)
    return {"n": n, "mean": mean, "se": se, "t": (mean / se if se and se == se else float("nan")),
            "v1_better": wins, "ties": ties, "v2_better": n - wins - ties}


def alerts_qualitative(cohort_rids: set[str]) -> list[str]:
    """alerts の軽い qualitative チェック: alert が付いた馬が実際に凡走 (着外) したか。
    取消 alert は現状ほぼ無い。馬体重大幅減/距離不安 等の軟マイナス alert が着外と相関するか見る."""
    out = []
    neg_kw = ("取消", "除外", "回避", "馬体重-", "距離不安", "休み明け", "格上げ")
    rows = []
    for rid in sorted(cohort_rids):
        snap = json.load(open(os.path.join(PRED_DIR, f"{rid}.json")))
        res = json.load(open(os.path.join(RES_DIR, f"{rid}.json")))
        order = [int(x) for x in (res.get("finish_order") or [])]
        top3 = set(order[:3])
        alerts = snap.get("llm_alerts") or {}
        if not alerts:
            # index_compare からも拾う
            for ic in snap.get("index_compare", []):
                if ic.get("alerts"):
                    alerts[str(ic["number"])] = ic["alerts"]
        for num, al in alerts.items():
            al = al or []
            neg = [a for a in al if any(k in a for k in neg_kw)]
            if not neg:
                continue
            n = int(num)
            in_top3 = n in top3
            rows.append((rid, n, neg, in_top3))
    if not rows:
        return ["  alerts 付き race が現状 0 (取消/軟マイナス alert がほぼ蓄積されていない)。"]
    flagged = len(rows)
    busted = sum(1 for *_, t in rows if not t)
    out.append(f"  ネガティブ alert 付き (馬, race): {flagged} 件")
    out.append(f"    うち着外 (top3 圏外) = {busted}/{flagged}  ← alert 通りに凡走した割合")
    for rid, n, neg, t in rows[:12]:
        out.append(f"    {rid} 馬{n}: {neg}  → {'着内' if t else '着外'}")
    out.append("  ※ N が極小。取消 alert はまだ無くサンプルが軟マイナスのみなので参考値。")
    return out


def fmt_pct(x: float) -> str:
    return f"{x*100:5.1f}%" if x == x else "  n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="機械可読 JSON で出力")
    ap.add_argument("--min-n", type=int, default=1, help="この N 未満なら警告のみ")
    args = ap.parse_args()

    cohort = load_cohort()
    N = len(cohort)

    ra = evaluate(cohort, "a")
    rb = evaluate(cohort, "b")
    rc = evaluate(cohort, "c")

    cb_vs_a = paired_loglik_advantage(cohort, "c", "a")   # 本番(Claude込) vs 市場
    c_vs_b = paired_loglik_advantage(cohort, "c", "b")    # Claude 寄与の純差分
    b_vs_a = paired_loglik_advantage(cohort, "b", "a")    # model 寄与 (Claude 抜き)

    if args.json:
        print(json.dumps({
            "n_races": N,
            "n_b_excluded_recon": sum(1 for r in cohort if not r["b_ok"]),
            "recon_tol": RECON_TOL,
            "a_market": ra, "b_model_market": rb, "c_plus_claude": rc,
            "paired_c_minus_b_claude_effect": c_vs_b,
            "paired_c_minus_a": cb_vs_a,
            "paired_b_minus_a_model_effect": b_vs_a,
        }, ensure_ascii=False, indent=2))
        return

    print("=" * 78)
    print("Claude 指数/alerts の OOS 価値測定 (ライブ蓄積 snapshot, 再スクレイプなし=leakage ゼロ)")
    print("=" * 78)
    print(f"対象 race (llm_win_index 非null かつ result あり) : N = {N}")
    avg_blend = sum(r["llm_blend"] for r in cohort) / N if N else 0.0
    print(f"平均 llm_blend = {avg_blend:.2f}  /  T_LLM = {E.T_LLM}  /  β(market) = {E.BLEND_DEFAULT}")
    src = defaultdict(int)
    for r in cohort:
        src[r["odds_source"]] += 1
    print(f"odds 源内訳: {dict(src)}")
    n_b_excl = sum(1 for r in cohort if not r["b_ok"])
    if n_b_excl:
        bad = [(r["rid"], round(r["recon_err"], 3), r["llm_blend"]) for r in cohort if not r["b_ok"]]
        print(f"(b) から除外 (逆算誤差 > {RECON_TOL}): {n_b_excl} race  "
              f"→ a/c は全 {N}、b は {N - n_b_excl}。除外={bad}")
        print("  ※ 高 llm_blend で fundamental 復元が荒れる race のみ。(a)/(c) は厳密。")
    print()
    print("top-1 で『実1着馬』を当てる 3 分布の比較:")
    print(f"  {'variant':<26} {'N':>4} {'logloss':>9} {'hit@1':>7} {'tan-ROI':>8} {'hits':>5}")
    print("  " + "-" * 64)
    rows = [
        ("(a) 市場のみ (de-vig)", ra),
        ("(b) model+市場 (Claude抜)", rb),
        ("(c) (b)+Claude指数 [本番]", rc),
    ]
    for label, r in rows:
        print(f"  {label:<26} {r['n']:>4} {r['logloss']:>9.4f} {fmt_pct(r['hit']):>7} "
              f"{fmt_pct(r['roi']):>8} {r['hits']:>5}")
    print()
    print("Claude を足すと OOS で改善するか (per-race log p(実1着馬) の対比較):")

    def show(tag, p):
        if p.get("n", 0) == 0:
            print(f"  {tag}: n=0")
            return
        sign = "+" if p["mean"] >= 0 else ""
        print(f"  {tag}")
        print(f"      Δmean log p = {sign}{p['mean']:.4f}  (SE {p['se']:.4f}, t≈{p['t']:.2f}, n={p['n']})")
        print(f"      レース毎 勝敗: 前者勝ち {p['v1_better']} / 引分 {p['ties']} / 後者勝ち {p['v2_better']}")

    show("(c) − (b)  = Claude 指数の純寄与", c_vs_b)
    show("(b) − (a)  = モデル fundamental の寄与", b_vs_a)
    show("(c) − (a)  = 本番(モデル+Claude) vs 市場", cb_vs_a)
    print()
    print("alerts の qualitative チェック (取消/軟マイナス alert → 実際に凡走したか):")
    for line in alerts_qualitative({r["rid"] for r in cohort}):
        print(line)
    print()
    print("解釈:")
    # 簡単な自動コメント (logloss/符号は b_ok のみで揃えた paired 比較が正; ROI/hit は全 race の集計値)
    d_roi = rc["roi"] - rb["roi"]
    if c_vs_b.get("n"):
        # paired は b_ok race のみ → (c)−(b) の正味 logloss 改善 = -Δmean log p
        d_ll = -c_vs_b["mean"]
        verdict_ll = ("改善(logloss低下)" if d_ll < -1e-4 else
                      "悪化(logloss上昇)" if d_ll > 1e-4 else "ほぼ不変")
        print(f"  ・(c) vs (b) logloss 差 (paired, b_ok の {c_vs_b['n']} race) = {d_ll:+.4f} "
              f"→ Claude で {verdict_ll}")
        favor = "Claude 寄り(+)" if c_vs_b["mean"] > 0 else "モデルのみ寄り(-)"
        sig = "有意でない (|t|<2)" if abs(c_vs_b.get("t", 0)) < 2 else "弱い有意性 (|t|≥2)"
        print(f"  ・Claude 純寄与の符号: {favor} / 統計的には {sig} (t≈{c_vs_b.get('t', float('nan')):.2f})")
    print(f"  ・(c) vs (b) 単勝ROI 差 = {d_roi*100:+.1f}pt, hit 差 = {(rc['hit']-rb['hit'])*100:+.1f}pt "
          f"(全 race 集計, 1着馬に100円)")
    print(f"  ・現状 N={N} と小さく結論は不確実。Claude 指数つき snapshot がライブ蓄積で")
    print("    増えたら本 harness を再実行して符号と有意性を再確認すること。")
    print("  ・過去 race の Claude 再評価は web 検索で結果が漏れる (leakage) ため本 harness は")
    print("    必ず『蓄積済 snapshot の再計算なし評価』である点に注意 (再スクレイプ禁止)。")
    if N < args.min_n:
        print(f"\n[WARN] N={N} < --min-n={args.min_n}: 数値は参考値。")


if __name__ == "__main__":
    main()
