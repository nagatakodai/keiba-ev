#!/usr/bin/env python3
"""束の事後較正レポート — 「EV の数字を現実に合わせる」の定点観測。

data/predictions/*.json (snapshot) × data/results/*.json を突合し、
  1. 系列別 実ROI (EV束 / 3連単束 × mode × rank_source)
  2. 券種別 楽観係数 (Σ予測prob ÷ 実的中数 — 1.0 が完全較正、>1 が楽観)
  3. 束レベル較正 (予測 bundle_hit_prob vs 実測的中率)
  4. 券種別オッズドリフト (確定オッズ/保存オッズ、DRIFT_SHADE の較正材料)
  5. 単勝 top-1 較正 bin (予測1着確率帯ごとの実勝率)
を出す。読み取り専用 (本番コード・データを一切変更しない)。

運用: 週次などで実行し、
  - 楽観係数が 1 から大きくズレ続ける券種 → ev/portfolio の確率・シェード較正へ反映
  - DRIFT_SHADE 提案値が現行とズレたら src/portfolio.py の表を更新
  - 系列 ROI が rolling で 100% を超えるまで実弾 bankroll を上げない

使い方:
    .venv/bin/python scripts/bundle_calibration_report.py
    .venv/bin/python scripts/bundle_calibration_report.py --since 20260605 --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RES_DIR = ROOT / "data" / "results"

sys.path.insert(0, str(ROOT))
from src.portfolio import DRIFT_SHADE, TORIGAMI_MARGIN  # noqa: E402


def _final_odds_key(bet_type: str, key: list) -> str:
    if bet_type in ("win", "place"):
        return f"{bet_type}:{key[0]}"
    if bet_type in ("quinella", "wide", "trio"):
        return f"{bet_type}:{'-'.join(map(str, sorted(key)))}"
    return f"{bet_type}:{'-'.join(map(str, key))}"


def _leg_hits(bet_type: str, key: list, fo: list[int], n_runners: int | None = None) -> bool:
    a, b, c = fo[0], fo[1], fo[2]
    top3 = {a, b, c}
    if bet_type == "win":
        return key[0] == a
    if bet_type == "place":
        # 出走頭数ルール: 7頭以下は複勝2着まで・4頭以下は発売なし (2026-06-10 bughunt 修正)
        if n_runners is not None and n_runners <= 4:
            return False
        if n_runners is not None and n_runners <= 7:
            return key[0] in (a, b)
        return key[0] in top3
    if bet_type == "quinella":
        return set(key) == {a, b}
    if bet_type == "wide":
        return set(key) <= top3
    if bet_type == "exacta":
        return tuple(key) == (a, b)
    if bet_type == "trio":
        return set(key) == top3
    if bet_type == "trifecta":
        return tuple(key) == (a, b, c)
    return False


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    i = max(0, min(len(xs) - 1, int(p / 100 * len(xs))))
    return xs[i]


def _wilson(hits: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval (的中率の信頼区間)。"""
    if n == 0:
        return 0.0, 1.0
    z = 1.96
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, center - half), min(1.0, center + half)


def load_pairs(since: str | None):
    pairs = []
    for rf in sorted(glob.glob(str(RES_DIR / "*.json"))):
        rid = os.path.basename(rf)[:-5]
        pf = PRED_DIR / f"{rid}.json"
        if not pf.exists():
            continue
        r = json.load(open(rf))
        fo = r.get("finish_order") or []
        if len(fo) < 3:
            continue
        d = json.load(open(pf))
        # 日付フィルタは snapshot の saved_at で行う。race_id 先頭8桁は「年+場コード」で
        # 日付ではない (NAR は rid[6:10] が MMDD、JRA は回/日) ため rid 比較は誤フィルタになる。
        if since and (d.get("saved_at") or "")[:10].replace("-", "") < since:
            continue
        pairs.append((rid, d, r))
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYYMMDD 以降の race のみ")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    args = ap.parse_args()

    pairs = load_pairs(args.since)
    out: dict = {"n_races": len(pairs)}

    # ---- 1. 系列別 実ROI ----
    series = defaultdict(lambda: dict(n=0, stake=0.0, ret=0.0, hits=0))
    # ---- 2. 券種別 楽観係数 ----
    bt_cal = defaultdict(lambda: dict(n=0, pred=0.0, hits=0))
    # ---- 3. 束レベル較正 ----
    bundle_cal = defaultdict(lambda: dict(n=0, pred=0.0, hits=0))
    # ---- 4. ドリフト ----
    drift = defaultdict(list)
    # ---- 5. 単勝 top-1 較正 bin ----
    win_bins = defaultdict(lambda: dict(n=0, pred=0.0, hits=0))

    for rid, d, r in pairs:
        fo = r["finish_order"]
        final = r.get("final_odds") or {}
        tri_payout = r.get("trifecta_payout")
        # 出走頭数 (複勝の頭数ルール用)。win_probs_model → bet_tables.win で推定。
        n_runners = None
        for src_field in (d.get("win_probs_model"), (d.get("bet_tables") or {}).get("win")):
            if src_field:
                n_runners = len(src_field)
                break

        for kind, b in (("ev", d.get("recommended_bundle")),
                        ("t", d.get("recommended_bundle_t"))):
            if not b or not b.get("legs"):
                continue
            if kind == "t":
                mode = b.get("mode") or "hit(旧)"
                label = f"3連単束/{mode}/{b.get('rank_source') or '?'}"
            else:
                label = "EV束"
            s = series[label]
            s["n"] += 1
            race_hit = False
            for l in b["legs"]:
                bt, key, stake = l["bet_type"], l["key"], l["stake"]
                s["stake"] += stake
                c = bt_cal[bt]
                c["n"] += 1
                c["pred"] += l.get("prob") or 0.0
                hit = _leg_hits(bt, key, fo, n_runners)
                fok = _final_odds_key(bt, key)
                f_odds = final.get(fok)
                if f_odds and l.get("odds"):
                    drift[bt].append(f_odds / l["odds"])
                if hit:
                    c["hits"] += 1
                    race_hit = True
                    if bt == "trifecta" and tri_payout:
                        s["ret"] += tri_payout * stake / 100.0
                    else:
                        s["ret"] += (f_odds or l["odds"]) * stake
                    s["hits"] += 0  # per-leg ではなく per-race で数える (下)
            if race_hit:
                s["hits"] += 1
            bc = bundle_cal[label]
            bc["n"] += 1
            bc["pred"] += b.get("bundle_hit_prob") or b.get("covered_prob") or 0.0
            bc["hits"] += 1 if race_hit else 0

        # 単勝 top-1 較正 (bet_tables.win の確率1位)
        win_rows = (d.get("bet_tables") or {}).get("win") or []
        if win_rows:
            top = max(win_rows, key=lambda x: x.get("prob") or 0.0)
            p = top.get("prob") or 0.0
            bink = f"{int(p * 10) * 10:02d}-{int(p * 10) * 10 + 10:02d}%"
            wb = win_bins[bink]
            wb["n"] += 1
            wb["pred"] += p
            wb["hits"] += 1 if top["key"][0] == fo[0] else 0

    # ---- print ----
    def emit(title: str) -> None:
        print(f"\n=== {title} ===")

    emit(f"系列別 実ROI (n_races={len(pairs)}, since={args.since or '全期間'})")
    out["series"] = {}
    for label, s in sorted(series.items()):
        roi = s["ret"] / s["stake"] * 100 if s["stake"] else 0.0
        lo, hi = _wilson(s["hits"], s["n"])
        out["series"][label] = dict(races=s["n"], hits=s["hits"], stake=s["stake"],
                                    ret=round(s["ret"]), roi=round(roi, 1))
        print(f"  {label:28s} races={s['n']:4d} hit={s['hits']:3d} "
              f"({s['hits']/s['n']*100 if s['n'] else 0:.1f}% CI[{lo*100:.0f},{hi*100:.0f}]) "
              f"stake=¥{s['stake']:,.0f} ret=¥{s['ret']:,.0f} ROI={roi:.1f}%")

    emit("券種別 楽観係数 (Σ予測prob ÷ 実的中数; >1 = 楽観)")
    out["bet_type_calibration"] = {}
    for bt, c in sorted(bt_cal.items()):
        factor = c["pred"] / c["hits"] if c["hits"] else float("inf")
        out["bet_type_calibration"][bt] = dict(legs=c["n"], pred_hits=round(c["pred"], 1),
                                               hits=c["hits"],
                                               optimism=round(factor, 2) if c["hits"] else None)
        print(f"  {bt:10s} legs={c['n']:5d} 予測的中数={c['pred']:7.1f} 実的中={c['hits']:4d} "
              f"楽観係数={'∞' if not c['hits'] else f'{factor:.2f}'}")

    emit("束レベル較正 (平均予測的中率 vs 実測的中率)")
    out["bundle_calibration"] = {}
    for label, bc in sorted(bundle_cal.items()):
        pred = bc["pred"] / bc["n"] * 100 if bc["n"] else 0
        real = bc["hits"] / bc["n"] * 100 if bc["n"] else 0
        out["bundle_calibration"][label] = dict(n=bc["n"], pred_pct=round(pred, 1),
                                                real_pct=round(real, 1))
        print(f"  {label:28s} n={bc['n']:4d} 予測 {pred:5.1f}% vs 実測 {real:5.1f}%")

    emit(f"券種別ドリフト 確定/保存 オッズ比 (現行 DRIFT_SHADE / margin={TORIGAMI_MARGIN})")
    out["drift"] = {}
    for bt, xs in sorted(drift.items()):
        if len(xs) < 3:
            print(f"  {bt:10s} n={len(xs)} (少なすぎて省略)")
            continue
        med, p25, p5 = st.median(xs), _pct(xs, 25), _pct(xs, 5)
        breach = sum(1 for x in xs if x < 1 / TORIGAMI_MARGIN) / len(xs)
        cur = DRIFT_SHADE.get(bt)
        # 提案: 的中条件付き下振れの p25-median の中間を保守的シェードとする
        suggest = round(min(1.0, (med + p25) / 2), 2)
        out["drift"][bt] = dict(n=len(xs), median=round(med, 3), p25=round(p25, 3),
                                p5=round(p5, 3), breach_pct=round(breach * 100, 1),
                                current_shade=cur, suggested_shade=suggest)
        print(f"  {bt:10s} n={len(xs):4d} median={med:.3f} p25={p25:.3f} p5={p5:.3f} "
              f"margin突破={breach*100:.0f}% 現行shade={cur} → 提案={suggest}")

    emit("単勝 top-1 較正 bin (モデル+市場ブレンド済 確率帯ごとの実勝率)")
    out["win_calibration"] = {}
    for bink, wb in sorted(win_bins.items()):
        pred = wb["pred"] / wb["n"] * 100 if wb["n"] else 0
        real = wb["hits"] / wb["n"] * 100 if wb["n"] else 0
        out["win_calibration"][bink] = dict(n=wb["n"], pred_pct=round(pred, 1),
                                            real_pct=round(real, 1))
        print(f"  {bink:8s} n={wb['n']:4d} 予測 {pred:5.1f}% vs 実勝率 {real:5.1f}%")

    if args.json:
        print("\n" + json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
