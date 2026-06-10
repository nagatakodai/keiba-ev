#!/usr/bin/env python3
"""Benter 2-step の本実装: 蓄積 live データで α,β を conditional logit MLE 推定する。

Benter (1994) の combined model:
    c_i = exp(α·ln f_i + β·ln π_i) / Σ_j exp(α·ln f_j + β·ln π_j)
      f = モデル (市場フリー fundamental, Claude 指数込み)
      π = 市場暗黙率 (de-vig 済)

現行 production は α+β=1 の凸結合 (β=0.78 固定) だが、Benter 流は **α,β を自由に**
データで推定する (合成が市場より sharp になり得る)。本スクリプトは
data/predictions × data/results から勝者尤度を最大化する (α,β) を fit し、
data/models/blend_mle.json に保存する。

f の取得:
  - 新 snapshot (2026-06-10〜): `win_probs_model` (market_blend=0 の probs_t.win) を直接使用。
  - 旧 snapshot (live β=0 時代): bet_tables.win の prob がそのまま市場フリー値。
π の取得: bet_tables.win の odds から 1/odds (未正規化) → power_method_overround。

⚠ まだ live 配線はしない (報告のみ)。N が十分溜まり係数が安定したら
   estimate_probs に free-(α,β) パスを足して切替える。

使い方:
    .venv/bin/python scripts/fit_blend_mle.py
    .venv/bin/python scripts/fit_blend_mle.py --since 20260605 --min-races 100
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RES_DIR = ROOT / "data" / "results"
OUT_PATH = ROOT / "data" / "models" / "blend_mle.json"

sys.path.insert(0, str(ROOT))
from src.ev import power_method_overround  # noqa: E402


def load_races(since: str | None):
    """(log f, log π, winner_idx) の配列リストを返す。"""
    races = []
    for rf in sorted(glob.glob(str(RES_DIR / "*.json"))):
        rid = os.path.basename(rf)[:-5]
        pf = PRED_DIR / f"{rid}.json"
        if not pf.exists():
            continue
        r = json.load(open(rf))
        fo = r.get("finish_order") or []
        if not fo:
            continue
        d = json.load(open(pf))
        # 日付フィルタは saved_at で行う (race_id 先頭8桁は年+場コードで日付ではない)。
        if since and (d.get("saved_at") or "")[:10].replace("-", "") < since:
            continue
        win_rows = (d.get("bet_tables") or {}).get("win") or []
        odds = {row["key"][0]: row["odds"] for row in win_rows if row.get("odds", 0) > 0}
        if len(odds) < 3:
            continue
        # f: 新フィールド優先、無ければ旧 β=0 時代の bet_tables.win prob。
        # 新レジーム (model_no_info キーあり = MARKET_BLEND_LIVE=0.78 以降) なのに
        # win_probs_model が無い snapshot は bet_tables.win が市場ブレンド済みで
        # fundamental 不明 → fit から除外 (混ぜると α が市場へ偽膨張する)。
        wpm = d.get("win_probs_model")
        if wpm:
            f = {int(k): float(v) for k, v in wpm.items()}
        elif "model_no_info" in d:
            continue
        else:
            f = {row["key"][0]: row.get("prob") or 0.0 for row in win_rows}
        horses = [n for n in odds if f.get(n, 0.0) > 0]
        if len(horses) < 3 or fo[0] not in horses:
            continue
        raw = {n: 1.0 / odds[n] for n in horses}
        try:
            pi = power_method_overround(raw)
        except Exception:
            s = sum(raw.values())
            pi = {k: v / s for k, v in raw.items()}
        fs = sum(f[n] for n in horses)
        pis = sum(pi[n] for n in horses)
        lf = np.array([math.log(max(f[n] / fs, 1e-9)) for n in horses])
        lp = np.array([math.log(max(pi[n] / pis, 1e-9)) for n in horses])
        races.append((lf, lp, horses.index(fo[0])))
    return races


def neg_ll(params, races) -> float:
    a, b = params
    total = 0.0
    for lf, lp, wi in races:
        z = a * lf + b * lp
        z = z - z.max()
        total += z[wi] - math.log(np.exp(z).sum())
    return -total / len(races)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYYMMDD 以降のみ")
    ap.add_argument("--min-races", type=int, default=50)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    races = load_races(args.since)
    n = len(races)
    print(f"評価対象 race: N={n}")
    if n < args.min_races:
        print(f"⚠ N < {args.min_races}: fit はするが係数の採用は推奨しない")

    # ベースライン
    ll_market = -neg_ll((0.0, 1.0), races)
    ll_model = -neg_ll((1.0, 0.0), races)
    ll_prod = -neg_ll((0.22, 0.78), races)   # 現行 α+β=1, β=0.78
    print(f"log-lik/race: market only (α=0,β=1) : {ll_market:.4f}")
    print(f"log-lik/race: model only  (α=1,β=0) : {ll_model:.4f}")
    print(f"log-lik/race: production  (α=.22,β=.78): {ll_prod:.4f}")

    # 自由 MLE
    res = minimize(neg_ll, x0=np.array([0.3, 0.9]), args=(races,), method="Nelder-Mead",
                   options={"xatol": 1e-4, "fatol": 1e-7, "maxiter": 2000})
    a, b = float(res.x[0]), float(res.x[1])
    ll_free = -res.fun
    print(f"log-lik/race: free MLE    (α={a:.3f},β={b:.3f}): {ll_free:.4f}")
    print(f"  Δ vs market-only: {ll_free - ll_market:+.4f} "
          f"({'モデルは市場に上乗せあり' if ll_free > ll_market + 1e-4 else 'モデルの上乗せは確認できない'})")

    # bootstrap CI (race resample)
    rng = np.random.default_rng(7)
    boots = []
    for _ in range(200):
        idx = rng.integers(0, n, n)
        sample = [races[i] for i in idx]
        r2 = minimize(neg_ll, x0=np.array([a, b]), args=(sample,), method="Nelder-Mead",
                      options={"xatol": 1e-3, "fatol": 1e-6, "maxiter": 500})
        boots.append(r2.x)
    boots = np.array(boots)
    a_ci = np.percentile(boots[:, 0], [2.5, 97.5])
    b_ci = np.percentile(boots[:, 1], [2.5, 97.5])
    print(f"bootstrap 95%CI: α [{a_ci[0]:.3f}, {a_ci[1]:.3f}]  β [{b_ci[0]:.3f}, {b_ci[1]:.3f}]")
    alpha_sig = a_ci[0] > 0.0
    print(f"α>0 が有意: {'YES — モデル成分に独立情報' if alpha_sig else 'NO — 市場のみで十分 (β≈1 を使うべき)'}")

    if not args.no_save:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        json.dump({
            "alpha": a, "beta": b, "n_races": n,
            "alpha_ci95": list(map(float, a_ci)), "beta_ci95": list(map(float, b_ci)),
            "ll_free": ll_free, "ll_market_only": ll_market,
            "ll_model_only": ll_model, "ll_production": ll_prod,
            "alpha_significant": bool(alpha_sig),
            "since": args.since, "fitted_at": dt.datetime.now().isoformat(timespec="seconds"),
            "note": "報告のみ・live 未配線。係数が複数 fit で安定したら estimate_probs に free-(α,β) パスを追加して採用する。",
        }, open(OUT_PATH, "w"), ensure_ascii=False, indent=1)
        print(f"saved: {OUT_PATH}")


if __name__ == "__main__":
    main()
