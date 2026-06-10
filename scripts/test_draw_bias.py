"""NAR ダートの「枠順 (draw) / 隊列バイアス」が市場 (win オッズ) を上回る情報を持つか β-MLE で検証。

仮説: 小回り NAR (門別/金沢/笠松 等) は内枠・先行有利が強く、枠×場×距離の交互作用を
公衆が軽視する余地がある。ただし当プロジェクトでは速度/pace/位置別の特徴は全て β-MLE=1.0
(= 市場のコピーで超えられない) だったので、これも β=1.0 の可能性が高い。正直に測る。

設計 (test_position_prob.py / train_segment_models.py の β-MLE 書式を踏襲):
  - 時系列 3-fold: A=[0,60%) で fundamental lambdarank 学習、B=[60,80%) で β-MLE 推定
    (= conditional-logit 勝者 log-lik 最大化, train とは別 partition で 1 回だけ → overfit 回避)、
    C=[80,100%) は完全 hold-out で frozen β を評価 (単勝 OOS ROI / log-loss)。
  - baseline (26 feat) vs +draw (26 + 枠 feat) の β と OOS 指標を比較。
  - β < ~0.9 = モデルが市場を超える情報を持つ。β ~ 1.0 = 市場のコピー (edge 無し)。

枠データの所在: all.parquet に枠番(draw)列は **無い**。src.dataset.load_race(rid) の
  rd.race.horses[].bracket (枠番 1-8, parse.py が出馬表 cell0 から取得) にある。
  → load_race で (race_id, horse_number) -> bracket を引いて parquet に join する。

leakage 防止:
  - 枠別勝率の target encoding (venue×距離bucket×枠bucket -> P(win)) は **fold A のみ**で集計し、
    グローバル平均へ shrink してから B/C に lookup で適用 (B/C の正解は一切使わない)。
  - その他の draw 特徴 (生枠 / 枠÷n_horses / 馬番÷n_horses) は当該行のみから決まる静的量。

使い方: python scripts/test_draw_bias.py [nar|nar_small|all]
  nar       = 全 NAR ダート (既定)
  nar_small = 門別/金沢/笠松/水沢/高知/佐賀 等の小回りに限定 (仮説の核)
"""
from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dataset import load_race  # noqa: E402
from src.ev import power_method_overround  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
import json  # noqa: E402

META = json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())
FEATS = META["feature_cols"]  # 26 baseline feats
PARAMS = dict(META["params"])
JRA_CODES = {f"{i:02d}" for i in range(1, 11)}

# 小回り NAR (内枠・先行有利が言われる場)。仮説の核。
SMALL_VENUES = {"門別", "金沢", "笠松", "水沢", "高知", "佐賀", "盛岡", "姫路"}

# draw 特徴 (parquet に無い → 自前で作る列名)
DRAW_FEATS = [
    "bracket",            # 生枠番 (1..8)
    "bracket_rel",        # 枠 / 最大枠 (≒ 内外位置 0..1)
    "gate_rel",           # 馬番 / n_horses (ゲート位置 0..1)
    "is_inner_bracket",   # 枠 ∈ {1,2} (内枠フラグ)
    "draw_winrate_te",    # venue×距離bucket×枠bucket の枠別勝率 (fold A 集計, shrink)
]


def _race_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _dist_bucket(d: float) -> str:
    if d <= 1100:
        return "S"   # sprint
    if d <= 1500:
        return "M"   # mile-ish
    return "L"       # long


def _bracket_bucket(b: int) -> str:
    if b <= 2:
        return "in"
    if b <= 5:
        return "mid"
    return "out"


def _devig(odds: np.ndarray) -> np.ndarray:
    raw = 1.0 / np.asarray(odds, float)
    # 未正規化 1/odds のまま de-vig へ (正規化すると k=1 no-op, 2026-06-10 修正)
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    v = np.array([d[i] for i in range(len(raw))], float)
    s = v.sum()
    return v / s if s > 0 else raw / raw.sum()


def _softmax_T(scores: np.ndarray, T: float) -> np.ndarray:
    z = np.asarray(scores, float) / max(T, 1e-6)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def load_brackets(rids: list[str]) -> dict[tuple[str, int], int]:
    """(race_id, horse_number) -> bracket(枠番) を cached HTML から構築。"""
    out: dict[tuple[str, int], int] = {}
    miss = 0
    for rid in rids:
        loaded = load_race(rid)
        if loaded is None:
            miss += 1
            continue
        rd, _ = loaded
        for h in rd.race.horses:
            if h.bracket and h.bracket > 0:
                out[(rid, int(h.number))] = int(h.bracket)
    if miss:
        print(f"  (load_race miss: {miss} races without cached shutuba)")
    return out


def attach_draw(df: pd.DataFrame, bmap: dict[tuple[str, int], int]) -> pd.DataFrame:
    df = df.copy()
    key = list(zip(df["race_id"].astype(str), df["horse_number"].astype(int)))
    df["bracket"] = [bmap.get(k, 0) for k in key]
    # 枠が取れた race のみ残す (枠 0 の混入を避ける = bracket 列の意味が崩れる)
    good_rid = (
        df.groupby("race_id")["bracket"]
        .apply(lambda s: (s > 0).mean() >= 0.8)
        .pipe(lambda s: s[s].index)
    )
    df = df[df["race_id"].isin(good_rid)].copy()
    # 枠取れてない端数行 (取消等の残骸) は中央値補完してから rel を作る
    df.loc[df["bracket"] <= 0, "bracket"] = (
        df.groupby("race_id")["bracket"].transform(lambda s: s[s > 0].median())
    ).fillna(4)
    maxb = df.groupby("race_id")["bracket"].transform("max").clip(lower=1)
    df["bracket_rel"] = df["bracket"] / maxb
    df["gate_rel"] = df["horse_number"] / df["n_horses"].clip(lower=1)
    df["is_inner_bracket"] = (df["bracket"] <= 2).astype(float)
    return df


def fit_draw_winrate_te(df_a: pd.DataFrame, k: float = 50.0) -> tuple[dict, float]:
    """fold A のみで venue×距離bucket×枠bucket -> P(win) を集計 (shrink)。leakage 無し。"""
    g = df_a.copy()
    g["db"] = g["distance"].map(_dist_bucket)
    g["bb"] = g["bracket"].astype(int).map(_bracket_bucket)
    glob = float(g["target_top1"].mean())  # グローバル 1 着率 (= 1/平均頭数 近辺)
    tbl: dict[tuple, float] = {}
    grp = g.groupby(["venue", "db", "bb"])["target_top1"]
    for key, s in grp:
        n = len(s)
        mean = float(s.mean())
        # ベイズ shrink: (n*mean + k*glob) / (n + k)
        tbl[key] = (n * mean + k * glob) / (n + k)
    return tbl, glob


def apply_draw_winrate_te(df: pd.DataFrame, tbl: dict, glob: float) -> pd.DataFrame:
    df = df.copy()
    db = df["distance"].map(_dist_bucket)
    bb = df["bracket"].astype(int).map(_bracket_bucket)
    keys = list(zip(df["venue"].astype(str), db, bb))
    df["draw_winrate_te"] = [tbl.get(k, glob) for k in keys]
    return df


def race_arrays(df: pd.DataFrame, booster, feats: list[str]):
    """race ごとに (model_win_probs(score), devig_market, winner_idx, odds, y_top1) を作る。"""
    out = []
    for _rid, g in df.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 4:
            continue
        fp = g["finish_pos"].to_numpy()
        # winner 行 index
        widx = None
        for i, p in enumerate(fp):
            if p == 1:
                widx = i
                break
        if widx is None:
            continue
        raw = booster.predict(g[feats].values)
        odds = g["win_odds"].to_numpy(float)
        mk = _devig(odds)
        out.append({"raw": np.asarray(raw, float), "mk": mk, "w": int(widx),
                    "odds": odds, "y": g["target_top1"].to_numpy(float)})
    return out


def fit_T(races, raw_key="raw"):
    """単勝 (winner) log-loss 最小の softmax 温度を grid で。"""
    best_T, best = 0.5, 1e18
    for T in [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.5]:
        ll = 0.0
        for r in races:
            p = _softmax_T(r[raw_key], T)
            ll -= np.log(max(p[r["w"]], 1e-12))
        if ll < best:
            best, best_T = ll, T
    return best_T


def fit_beta(races, T):
    """conditional-logit 勝者 log-lik 最大化で β (market_blend) を MLE。

    blend = softmax( (1-β)·log(model_p) + β·log(market_p) )。β=1 → 市場のみ (edge 無し)。
    """
    pre = [(_softmax_T(r["raw"], T), r["mk"], r["w"]) for r in races]

    def neg_ll(beta):
        a = 1.0 - beta
        s = 0.0
        for mp, mk, w in pre:
            z = a * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
            z = z - z.max()
            e = np.exp(z)
            bp = e / e.sum()
            s -= np.log(max(bp[w], 1e-12))
        return s
    res = minimize_scalar(neg_ll, bounds=(0.0, 1.0), method="bounded")
    return float(res.x)


def eval_holdout(races, T, beta):
    """fold C で frozen (T, β) の単勝 OOS ROI / log-loss を model / market / blend で評価。"""
    rows = {"model": dict(stake=0, pay=0, hit=0, ll=0.0),
            "market": dict(stake=0, pay=0, hit=0, ll=0.0),
            "blend": dict(stake=0, pay=0, hit=0, ll=0.0)}
    n = 0
    for r in races:
        n += 1
        mp = _softmax_T(r["raw"], T)
        mk = r["mk"]
        z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
        z = z - z.max()
        bp = np.exp(z); bp = bp / bp.sum()
        odds, w = r["odds"], r["w"]
        for mode, p in (("model", mp), ("market", mk), ("blend", bp)):
            top = int(np.argmax(p))
            rows[mode]["stake"] += 100
            if top == w:
                rows[mode]["hit"] += 1
                rows[mode]["pay"] += int(100 * odds[w])
            rows[mode]["ll"] -= np.log(max(p[w], 1e-12))
    res = {}
    for mode, d in rows.items():
        res[mode] = {
            "roi": (d["pay"] / d["stake"] * 100) if d["stake"] else 0.0,
            "hit": d["hit"],
            "ll": d["ll"] / max(n, 1),
        }
    res["n"] = n
    return res


def run_segment(df_seg: pd.DataFrame, label: str, bmap: dict):
    # settled (winner ラベルあり) のみ
    df_seg = df_seg[df_seg.race_id.isin(
        df_seg.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    df = attach_draw(df_seg, bmap)
    rids = sorted(df.race_id.astype(str).unique().tolist(), key=_race_int)
    n = len(rids)
    if n < 40:
        print(f"\n[{label}] races={n} — too few, skip")
        return None
    A = set(rids[: int(n * .60)]); B = set(rids[int(n * .60): int(n * .80)]); C = set(rids[int(n * .80):])
    da = df[df.race_id.astype(str).isin(A)].sort_values(["race_id", "horse_number"])
    db = df[df.race_id.astype(str).isin(B)].sort_values(["race_id", "horse_number"])
    dc = df[df.race_id.astype(str).isin(C)].sort_values(["race_id", "horse_number"])
    print(f"\n========== [{label}]  races={n}  (A={len(A)} train / B={len(B)} fit-β / C={len(C)} holdout) ==========")
    print(f"  venues: {sorted(df.venue.dropna().unique().tolist())}")

    # target-encoding 枠別勝率 (fold A only)
    te_tbl, glob = fit_draw_winrate_te(da)
    da = apply_draw_winrate_te(da, te_tbl, glob)
    db = apply_draw_winrate_te(db, te_tbl, glob)
    dc = apply_draw_winrate_te(dc, te_tbl, glob)

    # 枠別勝率の素の傾向 (内/中/外 × 距離) を A で表示 (= 仮説の生データ)
    diag = da.assign(bb=da["bracket"].astype(int).map(_bracket_bucket),
                     dbk=da["distance"].map(_dist_bucket))
    print("  [fold A] 枠bucket別 1着率 (n>=200 のみ, 期待=1/平均頭数):")
    exp = 1.0 / da.groupby("race_id")["n_horses"].first().mean()
    print(f"    (ランダム期待 ≈ {exp*100:.1f}%)")
    for dbk in ["S", "M", "L"]:
        parts = []
        for bb in ["in", "mid", "out"]:
            s = diag[(diag.bb == bb) & (diag.dbk == dbk)]["target_top1"]
            if len(s) >= 200:
                parts.append(f"{bb}={s.mean()*100:4.1f}%(n={len(s)})")
        if parts:
            print(f"    dist={dbk}: " + "  ".join(parts))

    out = {}
    for variant, feats in (("baseline(26)", FEATS), ("+draw(26+5)", FEATS + DRAW_FEATS)):
        ga = da.groupby("race_id", sort=False).size().to_numpy()
        gb = db.groupby("race_id", sort=False).size().to_numpy()
        dtr = lgb.Dataset(da[feats].values, label=da["target_rank"].values, group=ga)
        dva = lgb.Dataset(db[feats].values, label=db["target_rank"].values, group=gb, reference=dtr)
        booster = lgb.train(PARAMS, dtr, num_boost_round=800, valid_sets=[dva],
                            callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)])
        rb = race_arrays(db, booster, feats)
        rc = race_arrays(dc, booster, feats)
        T = fit_T(rb)
        beta = fit_beta(rb, T)
        ev = eval_holdout(rc, T, beta)
        out[variant] = dict(beta=beta, T=T, ev=ev, booster=booster, feats=feats)
        m, mk, bl = ev["model"], ev["market"], ev["blend"]
        print(f"\n  --- {variant} ---")
        print(f"    β-MLE (fold B) = {beta:.3f}   T={T:.2f}   "
              f"{'→ 市場超えの可能性 (β<0.9)' if beta < 0.9 else '→ edge 無し (β≈1 = 市場コピー)'}")
        print(f"    holdout C (n={ev['n']}) 単勝:")
        print(f"       model : ROI {m['roi']:6.1f}%  hit {m['hit']:3d}  logloss {m['ll']:.4f}")
        print(f"       market: ROI {mk['roi']:6.1f}%  hit {mk['hit']:3d}  logloss {mk['ll']:.4f}")
        print(f"       blend : ROI {bl['roi']:6.1f}%  hit {bl['hit']:3d}  logloss {bl['ll']:.4f}")

    # +draw が baseline 比でどう動いたか
    bb, dd = out["baseline(26)"], out["+draw(26+5)"]
    print(f"\n  >>> Δ(+draw − baseline): "
          f"β {dd['beta']-bb['beta']:+.3f}  |  "
          f"holdout blend ROI {dd['ev']['blend']['roi']-bb['ev']['blend']['roi']:+.1f}pt  |  "
          f"blend logloss {dd['ev']['blend']['ll']-bb['ev']['blend']['ll']:+.4f}")

    # +draw モデルでの draw 特徴の gain importance
    bo = dd["booster"]; fn = dd["feats"]
    gain = bo.feature_importance(importance_type="gain")
    imp = sorted(zip(fn, gain), key=lambda x: -x[1])
    tot = sum(gain) or 1.0
    print("  +draw モデルの draw 特徴 gain share:")
    for f in DRAW_FEATS:
        gi = dict(imp).get(f, 0)
        rank = [i for i, (nm, _) in enumerate(imp) if nm == f]
        r = rank[0] + 1 if rank else -1
        print(f"     {f:18s} gain {gi/tot*100:5.2f}%  (rank {r}/{len(fn)})")
    return out


def main() -> int:
    seg = sys.argv[1] if len(sys.argv) > 1 else "nar"
    df = pd.read_parquet(ALL)
    isj = df.race_id.astype(str).str[4:6].isin(JRA_CODES)
    nar_dirt = df[(~isj) & (df["surface"] == "ダート")].copy()

    print("枠データの所在: all.parquet に枠番列は無い → src.dataset.load_race の "
          "rd.race.horses[].bracket (parse.py が出馬表から取得) を join")
    all_rids = nar_dirt.race_id.astype(str).unique().tolist()
    print(f"NAR ダート races={len(all_rids)} の枠を cached HTML から構築中 ...")
    bmap = load_brackets(all_rids)
    cov_rids = {rid for (rid, _n) in bmap.keys()}
    print(f"  枠取得できた race: {len(cov_rids)}/{len(all_rids)}  "
          f"((rid,馬番) entries={len(bmap)})")

    if seg in ("nar", "all"):
        run_segment(nar_dirt, "NAR-dirt-ALL", bmap)
    if seg in ("nar_small", "all", "nar"):
        small = nar_dirt[nar_dirt.venue.isin(SMALL_VENUES)].copy()
        run_segment(small, f"NAR-dirt-SMALL({'/'.join(sorted(SMALL_VENUES))})", bmap)

    print("\n注: β は train(A) と別 partition(B) で 1 回だけ MLE 凍結 = overfit 回避済。")
    print("    β≈1.0 は『枠を入れても勝者の確率配分は市場のコピーに収束 = 市場超えの情報なし』を意味する。")
    print("    OOS(C) の ROI が全 mode で 100% 未満なら依然 -EV (控除率に負ける)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
