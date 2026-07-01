#!/usr/bin/env python3
"""仮指数 (src/provisional.py) の因子別 予測妥当性を実結果でバックテストし、重みを較正する。

ユーザ指示 2026-07-01 (選択肢b): 「重み・因子の実データチューニング」。
data/raw の出馬表+馬柱 HTML から RaceData を再構築 → provisional_breakdown で各馬の因子スコアを
出し、data/datasets/all.parquet の finish_pos (実着順) と (race_id, horse_number) で突合。

出力:
  ① 因子別 AUC (factor_score vs 1着 / 複勝圏)。0.5=ランダム、高いほど 1着を当てる予測力。
  ② 仮指数(現行重み) の top1/top3 的中率 と AUC を field-size ベースラインと比較。
  ③ AUC(−0.5) 比例の **推奨重み** と、それで組み直した仮指数の的中率 (in-sample 参考)。

読み取り専用・scrape 不要 (data/raw の cache + parquet のみ)。CLAUDE.md の overfit 戒めに従い、
結論は「方向性」であり in-sample の再重み付けを鵜呑みにしない (要 out-of-sample 再検証)。

    PYTHONPATH=. .venv/bin/python scripts/provisional_validity.py [--sample 800] [--seed 0]
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


def _auc(scores: list[float], labels: list[int]) -> float:
    """rank-based AUC (Mann-Whitney)。labels は 0/1。片側しか無ければ nan。"""
    n = len(scores)
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and scores[order[j]] == scores[order[i]]:
            j += 1
        avg = (i + j - 1) / 2.0 + 1.0    # 1-indexed 平均順位 (タイは平均)
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
    """cached 出馬表+馬柱 HTML から RaceData を再構築 (past_runs 込)。失敗は None。"""
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=800, help="評価レース数 (時系列の新しい方から)")
    ap.add_argument("--min-horses", type=int, default=7)
    args = ap.parse_args()

    df = pd.read_parquet(PARQUET, columns=["race_id", "horse_number", "finish_pos", "race_date"])
    df["race_id"] = df["race_id"].astype(str)
    # 実着順ルックアップ: (rid, number) -> finish_pos
    fin = {(r.race_id, int(r.horse_number)): (int(r.finish_pos) if pd.notna(r.finish_pos) else None)
           for r in df.itertuples()}
    # 評価対象 rid: race_date の新しい順 (pseudo-holdout)。cache のあるものだけ。
    rid_date = (df.groupby("race_id")["race_date"].max().reset_index()
                .sort_values("race_date", ascending=False))
    rids = [r for r in rid_date["race_id"].tolist()
            if (RAW / f"{r}-past.html.gz").exists()][: args.sample * 3]

    factors = list(P.WEIGHTS.keys())
    # プール: factor -> [(score, is_win, is_top3)]
    pool: dict[str, list[tuple[float, int, int]]] = {f: [] for f in factors}
    prov_pool: list[tuple[float, int, int]] = []       # (仮指数, win, top3)
    top1_hit = top3_cov = n_races = 0
    # 再重み付け実験用に per-race の (breakdown, finish) を保持
    race_cache: list[tuple[dict, dict, int]] = []       # (breakdown, {num:finish}, field_size)

    used = 0
    for rid in rids:
        if used >= args.sample:
            break
        rd = _reconstruct(rid)
        if rd is None:
            continue
        horses = [h for h in rd.race.horses if not h.absent]
        if len(horses) < args.min_horses:
            continue
        finishes = {h.number: fin.get((rid, h.number)) for h in horses}
        if sum(1 for v in finishes.values() if v == 1) != 1:
            continue                                    # 勝ち馬が特定できないレースは除外
        bd = P.provisional_breakdown(rd)
        prov = P.provisional_index(rd)
        if not prov:
            continue
        used += 1
        n_races += 1
        for h in horses:
            fp = finishes[h.number]
            win = 1 if fp == 1 else 0
            top3 = 1 if (fp in (1, 2, 3)) else 0
            for f in factors:
                s = bd[h.number].get(f)
                if s is not None:
                    pool[f].append((s, win, top3))
            prov_pool.append((prov[h.number], win, top3))
        # top1/top3 判定
        winner = next(n for n, fp in finishes.items() if fp == 1)
        ranked = sorted(prov.items(), key=lambda kv: -kv[1])
        if ranked and ranked[0][0] == prov[winner]:
            # 同値タイの曖昧さを避け「winner が仮指数最大値と一致」で判定
            pass
        top_num = max(prov, key=prov.get)
        if top_num == winner:
            top1_hit += 1
        if winner in {n for n, _ in ranked[:3]}:
            top3_cov += 1
        race_cache.append((bd, finishes, len(horses)))

    if n_races == 0:
        print("評価可能なレースが無い (cache/parquet 不足)")
        return

    print(f"=== 仮指数 予測妥当性バックテスト (評価 {n_races} レース) ===\n")
    print("① 因子別 AUC (1着を当てる予測力・0.5=ランダム・高いほど良い)")
    print(f"   {'因子':<20} {'重み':>6} {'AUC(1着)':>9} {'AUC(複勝)':>9} {'n':>7}")
    auc_win: dict[str, float] = {}
    for f in factors:
        sc = [x[0] for x in pool[f]]
        aw = _auc(sc, [x[1] for x in pool[f]])
        a3 = _auc(sc, [x[2] for x in pool[f]])
        auc_win[f] = aw if aw == aw else 0.5    # nan→0.5
        print(f"   {f:<20} {P.WEIGHTS[f]:>6.2f} {aw:>9.3f} {a3:>9.3f} {len(sc):>7}")

    pw_auc = _auc([x[0] for x in prov_pool], [x[1] for x in prov_pool])
    pw_auc3 = _auc([x[0] for x in prov_pool], [x[2] for x in prov_pool])
    base_top1 = sum(1.0 / fs for _bd, _f, fs in race_cache) / n_races
    print(f"\n② 仮指数(現行重み) 全体:")
    print(f"   AUC(1着)={pw_auc:.3f}  AUC(複勝)={pw_auc3:.3f}")
    print(f"   top1 的中率 = {top1_hit}/{n_races} = {100*top1_hit/n_races:.1f}%  "
          f"(ランダム基準 {100*base_top1:.1f}%)")
    print(f"   top3 に勝ち馬を含む率 = {top3_cov}/{n_races} = {100*top3_cov/n_races:.1f}%")

    # ③ AUC(−0.5) 比例の推奨重み + その重みでの top1 的中 (in-sample 参考)
    edge = {f: max(0.0, auc_win[f] - 0.5) for f in factors}
    tot = sum(edge.values()) or 1.0
    rew = {f: edge[f] / tot for f in factors}
    print("\n③ AUC由来の推奨重み (edge=AUC−0.5 に比例・in-sample 参考):")
    for f in factors:
        print(f"   {f:<20} 現 {P.WEIGHTS[f]:.2f} → 推奨 {rew[f]:.2f}")
    # 再重み付けでの top1 的中 (同じ per-factor score を新重みで合成)
    rew_hit = 0
    for bd, finishes, _fs in race_cache:
        winner = next(n for n, fp in finishes.items() if fp == 1)
        best_num, best_val = None, -1.0
        for num, fac in bd.items():
            num2 = 0.0
            den2 = 0.0
            for f in factors:
                s = fac.get(f)
                if s is not None:
                    num2 += rew[f] * s
                    den2 += rew[f]
            v = num2 / den2 if den2 > 0 else 50.0
            if v > best_val:
                best_val, best_num = v, num
        if best_num == winner:
            rew_hit += 1
    print(f"\n   推奨重みでの top1 的中率 (in-sample) = {rew_hit}/{n_races} = "
          f"{100*rew_hit/n_races:.1f}%  (現行 {100*top1_hit/n_races:.1f}%)")
    print("\n※ ③は in-sample。CLAUDE.md の overfit 戒めに従い、採用前に別期間/別セグメントで再検証。")


if __name__ == "__main__":
    main()
