#!/usr/bin/env python3
"""late-money momentum (score→bet の単勝オッズ変化) のオフライン検証。

CLAUDE.md の新規 edge 候補①: 「score→bet のオッズ変化は informed money の痕跡」
(arXiv:2509.14645 は締切直前のオッズ変化とリターンの相関を実証) を、手元の
odds_timeline + results で検証する。読み取り専用 (既存データ/コードを変更しない)。

データ源:
- data/cache/odds_timeline/<race_id>.jsonl : score/bet 段の単勝・複勝オッズ
  (基準 = 最初の score 行、比較 = 最後の bet 行。複数 bet 行は最終を採用)
- data/results/<race_id>.json              : finish_order + final_odds
  (win:<勝ち馬> は全レースに存在 → 勝ち馬払戻は確定値、非勝ち馬は bet 段オッズで代用)

per-horse の momentum 比 r = win_bet / win_score (r<1 = 直前に売れた)。
 (a) レース内 r 五分位 → 勝率 / 3着内率 / flat ¥100 単勝 ROI
 (b) score オッズ帯 (fav<5 / mid5-15 / long>15) × 方向 (短縮<0.95 / ±5% / ドリフト>1.05)
     — favorite-longshot bias の統制。±5% は経路混在 (netkeiba→keibago 等) のノイズ帯
 (c) セグメント別 (NAR平地 / ばんえい=場65 / JRA) × 方向
 (d) 「レース内で最も売れた馬」が bet 段 de-vig 含意確率より勝つかの z 検定
     (Poisson-binomial 正規近似: z = (W−Σp) / √Σp(1−p))。最ドリフト馬を対照に併記
 (e) 複勝オッズの momentum (頭数ルール: ≤7頭は2着まで / ≤4頭は発売なし)

使い方:
    .venv/bin/python scripts/backtest_momentum.py
    .venv/bin/python scripts/backtest_momentum.py --since 20260601 --json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.odds_timeline import TIMELINE_DIR, read_rows  # noqa: E402

RES_DIR = ROOT / "data" / "results"
JRA_CODES = {f"{i:02d}" for i in range(1, 11)}

# 方向の閾値: 経路混在 (score=netkeiba / bet=keibago 等) でも単勝は比較可能だが
# <5% の変動はノイズ扱い (CLAUDE.md odds_timeline 節)。
SHORTEN_THR = 0.95
DRIFT_THR = 1.05


def segment(rid: str) -> str:
    """normalized race_id → セグメント。JRA cup=YYYY|VV|KK|DD (8桁, VV=01-10)、
    NAR cup=YYYY|VV|MMDD (10桁)。場65=帯広ばんえい (別競技なので分離)。"""
    cup = rid.split("-")[0]
    vv = cup[4:6] if len(cup) >= 6 else ""
    if len(cup) == 8 and vv in JRA_CODES:
        return "jra"
    if vv == "65":
        return "banei"
    return "nar"


def direction(r: float) -> str:
    if r < SHORTEN_THR:
        return "shortened"
    if r > DRIFT_THR:
        return "drifted"
    return "stable"


def new_agg() -> dict:
    return {"n": 0, "wins": 0, "top3": 0, "stake": 0, "ret": 0.0, "rs": []}


def acc(a: dict, r: float, hit_win: bool, hit_top3: bool, payout: float) -> None:
    a["n"] += 1
    a["wins"] += int(hit_win)
    a["top3"] += int(hit_top3)
    a["stake"] += 100
    a["ret"] += payout
    a["rs"].append(r)


def med(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def fmt_agg(a: dict) -> str:
    n = a["n"]
    if not n:
        return "(n=0)"
    roi = a["ret"] / a["stake"] * 100 if a["stake"] else 0.0
    warn = "" if n >= 30 else " ⚠n<30"
    return (f"n={n:5d}  win={a['wins'] / n:6.1%}  top3={a['top3'] / n:6.1%}  "
            f"flat単勝ROI={roi:6.1f}%  med_r={med(a['rs']):.3f}{warn}")


def agg_json(a: dict) -> dict:
    n = a["n"]
    return {
        "n": n,
        "win_rate": round(a["wins"] / n, 4) if n else None,
        "top3_rate": round(a["top3"] / n, 4) if n else None,
        "flat_win_roi": round(a["ret"] / a["stake"], 4) if a["stake"] else None,
        "median_r": round(med(a["rs"]), 4) if a["rs"] else None,
        "small_sample": n < 30,
    }


def load_races(since: str | None) -> list[tuple[str, dict, dict, dict]]:
    """(rid, score行, bet行, result) — score+bet 両 stage + 結果3着あり のレースのみ。"""
    races = []
    for f in sorted(glob.glob(str(TIMELINE_DIR / "*.jsonl"))):
        rid = os.path.basename(f)[:-6]
        rf = RES_DIR / f"{rid}.json"
        if not rf.exists():
            continue
        try:
            res = json.load(open(rf, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if len(res.get("finish_order") or []) < 3:
            continue
        rows = read_rows(rid)
        scores = [r for r in rows if r.get("stage") == "score"]
        bets = [r for r in rows if r.get("stage") == "bet"]
        if not scores or not bets:
            continue
        sc, bt = scores[0], bets[-1]   # 最も古い score を基準・最後の bet を比較
        # 日付フィルタは bet 行の captured_at (predictions の saved_at と同日)。
        if since and (bt.get("captured_at") or "")[:10].replace("-", "") < since:
            continue
        races.append((rid, sc, bt, res))
    return races


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default=None, help="YYYYMMDD (bet 行の captured_at で filter)")
    ap.add_argument("--json", action="store_true", help="集計を JSON で stdout に出す")
    args = ap.parse_args()

    races = load_races(args.since)

    quint = defaultdict(new_agg)                  # (a) q0(最短縮)..q4(最ドリフト)
    band_dir = defaultdict(new_agg)               # (b) (band, direction)
    seg_dir = defaultdict(new_agg)                # (c) (segment, direction)
    seg_quint0 = defaultdict(new_agg)             # (c 補助) segment × 最短縮五分位
    zdata = {"most_shortened": [], "most_drifted": []}   # (d) (p_implied, win?, payout)
    pl_quint = defaultdict(lambda: {"n": 0, "hits": 0, "stake": 0, "ret": 0.0, "rs": []})  # (e)
    n_used = n_mix = 0
    gaps: list[float] = []
    seg_count: dict[str, int] = defaultdict(int)

    for rid, sc, bt, res in races:
        w0 = (sc.get("odds") or {}).get("win") or {}
        w1 = (bt.get("odds") or {}).get("win") or {}
        common = [h for h in w1 if w0.get(h, 0) > 0 and w1.get(h, 0) > 0]
        if len(common) < 5:
            continue
        n_used += 1
        seg = segment(rid)
        seg_count[seg] += 1
        if set((sc.get("odds") or {}).keys()) != set((bt.get("odds") or {}).keys()):
            n_mix += 1   # 経路混在 (netkeiba score → keibago/jra bet 等)
        try:
            import datetime as dt
            gaps.append((dt.datetime.fromisoformat(bt["captured_at"])
                         - dt.datetime.fromisoformat(sc["captured_at"])
                         ).total_seconds() / 60.0)
        except Exception:  # noqa: BLE001
            pass
        fo = res["finish_order"]
        winner = str(fo[0])
        top3 = {str(x) for x in fo[:3]}
        final = res.get("final_odds") or {}
        r = {h: w1[h] / w0[h] for h in common}
        order = sorted(common, key=lambda h: (r[h], w1[h]))   # 同率は人気側を先に
        n = len(order)
        for i, h in enumerate(order):
            q = min(4, i * 5 // n)
            hit_win = (h == winner)
            hit_top3 = h in top3
            # 勝ち馬払戻は final_odds の確定値 (85/85 で存在)、無ければ bet 段オッズで代用。
            payout = (final.get(f"win:{h}") or w1[h]) * 100 if hit_win else 0.0
            band = "fav<5" if w0[h] < 5 else ("mid5-15" if w0[h] <= 15 else "long>15")
            g = direction(r[h])
            acc(quint[q], r[h], hit_win, hit_top3, payout)
            acc(band_dir[(band, g)], r[h], hit_win, hit_top3, payout)
            acc(seg_dir[(seg, g)], r[h], hit_win, hit_top3, payout)
            if q == 0:
                acc(seg_quint0[seg], r[h], hit_win, hit_top3, payout)
        # (d) 最短縮馬 / 最ドリフト馬 (対照): bet 段 de-vig 含意勝率と比較。
        inv = {h: 1.0 / w1[h] for h in w1 if w1[h] > 0}
        s_inv = sum(inv.values())
        for key, h in (("most_shortened", order[0]), ("most_drifted", order[-1])):
            p = inv.get(h, 0.0) / s_inv if s_inv > 0 else 0.0
            payout = (final.get(f"win:{h}") or w1[h]) * 100 if h == winner else 0.0
            zdata[key].append((p, h == winner, payout))
        # (e) 複勝 momentum (両 stage に複勝がある場合のみ)。
        p0 = (sc.get("odds") or {}).get("place") or {}
        p1 = (bt.get("odds") or {}).get("place") or {}
        common_p = [h for h in p1 if p0.get(h, 0) > 0 and p1.get(h, 0) > 0]
        n_run = bt.get("n_horses") or len(w1)
        if len(common_p) >= 5 and n_run > 4:   # ≤4頭は複勝発売なし
            paying = {str(x) for x in fo[:2]} if n_run <= 7 else top3
            rp = {h: p1[h] / p0[h] for h in common_p}
            order_p = sorted(common_p, key=lambda h: (rp[h], p1[h]))
            np_ = len(order_p)
            for i, h in enumerate(order_p):
                q = min(4, i * 5 // np_)
                a = pl_quint[q]
                a["n"] += 1
                a["stake"] += 100
                a["rs"].append(rp[h])
                if h in paying:
                    a["hits"] += 1
                    # 複勝確定値が final_odds に無い馬は bet 段の下限オッズで代用 (保守的)。
                    a["ret"] += (final.get(f"place:{h}") or p1[h]) * 100

    # ---------------- 出力 ----------------
    out: dict = {
        "n_races": n_used,
        "segments": dict(seg_count),
        "source_mix_races": n_mix,
        "median_gap_min": round(med(gaps), 1) if gaps else None,
        "thresholds": {"shorten": SHORTEN_THR, "drift": DRIFT_THR},
    }

    out["a_quintiles"] = {f"q{q}": agg_json(quint[q]) for q in sorted(quint)}
    out["b_band_direction"] = {
        f"{band}|{g}": agg_json(a) for (band, g), a in sorted(band_dir.items())}
    out["c_segment_direction"] = {
        f"{seg}|{g}": agg_json(a) for (seg, g), a in sorted(seg_dir.items())}
    out["c_segment_q0"] = {seg: agg_json(a) for seg, a in sorted(seg_quint0.items())}

    ztests = {}
    for key, data in zdata.items():
        n = len(data)
        W = sum(1 for _, w, _ in data if w)
        E = sum(p for p, _, _ in data)
        var = sum(p * (1 - p) for p, _, _ in data)
        z = (W - E) / math.sqrt(var) if var > 0 else float("nan")
        stake = n * 100
        ret = sum(pay for _, _, pay in data)
        ztests[key] = {
            "n": n, "wins": W, "expected_wins": round(E, 2),
            "z": round(z, 2) if not math.isnan(z) else None,
            "flat_win_roi": round(ret / stake, 4) if stake else None,
            "small_sample": n < 30,
        }
    out["d_ztest"] = ztests

    out["e_place_quintiles"] = {}
    for q in sorted(pl_quint):
        a = pl_quint[q]
        out["e_place_quintiles"][f"q{q}"] = {
            "n": a["n"],
            "place_hit_rate": round(a["hits"] / a["n"], 4) if a["n"] else None,
            "flat_place_roi": round(a["ret"] / a["stake"], 4) if a["stake"] else None,
            "median_r": round(med(a["rs"]), 4) if a["rs"] else None,
            "small_sample": a["n"] < 30,
        }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f"対象レース: {n_used} (score+bet 両 stage + 結果あり, 共通馬≥5)"
          + (f" / since={args.since}" if args.since else ""))
    print(f"  セグメント: {dict(seg_count)} / 経路混在 (券種集合が score≠bet): {n_mix}")
    print(f"  score→bet 中央値 gap: {out['median_gap_min']} 分 / "
          f"方向閾値: 短縮<{SHORTEN_THR} / ドリフト>{DRIFT_THR} (±5%=ノイズ帯)")

    print("\n(a) レース内 r=bet/score 五分位 (q0=最も売れた … q4=最もドリフト)")
    for q in sorted(quint):
        print(f"  q{q}: {fmt_agg(quint[q])}")

    print("\n(b) score オッズ帯 × 方向 (favorite-longshot bias の統制)")
    for band in ("fav<5", "mid5-15", "long>15"):
        for g in ("shortened", "stable", "drifted"):
            a = band_dir.get((band, g))
            if a:
                print(f"  {band:8s} {g:10s}: {fmt_agg(a)}")

    print("\n(c) セグメント × 方向 (banei=帯広ばんえい 場65)")
    for seg in ("nar", "banei", "jra"):
        for g in ("shortened", "stable", "drifted"):
            a = seg_dir.get((seg, g))
            if a:
                print(f"  {seg:6s} {g:10s}: {fmt_agg(a)}")
    print("  -- 最短縮五分位 (q0) のセグメント別 --")
    for seg in ("nar", "banei", "jra"):
        if seg in seg_quint0:
            print(f"  {seg:6s} q0       : {fmt_agg(seg_quint0[seg])}")

    print("\n(d) z 検定: レース内最短縮馬は bet 段 de-vig 含意勝率より勝つか (対照=最ドリフト馬)")
    for key, zt in ztests.items():
        z_s = f"{zt['z']:+.2f}" if zt["z"] is not None else "nan"
        warn = " ⚠n<30" if zt["small_sample"] else ""
        print(f"  {key:15s}: n={zt['n']} wins={zt['wins']} (期待 {zt['expected_wins']}) "
              f"z={z_s} flat単勝ROI={zt['flat_win_roi'] * 100:.1f}%{warn}")

    print("\n(e) 複勝オッズ momentum 五分位 (頭数ルール適用: ≤7頭=2着まで, ≤4頭=発売なし)")
    for q, a in sorted(out["e_place_quintiles"].items()):
        if not a["n"]:
            continue
        warn = " ⚠n<30" if a["small_sample"] else ""
        print(f"  {q}: n={a['n']:5d}  hit={a['place_hit_rate']:6.1%}  "
              f"flat複勝ROI={a['flat_place_roi'] * 100:6.1f}%  med_r={a['median_r']:.3f}{warn}")


if __name__ == "__main__":
    main()
