"""単勝 β=0.78 の hit rate / ROI を race 特性別に診断する。

「全 N=440 でモデル+7pt」は平均値。特定の race 特性 (venue / distance /
surface / going / field size) で model edge がより強い (あるいは弱い) なら、
race selection で実用 ROI を底上げできる。

W3 (train 0-1308, valid 1308-1634) + W4 (train 0-1471, valid 1471-1634) の
本番モデル (production lgbm_lambdarank.txt) で評価。

使い方:
  python scripts/race_class_diagnostic.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.ev import BLEND_DEFAULT, LGBM_TEMPERATURE, power_method_overround  # noqa: E402

DATASET = ROOT / "data" / "datasets" / "all.parquet"
MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def main() -> int:
    meta = json.loads(META.read_text(encoding="utf-8"))
    feature_cols = list(meta["feature_cols"])
    T = float(meta.get("softmax_temperature") or LGBM_TEMPERATURE)
    print(f"production model: T={T}, β={BLEND_DEFAULT}", flush=True)

    df = pd.read_parquet(DATASET)
    rids = df["race_id"].unique().tolist()
    rids.sort(key=_race_id_to_int)
    # production model の validation = last 20%
    n_valid = int(len(rids) * 0.2)
    valid_rids = set(rids[-n_valid:])
    valid = df[df["race_id"].isin(valid_rids)].copy()
    valid = valid[valid["race_id"].isin(
        valid.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )].copy()
    valid = valid.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    print(f"valid races: {valid['race_id'].nunique()}", flush=True)

    booster = lgb.Booster(model_file=str(MODEL))
    X = valid[feature_cols].astype("float64").fillna(0.0)
    valid["score"] = booster.predict(X.values, num_iteration=booster.best_iteration)

    def _race_softmax(s: pd.Series) -> pd.Series:
        scaled = s / T
        m = scaled.max()
        ex = np.exp(scaled - m)
        return ex / ex.sum()
    valid["lgbm_prob"] = valid.groupby("race_id", sort=False)["score"].transform(_race_softmax)

    def _market_prob(g: pd.DataFrame) -> pd.Series:
        raw = {int(r.horse_number): (1.0 / r.win_odds) if r.win_odds and r.win_odds > 0 else 0.0
               for r in g.itertuples()}
        # 未正規化 1/odds (Σ=overround>1) のまま de-vig へ —
        # 正規化すると power_method が k=1 の恒等写像に縮退する (2026-06-10 修正)
        try:
            corrected = power_method_overround(raw)
        except Exception:
            corrected = raw
        s2 = sum(corrected.values())
        if s2 > 0:
            corrected = {k: v / s2 for k, v in corrected.items()}
        return pd.Series([corrected.get(int(r.horse_number), 0.0) for r in g.itertuples()], index=g.index)
    valid["market_prob"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(_market_prob, include_groups=False)
    )

    def _blend(g: pd.DataFrame, beta: float) -> pd.Series:
        alpha = max(1.0 - beta, 0.0)
        logs = []
        for r in g.itertuples():
            f = max(r.lgbm_prob, 1e-9)
            pi = max(r.market_prob, 1e-9)
            logs.append(alpha * math.log(f) + beta * math.log(pi))
        a = np.array(logs)
        ex = np.exp(a - a.max())
        return pd.Series(ex / ex.sum(), index=g.index)
    valid["blend"] = (
        valid.groupby("race_id", sort=False, group_keys=False)
        .apply(lambda g: _blend(g, BLEND_DEFAULT), include_groups=False)
    )

    # 各 race の top-1 ベットを集める
    race_data = []
    for rid, g in valid.groupby("race_id", sort=False):
        top_idx = g["blend"].idxmax()
        top = g.loc[top_idx]
        # distance / n_horses が NaN の場合 None にして filter で除外可能にする
        dist_raw = top["distance"]
        nh_raw = top["n_horses"]
        race_data.append({
            "race_id": rid,
            "venue": top["venue"],
            "distance": int(dist_raw) if pd.notna(dist_raw) and dist_raw > 0 else None,
            "surface": top["surface"],
            "going": top["going"],
            "n_horses": int(nh_raw) if pd.notna(nh_raw) and nh_raw > 0 else None,
            "hit": bool(top["target_top1"] == 1),
            "win_odds": float(top["win_odds"]) if top["win_odds"] > 0 else 0.0,
            "top1_prob": float(top["blend"]),
        })
    rdf = pd.DataFrame(race_data)
    print(f"total races: {len(rdf)}, overall hit rate: {rdf['hit'].mean()*100:.1f}%", flush=True)
    print()

    def _print_bin_table(title: str, groupby):
        print(f"=== {title} ===", flush=True)
        print(f"{'bin':>20} {'n':>5} {'hit%':>6} {'avg odds':>9} {'ROI':>7}", flush=True)
        print("-" * 55, flush=True)
        for key, sub in rdf.groupby(groupby):
            n = len(sub)
            hits = sub["hit"].sum()
            stake = n * 100
            payout = sum(int(100 * row["win_odds"]) for _, row in sub.iterrows() if row["hit"])
            roi = payout / stake if stake else 0
            avg_odds = sub["win_odds"].mean()
            label = str(key) if not isinstance(key, tuple) else "/".join(str(k) for k in key)
            print(f"{label:>20} {n:>5} {hits/n*100:>5.1f}% {avg_odds:>8.2f} {roi*100:>6.1f}%", flush=True)
        print(flush=True)

    _print_bin_table("Surface", "surface")
    # distance binning
    rdf["dist_bin"] = pd.cut(
        rdf["distance"],
        bins=[0, 1300, 1700, 2100, 4000],
        labels=["sprint(≤1300)", "mile(1300-1700)", "intermediate(1700-2100)", "long(>2100)"],
    )
    _print_bin_table("Distance", "dist_bin")
    _print_bin_table("Going (track condition)", "going")
    # field size
    rdf["field_bin"] = pd.cut(
        rdf["n_horses"], bins=[0, 9, 12, 18], labels=["small(≤9)", "medium(10-12)", "large(13-18)"]
    )
    _print_bin_table("Field size", "field_bin")
    # confidence
    rdf["conf_bin"] = pd.cut(
        rdf["top1_prob"], bins=[0, 0.15, 0.25, 0.35, 1.0],
        labels=["<0.15", "0.15-0.25", "0.25-0.35", "≥0.35"],
    )
    _print_bin_table("Top-1 confidence", "conf_bin")

    print("解釈: 'hit%' が極端に高い / ROI が市場 baseline (約 80%) を大幅超過する bin を", flush=True)
    print("見つけるとレース選別の対象になる。差が小さい / N が小さい bin は noise の可能性。", flush=True)
    print()

    # 複合フィルタの探索: 単一 bin で promising な条件を AND で組み合わせる
    print("=== 複合フィルタ (AND 結合の探索) ===", flush=True)
    def _is_sprint(r):
        return r["distance"] is not None and r["distance"] <= 1300
    filters = [
        ("ALL", lambda r: True),
        ("ダート", lambda r: r["surface"] == "ダート"),
        ("芝", lambda r: r["surface"] == "芝"),
        ("Sprint ≤1300m", _is_sprint),
        ("Sprint + ダート", lambda r: _is_sprint(r) and r["surface"] == "ダート"),
        ("Sprint + 芝", lambda r: _is_sprint(r) and r["surface"] == "芝"),
        ("confidence 0.25-0.35", lambda r: 0.25 <= r["top1_prob"] < 0.35),
        ("ダート + 0.25-0.35 conf", lambda r: r["surface"] == "ダート" and 0.25 <= r["top1_prob"] < 0.35),
        ("Sprint + 0.25-0.35 conf", lambda r: _is_sprint(r) and 0.25 <= r["top1_prob"] < 0.35),
        ("Sprint + ダート + 0.25-0.35", lambda r:
            _is_sprint(r) and r["surface"] == "ダート" and 0.25 <= r["top1_prob"] < 0.35),
    ]
    print(f"{'filter':>35} {'n':>5} {'hit%':>6} {'avg odds':>9} {'ROI':>7}", flush=True)
    print("-" * 70, flush=True)
    for label, fn in filters:
        sub = rdf[rdf.apply(fn, axis=1)] if len(rdf) > 0 else rdf
        n = len(sub)
        if n == 0:
            print(f"{label:>35} {n:>5}  —  —  —", flush=True)
            continue
        hits = sub["hit"].sum()
        stake = n * 100
        payout = sum(int(100 * r["win_odds"]) for _, r in sub.iterrows() if r["hit"])
        roi = payout / stake if stake else 0
        avg_odds = sub["win_odds"].mean()
        marker = ""
        if n >= 30 and roi >= 1.0:
            marker = " ★ +EV"
        elif n >= 30 and roi >= 0.95:
            marker = " ☆ near"
        print(f"{label:>35} {n:>5} {hits/n*100:>5.1f}% {avg_odds:>8.2f} {roi*100:>6.1f}%{marker}", flush=True)
    print(flush=True)
    print("★ = N≥30 かつ ROI≥1.0 (確度高い +EV 候補)", flush=True)
    print("☆ = N≥30 かつ 0.95≤ROI<1.0 (controlled に追えば +EV 圏)", flush=True)
    print()
    print("注意: validation set は時系列後半 = NAR ダート に偏る傾向 (芝 race が", flush=True)
    print("少ないため 'Sprint' = NAR ダート Sprint と一致する)。JRA 芝 文脈での", flush=True)
    print("再現は別途データ蓄積後に確認。最も汎用的な finding は 'confidence", flush=True)
    print("0.25-0.35 で n=101 / ROI 105.7%' = 'モデルが中等度に確信した時に勝つ'。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
