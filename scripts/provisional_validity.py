#!/usr/bin/env python3
"""仮指数 (src/provisional.py) の因子別 予測妥当性を実結果でバックテストし、重みを較正する。

ユーザ指示 2026-07-01 (選択肢b→①): 「重み・因子の実データチューニング」を **セグメント別
(NAR / JRA / banei)** に行う。data/raw の出馬表+馬柱 HTML から RaceData を再構築 →
provisional_breakdown で各馬の因子スコアを出し、data/datasets/all.parquet の finish_pos
(実着順) と (race_id, horse_number) で突合。venue コード (rid[4:6]) で jra/nar/banei に分ける。

セグメント毎に出力:
  ① 因子別 AUC (factor_score vs 1着 / 複勝圏)。0.5=ランダム、高いほど 1着を当てる予測力。
  ② 仮指数(現行重み) の top1/top3 的中率 と AUC を field-size ベースラインと比較。
  ③ 現行重み × セグメントAUC比例 の 50/50 ブレンド **推奨重み** (paste 可) と、その in-sample top1。

読み取り専用・scrape 不要。CLAUDE.md の overfit 戒めに従い in-sample 較正を鵜呑みにしない。

    PYTHONPATH=. .venv/bin/python scripts/provisional_validity.py [--sample 800] [--segments nar,jra,banei]
"""
from __future__ import annotations

import argparse
import gzip
from pathlib import Path

import pandas as pd

from src import provisional as P
from src.parse import parse_past_runs, parse_shutuba

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PARQUET = ROOT / "data" / "datasets" / "all.parquet"

JRA_VENUES = {f"{i:02d}" for i in range(1, 11)}


def _seg_of_rid(rid: str) -> str:
    v = rid[4:6]
    if v == "65":
        return "banei"
    if v in JRA_VENUES:
        return "jra"
    return "nar"


def _auc(scores: list[float], labels: list[int]) -> float:
    """rank-based AUC (Mann-Whitney)。labels は 0/1。片側しか無ければ nan。"""
    n = len(scores)
    if n == 0:
        return float("nan")
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and scores[order[j]] == scores[order[i]]:
            j += 1
        avg = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[order[k]] = avg
        i = j
    pos = sum(labels)
    neg = n - pos
    if pos == 0 or neg == 0:
        return float("nan")
    sum_ranks_pos = sum(r for r, l in zip(ranks, labels) if l == 1)
    return (sum_ranks_pos - pos * (pos + 1) / 2.0) / (pos * neg)


def _reconstruct(rid: str):
    sh = RAW / f"{rid}-shutuba.html.gz"
    pp = RAW / f"{rid}-past.html.gz"
    if not sh.exists() or not pp.exists():
        return None
    try:
        rd = parse_shutuba(gzip.open(sh, "rt", encoding="utf-8").read(), race_id=rid)
        runs = parse_past_runs(gzip.open(pp, "rt", encoding="utf-8").read())
        for h in rd.race.horses:
            h.past_runs = runs.get(h.number, [])
        return rd
    except Exception:  # noqa: BLE001
        return None


def _report_segment(seg: str, pool, prov_pool, race_cache, top1_hit, top3_cov, n_races):
    factors = list(P._FACTORS.keys())
    segw = P._SEGMENT_WEIGHTS.get(seg, P.WEIGHTS)   # セグメントの現行重み
    print(f"\n{'='*66}\n=== セグメント: {seg.upper()}  (評価 {n_races} レース) ===")
    if n_races == 0:
        print("  評価可能レース無し")
        return None
    print("① 因子別 AUC (1着を当てる予測力・0.5=ランダム)")
    print(f"   {'因子':<20} {'現重み':>6} {'AUC(1着)':>9} {'AUC(複勝)':>9} {'n':>7}")
    auc_win: dict[str, float] = {}
    for f in factors:
        sc = [x[0] for x in pool[f]]
        aw = _auc(sc, [x[1] for x in pool[f]])
        a3 = _auc(sc, [x[2] for x in pool[f]])
        auc_win[f] = aw if aw == aw else 0.5
        print(f"   {f:<20} {segw[f]:>6.2f} {aw:>9.3f} {a3:>9.3f} {len(sc):>7}")

    pw = _auc([x[0] for x in prov_pool], [x[1] for x in prov_pool])
    pw3 = _auc([x[0] for x in prov_pool], [x[2] for x in prov_pool])
    base = sum(1.0 / fs for _bd, _f, fs in race_cache) / n_races
    print(f"② 仮指数(現行重み): AUC(1着)={pw:.3f} AUC(複勝)={pw3:.3f}  "
          f"top1={100*top1_hit/n_races:.1f}% (rand {100*base:.1f}%)  "
          f"top3被覆={100*top3_cov/n_races:.1f}%")

    # ③ 現行重み × セグメントAUC比例 の 50/50 ブレンド
    edge = {f: max(0.0, auc_win[f] - 0.5) for f in factors}
    tot = sum(edge.values()) or 1.0
    aucw = {f: edge[f] / tot for f in factors}
    blend = {f: 0.5 * segw[f] + 0.5 * aucw[f] for f in factors}
    bt = sum(blend.values()) or 1.0
    blend = {f: round(blend[f] / bt, 3) for f in factors}
    # 丸め誤差を最大重み因子に寄せて合計 1.000 に
    drift = round(1.0 - sum(blend.values()), 3)
    if abs(drift) >= 0.001:
        fmax = max(blend, key=blend.get)
        blend[fmax] = round(blend[fmax] + drift, 3)

    # in-sample: ブレンド重みでの top1 的中
    rew_hit = 0
    for bd, finishes, _fs in race_cache:
        winner = next(n for n, fp in finishes.items() if fp == 1)
        best_num, best_val = None, -1.0
        for num, fac in bd.items():
            num2 = den2 = 0.0
            for f in factors:
                s = fac.get(f)
                if s is not None:
                    num2 += blend[f] * s
                    den2 += blend[f]
            v = num2 / den2 if den2 > 0 else 50.0
            if v > best_val:
                best_val, best_num = v, num
        if best_num == winner:
            rew_hit += 1
    print(f"③ 推奨重み ({seg}, 現行×AUC比例 50/50・in-sample top1 "
          f"{100*rew_hit/n_races:.1f}% vs 現行 {100*top1_hit/n_races:.1f}%):")
    print(f'   WEIGHTS_{seg.upper()} = {{')
    for f in factors:
        print(f'       "{f}": {blend[f]:.3f},   # AUC {auc_win[f]:.3f}')
    print("   }")
    return blend


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=800, help="各セグメントの評価レース数上限 (新しい順)")
    ap.add_argument("--segments", type=str, default="nar,jra,banei")
    ap.add_argument("--min-horses", type=int, default=6)
    args = ap.parse_args()
    segs = [s.strip() for s in args.segments.split(",") if s.strip()]

    df = pd.read_parquet(PARQUET, columns=["race_id", "horse_number", "finish_pos", "race_date"])
    df["race_id"] = df["race_id"].astype(str)
    fin = {(r.race_id, int(r.horse_number)): (int(r.finish_pos) if pd.notna(r.finish_pos) else None)
           for r in df.itertuples()}
    rid_date = (df.groupby("race_id")["race_date"].max().reset_index()
                .sort_values("race_date", ascending=False))
    all_rids = [r for r in rid_date["race_id"].tolist()
                if (RAW / f"{r}-past.html.gz").exists()]

    factors = list(P.WEIGHTS.keys())
    # セグメント毎の accumulator
    acc = {s: dict(pool={f: [] for f in factors}, prov=[], race_cache=[],
                   top1=0, top3=0, n=0) for s in segs}

    for rid in all_rids:
        seg = _seg_of_rid(rid)
        if seg not in acc or acc[seg]["n"] >= args.sample:
            continue
        rd = _reconstruct(rid)
        if rd is None:
            continue
        horses = [h for h in rd.race.horses if not h.absent]
        if len(horses) < args.min_horses:
            continue
        finishes = {h.number: fin.get((rid, h.number)) for h in horses}
        if sum(1 for v in finishes.values() if v == 1) != 1:
            continue
        bd = P.provisional_breakdown(rd)
        prov = P.provisional_index(rd)
        if not prov:
            continue
        a = acc[seg]
        a["n"] += 1
        for h in horses:
            fp = finishes[h.number]
            win = 1 if fp == 1 else 0
            top3 = 1 if fp in (1, 2, 3) else 0
            for f in factors:
                s = bd[h.number].get(f)
                if s is not None:
                    a["pool"][f].append((s, win, top3))
            a["prov"].append((prov[h.number], win, top3))
        winner = next(n for n, fp in finishes.items() if fp == 1)
        ranked = sorted(prov.items(), key=lambda kv: -kv[1])
        if max(prov, key=prov.get) == winner:
            a["top1"] += 1
        if winner in {n for n, _ in ranked[:3]}:
            a["top3"] += 1
        a["race_cache"].append((bd, finishes, len(horses)))

    print(f"仮指数 セグメント別 予測妥当性 (data/raw cache + all.parquet 突合)")
    out = {}
    for s in segs:
        a = acc[s]
        out[s] = _report_segment(s, a["pool"], a["prov"], a["race_cache"],
                                  a["top1"], a["top3"], a["n"])
    print("\n※ in-sample 較正。採用は方向性の裏付けとして。標本の薄いセグメント(banei等)は特に慎重に。")


if __name__ == "__main__":
    main()
