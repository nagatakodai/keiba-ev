"""NAR ダートの「騎手/厩舎の勝負気配 (intent)」が市場 (win オッズ) を上回る情報を持つか
β-MLE で検証する。当プロジェクトは他の全特徴で β=1.0 (市場超え不可)。これも β=1.0 か正直に測る。

仮説 (NAR は intent signal が強いとされる):
  - jockey upgrade : 格上騎手を人気薄に乗せる勝負気配
  - 人気薄 × 強騎手 interaction
  - クラス降級 (class drop) : より低いクラスへ卸して勝ちに行く
  - 遠征 (travel)        : 別場へ遠征 = 勝負レース

検証手法 (train_segment_models.py / test_position_prob.py に倣う):
  時系列 3-fold:  A=[0,60%)  LightGBM fundamental 学習 (lambdarank)
                  B=[60,80%) 別 partition で β を conditional-logit 勝者 MLE (overfit 回避)
                  C=[80,100%) 完全 hold-out で frozen β を OOS 単勝 ROI 評価
  baseline (既存 26 特徴) と +intent (baseline + intent 特徴) で 2 本立てて β / OOS を比較。

leakage 防止:
  - 騎手通算勝率は **train fold A の race 結果のみ** で集計 (finish_pos==1 / starts) → B/C へ適用。
  - past_runs は馬柱 = 構造的に対象 race 日付以前 (実機確認: 28671 past run 全て race date 未満)。
  - intent 特徴は load_race の rd.race.horses[].jockey_name / past_runs[].jockey / race_class / venue から構築。

使い方: .venv/bin/python scripts/test_intent.py [nar|jra|all]   (既定 nar)
"""
from __future__ import annotations

import json
import re
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
META = json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())
BASE_FEATS = META["feature_cols"]            # 既存 26 特徴 (baseline)
PARAMS = dict(META["params"])
JRA_CODES = {f"{i:02d}" for i in range(1, 11)}

INTENT_FEATS = [
    "jockey_winrate",          # 今走騎手の通算勝率 (train fold 集計)
    "jockey_upgrade",          # 今走騎手勝率 − 馬の過去走平均騎手勝率 (>0 = 格上騎手起用)
    "longshot_x_jockey",       # max(0, win_odds-10)/win_odds * jockey_winrate (人気薄×強騎手)
    "class_drop",              # 過去走平均クラス − 今走クラス (>0 = 降級)
    "travel",                  # 今走場が直近過去走の場と異なる (遠征) 0/1
]


def _race_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# class rank (小さいほど格上)。NAR 番組: A>B>C のラダー + 組分け + 重賞/OP + 3歳/2歳/未勝利。
# 98.2% coverage (実機確認)。残りは "...(3歳)" 系の地方特別 → 3歳条件として吸収。
# ---------------------------------------------------------------------------
def _class_rank(s: str | None):
    if not s:
        return None
    if "重賞" in s or "G1" in s or "G2" in s or "G3" in s or "Jpn" in s:
        return 1
    if "OP" in s or "オープン" in s:
        return 3
    m = re.search(r"([ABC])\s*([1-4])", s)
    if m:
        return {"A": 0, "B": 1, "C": 2}[m.group(1)] * 10 + int(m.group(2)) + 4  # A1=5 .. C4=28
    m2 = re.search(r"(\d)\s*勝クラス", s)
    if m2:
        return 18 - int(m2.group(1)) * 2  # 1勝=16 / 2勝=14 / 3勝=12
    if "未勝利" in s or "新馬" in s or "フレッシュ" in s or "未" in s:
        return 32
    if "2歳" in s:
        return 31
    if re.search(r"3歳[\d一二三四五六七八九十ー\-]", s) or "3歳条件" in s or "3歳以上" in s or "(3歳)" in s:
        return 26
    if "A" in s:
        return 8
    if "B" in s:
        return 18
    if "C" in s:
        return 27
    return None


def _seg_mask(rid_str: pd.Series, surface: pd.Series, seg: str) -> pd.Series:
    isj = rid_str.str[4:6].isin(JRA_CODES)
    if seg == "nar":
        return (~isj) & (surface == "ダート")
    if seg == "jra":
        return isj
    return pd.Series(True, index=rid_str.index)


def _devig(odds):
    raw = 1.0 / np.asarray(odds, float)
    raw = raw / raw.sum()
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    v = np.array([d[i] for i in range(len(raw))], float)
    s = v.sum()
    return v / s if s > 0 else raw


# ---------------------------------------------------------------------------
# 騎手通算勝率を train fold の race だけで集計 (leakage 防止)。
# ---------------------------------------------------------------------------
def _build_jockey_winrate(train_rids: set[str]) -> tuple[dict[str, float], float]:
    wins: dict[str, int] = {}
    starts: dict[str, int] = {}
    for rid in train_rids:
        ld = load_race(rid)
        if not ld:
            continue
        rd, _res = ld
        for h in rd.race.horses:
            if h.absent or not h.jockey_name:
                continue
            jk = h.jockey_name
            starts[jk] = starts.get(jk, 0) + 1
            # finish_pos==1 を馬柱の has-result から拾えないので結果 dict から
        res = ld[1]
        winner_num = None
        if res and (res.get("finish_order") or []):
            try:
                winner_num = int(res["finish_order"][0])
            except (ValueError, TypeError):
                winner_num = None
        if winner_num is not None:
            for h in rd.race.horses:
                if h.number == winner_num and h.jockey_name:
                    wins[h.jockey_name] = wins.get(h.jockey_name, 0) + 1
                    break
    # global mean (prior) — starts が少ない騎手の shrink 用
    tot_w = sum(wins.values())
    tot_s = sum(starts.values())
    gmean = (tot_w / tot_s) if tot_s else 0.08
    # empirical-Bayes shrink (prior strength = 20 starts)
    K = 20.0
    rate = {jk: (wins.get(jk, 0) + gmean * K) / (st + K) for jk, st in starts.items()}
    return rate, gmean


# ---------------------------------------------------------------------------
# 1 レースぶんの intent 特徴を馬番→dict で返す。jrate = 騎手勝率テーブル (train 集計)。
# ---------------------------------------------------------------------------
def _intent_for_race(rd, jrate: dict[str, float], gmean: float) -> dict[int, dict]:
    cur_rank = _class_rank(rd.race.race_class)
    cur_venue = rd.race.venue_name
    out: dict[int, dict] = {}
    for h in rd.race.horses:
        if h.absent:
            continue
        jw = jrate.get(h.jockey_name, gmean)
        # 馬の過去走の騎手勝率 (常用騎手) 平均
        past_jw = [jrate.get(pr.jockey, gmean) for pr in h.past_runs if pr.jockey]
        mean_past_jw = float(np.mean(past_jw)) if past_jw else gmean
        upgrade = jw - mean_past_jw
        # 人気薄 (高オッズ) × 強騎手。win_odds は parquet 側にあるので後で掛ける用に raw を出す
        # class drop: 過去走平均クラス rank − 今走 rank ( >0 = 今走の方が格下クラス = 降級 )
        past_ranks = [r for r in (_class_rank(pr.race_class) for pr in h.past_runs) if r is not None]
        if past_ranks and cur_rank is not None:
            class_drop = float(np.mean(past_ranks)) - cur_rank
        else:
            class_drop = 0.0
        # 遠征: 直近過去走 (past_runs[0]) の場が今走と違う
        travel = 0.0
        if h.past_runs and cur_venue:
            last_v = h.past_runs[0].venue
            if last_v and last_v != cur_venue:
                travel = 1.0
        out[h.number] = {
            "jockey_winrate": jw,
            "jockey_upgrade": upgrade,
            "_jw_for_inter": jw,        # interaction は parquet の win_odds と合成
            "class_drop": class_drop,
            "travel": travel,
        }
    return out


def _attach_intent(df: pd.DataFrame, jrate: dict[str, float], gmean: float) -> pd.DataFrame:
    """df (race 行) に intent 特徴列を追加。race_id ごとに load_race して馬番で join。"""
    cols = {f: [] for f in INTENT_FEATS}
    idx = []
    cache_rids = df["race_id"].astype(str).unique().tolist()
    feat_by_rid: dict[str, dict[int, dict]] = {}
    for rid in cache_rids:
        ld = load_race(rid)
        if not ld:
            continue
        feat_by_rid[rid] = _intent_for_race(ld[0], jrate, gmean)
    for row in df.itertuples():
        rid = str(row.race_id)
        num = int(row.horse_number)
        fr = feat_by_rid.get(rid, {})
        d = fr.get(num)
        idx.append(row.Index)
        if d is None:
            cols["jockey_winrate"].append(gmean)
            cols["jockey_upgrade"].append(0.0)
            cols["longshot_x_jockey"].append(0.0)
            cols["class_drop"].append(0.0)
            cols["travel"].append(0.0)
        else:
            odds = float(row.win_odds) if row.win_odds and row.win_odds > 0 else 999.0
            longshot = max(0.0, odds - 10.0) / odds  # 0 (人気) .. ~1 (大穴)
            cols["jockey_winrate"].append(d["jockey_winrate"])
            cols["jockey_upgrade"].append(d["jockey_upgrade"])
            cols["longshot_x_jockey"].append(longshot * d["_jw_for_inter"])
            cols["class_drop"].append(d["class_drop"])
            cols["travel"].append(d["travel"])
    out = df.copy()
    for f in INTENT_FEATS:
        out[f] = pd.Series(cols[f], index=idx)
    return out


def _race_arrays(df: pd.DataFrame, feats: list[str]):
    """race ごとに (X, win_odds, winner_idx) を貯める。"""
    out = []
    for _rid, g in df.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 4:
            continue
        fp = g["finish_pos"].to_numpy()
        pos = {int(p): i for i, p in enumerate(fp) if p == 1}
        if 1 not in pos:
            continue
        out.append({
            "X": g[feats].values,
            "odds": g["win_odds"].to_numpy(float),
            "winner": pos[1],
        })
    return out


def _softmax(x, t=1.0):
    z = np.asarray(x, float) / max(t, 1e-6)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def _train_and_eval(dfa, dfb, dfc, feats, label):
    """A で lambdarank 学習 → B で β MLE (conditional logit) → C で OOS 単勝 ROI。"""
    ga = dfa.groupby("race_id", sort=False).size().to_numpy()
    gb = dfb.groupby("race_id", sort=False).size().to_numpy()
    dtr = lgb.Dataset(dfa[feats].values, label=dfa["target_rank"].values, group=ga)
    dva = lgb.Dataset(dfb[feats].values, label=dfb["target_rank"].values, group=gb, reference=dtr)
    booster = lgb.train(PARAMS, dtr, num_boost_round=800, valid_sets=[dva],
                        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)])

    rb = _race_arrays(dfb, feats)
    rc = _race_arrays(dfc, feats)

    # T (softmax 温度) を B の勝者 logloss で軽く合わせる (train_segment_models 流)
    def fit_T(races):
        best_T, best = 0.5, 1e18
        for T in [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0]:
            ll = 0.0
            for r in races:
                p = _softmax(booster.predict(r["X"]), T)
                ll -= np.log(max(p[r["winner"]], 1e-12))
            if ll < best:
                best, best_T = ll, T
        return best_T
    T = fit_T(rb)

    # β: conditional-logit 勝者 MLE on B (model logit と market logit を log-linear blend)
    pre = [(_softmax(booster.predict(r["X"]), T), _devig(r["odds"]), r["winner"]) for r in rb]

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
    beta = float(minimize_scalar(neg_ll, bounds=(0.0, 1.0), method="bounded").x)

    # OOS 単勝 ROI on C: model-only / market / blend(β) の top-1 を ¥100 ずつ
    def roi(races, mode):
        stake = pay = hit = 0
        for r in races:
            mp = _softmax(booster.predict(r["X"]), T)
            mk = _devig(r["odds"])
            if mode == "model":
                p = mp
            elif mode == "market":
                p = mk
            else:
                z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
                z = z - z.max()
                p = np.exp(z); p = p / p.sum()
            top = int(np.argmax(p))
            stake += 100
            if top == r["winner"]:
                hit += 1
                pay += int(100 * r["odds"][top])
        return (pay / stake * 100 if stake else 0.0), hit, stake // 100

    # hold-out C の logloss も (model/market/blend)
    def logloss(races, mode):
        s = nn = 0
        for r in races:
            mp = _softmax(booster.predict(r["X"]), T)
            mk = _devig(r["odds"])
            if mode == "model":
                p = mp
            elif mode == "market":
                p = mk
            else:
                z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
                z = z - z.max()
                p = np.exp(z); p = p / p.sum()
            s -= np.log(max(p[r["winner"]], 1e-12)); nn += 1
        return s / nn if nn else 0.0

    rm, hm, n = roi(rc, "model")
    rk, hk, _ = roi(rc, "market")
    rbl, hbl, _ = roi(rc, "blend")
    print(f"  [{label}]  β-MLE(B) = {beta:.3f}   T={T:.2f}")
    print(f"     hold-out C logloss  model {logloss(rc,'model'):.4f} / market {logloss(rc,'market'):.4f} / blend {logloss(rc,'blend'):.4f}")
    print(f"     OOS 単勝 ROI (n={n})  model {rm:.1f}% (hit {hm}) / market {rk:.1f}% (hit {hk}) / blend {rbl:.1f}% (hit {hbl})")
    return {"beta": beta, "T": T, "n": n,
            "roi_model": rm, "roi_market": rk, "roi_blend": rbl,
            "ll_model": logloss(rc, "model"), "ll_market": logloss(rc, "market"), "ll_blend": logloss(rc, "blend"),
            "booster": booster}


def _feat_importance(booster, feats):
    gain = booster.feature_importance(importance_type="gain")
    pairs = sorted(zip(feats, gain), key=lambda x: -x[1])
    tot = sum(gain) or 1.0
    return [(f, g / tot * 100) for f, g in pairs]


def main() -> int:
    seg = sys.argv[1] if len(sys.argv) > 1 else "nar"
    df = pd.read_parquet(ALL)
    rid_str = df["race_id"].astype(str)
    df = df[_seg_mask(rid_str, df["surface"], seg)].copy()
    # winner のある race のみ (β MLE / ROI は勝者必須)
    df = df[df["race_id"].isin(
        df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    rids = sorted(df["race_id"].unique().tolist(), key=_race_int)
    n = len(rids)
    A = set(rids[: int(n * 0.60)])
    B = set(rids[int(n * 0.60): int(n * 0.80)])
    C = set(rids[int(n * 0.80):])
    print(f"=== intent signal β-MLE check [{seg}] races={n} (A={len(A)} train / B={len(B)} β-fit / C={len(C)} holdout) ===")
    print(f"    baseline feats={len(BASE_FEATS)}  +intent feats={INTENT_FEATS}")

    # 騎手勝率テーブルは A (train) のみで集計 → leakage 防止
    print("  [1/3] 騎手通算勝率を train fold A から集計中 ...", flush=True)
    jrate, gmean = _build_jockey_winrate(A)
    print(f"        jockeys={len(jrate)}  global win mean={gmean:.3f}", flush=True)

    dfa = df[df["race_id"].isin(A)].sort_values(["race_id", "horse_number"])
    dfb = df[df["race_id"].isin(B)].sort_values(["race_id", "horse_number"])
    dfc = df[df["race_id"].isin(C)].sort_values(["race_id", "horse_number"])

    print("  [2/3] intent 特徴を A/B/C に付与中 (load_race) ...", flush=True)
    dfa = _attach_intent(dfa, jrate, gmean)
    dfb = _attach_intent(dfb, jrate, gmean)
    dfc = _attach_intent(dfc, jrate, gmean)

    # intent 特徴の素の signal を確認: 勝ち馬 vs 負け馬で平均差
    print("  intent 特徴 (勝ち馬 mean vs 負け馬 mean, A+B+C):")
    full = pd.concat([dfa, dfb, dfc])
    won = full[full["finish_pos"] == 1]
    lost = full[full["finish_pos"] != 1]
    for f in INTENT_FEATS:
        print(f"     {f:18s} won {won[f].mean():+.4f}  lost {lost[f].mean():+.4f}  Δ {won[f].mean()-lost[f].mean():+.4f}")

    # longshot_x_jockey は win_odds を含む = 市場量の proxy。これがあると model が odds を
    # 横流しで使い feature gain を独占する (実機 71%)。純粋な「市場に無い intent」だけを
    # 測るため、odds を含まない 4 特徴だけの arm も並べる。
    INTENT_NOODDS = [f for f in INTENT_FEATS if f != "longshot_x_jockey"]
    print("  [3/3] baseline / +intent(全5) / +intent(odds除外4) を 3-fold β-MLE で比較:", flush=True)
    res_base = _train_and_eval(dfa, dfb, dfc, BASE_FEATS, "baseline      ")
    res_int = _train_and_eval(dfa, dfb, dfc, BASE_FEATS + INTENT_FEATS, "+intent(全5)  ")
    res_no = _train_and_eval(dfa, dfb, dfc, BASE_FEATS + INTENT_NOODDS, "+intent(odds除外)")

    print("\n  +intent(全5) モデルの feature gain 上位 (intent 列に★):")
    for f, pct in _feat_importance(res_int["booster"], BASE_FEATS + INTENT_FEATS)[:14]:
        star = " ★" if f in INTENT_FEATS else ""
        print(f"     {pct:5.1f}%  {f}{star}")
    intent_gain = sum(p for f, p in _feat_importance(res_int["booster"], BASE_FEATS + INTENT_FEATS) if f in INTENT_FEATS)
    print(f"     → intent 5 特徴の合計 gain シェア = {intent_gain:.1f}%")
    print("  +intent(odds除外4) モデルの intent gain シェア:")
    noodds_gain = sum(p for f, p in _feat_importance(res_no["booster"], BASE_FEATS + INTENT_NOODDS) if f in INTENT_NOODDS)
    for f, pct in _feat_importance(res_no["booster"], BASE_FEATS + INTENT_NOODDS):
        if f in INTENT_NOODDS:
            print(f"     {pct:5.2f}%  {f} ★")
    print(f"     → odds除外 intent 4 特徴の合計 gain シェア = {noodds_gain:.1f}%")

    print("\n=== 結論 ===")
    print(f"  β-MLE:  baseline {res_base['beta']:.3f}  /  +intent(全5) {res_int['beta']:.3f}"
          f"  /  +intent(odds除外) {res_no['beta']:.3f}   (1.0 = 市場のコピー = 上回り無し)")
    print(f"  OOS 単勝 ROI(blend):  baseline {res_base['roi_blend']:.1f}%  /  +intent(全5) {res_int['roi_blend']:.1f}%"
          f"  /  +intent(odds除外) {res_no['roi_blend']:.1f}%   (市場 {res_base['roi_market']:.1f}%)")
    print(f"  OOS logloss(blend):   baseline {res_base['ll_blend']:.4f}  /  +intent(全5) {res_int['ll_blend']:.4f}"
          f"  /  +intent(odds除外) {res_no['ll_blend']:.4f}")
    any_beat = min(res_int["beta"], res_no["beta"]) < 0.90
    print("  判定:", "intent が市場超え (β<0.9)" if any_beat
          else "market copy (β≈1.0) — intent も市場を超えられず (他 26 特徴と同じ結論)")
    print(f"  注: N={res_int['n']} (hold-out C)。β は別 partition B で凍結 = overfit 回避。"
          " ROI<100% は依然 -EV。raw signal は正方向 (上表) だが市場が既に織り込み済。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
