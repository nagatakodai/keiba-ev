"""NAR ダートの「展開 (ペース) 予測」を特徴量化し、市場を OOS で上回るか β-MLE で検証する。

NAR は中間ラップ非公開なので laps からのペース図表は作れない。代わりに **出走全馬の脚質
(過去走の通過順 passing 由来) から today のペース展開を推定**し、各馬にそのペースが向くかの
特徴量 (pace_advantage) を作る (プロの展開ハンデ)。仮説: NAR の公衆は展開を過小評価するので
edge が残るかもしれない。

既に検証済: 速度図表 v2 / win 確率 / 馬自身の front_rate(脚質) は β-MLE≈1.0 (市場超えず)。
本スクリプトが測るのは **馬自身の脚質ではなく「field 構成 × 自分の脚質」の交互作用**
(= 公衆が見落とすとされる相対的な展開利)。

特徴量 (全て発走前に分かる過去走由来。当該レースの結果は不使用 = no leakage):
  pace_self_pos   : 自分の平均 早期位置率 (early_corner / field_size。小=前)。0-1。
  pace_front_count: today の推定 逃げ・先行頭数 (出走馬の pos_score を闾値判定して数える)。
  pace_pressure   : today の推定ペース圧 (前に行きたい馬が多いほど高い = ハイペース)。
  pace_lone_front : 自分が前で かつ 前に行く馬が少ない (= 単騎逃げ/番手の前残り利)。
  pace_advantage  : 推定ペース × 自分の脚質 の向き (ハイペース×差し=+, スロー×逃げ=+)。

検証: baseline (既存26特徴量) vs +pace (5特徴量追加) を 3-fold
  (A=[0,60%)学習 / B=[60,80%)で T・β を MLE / C=[80,100%)完全 hold-out) で学習し、
  β-MLE と hold-out 単勝 ROI / log-loss を比較。β が 1.0 から下がれば展開予測が edge。

test_speed_v2.py / train_segment_models.py の β-MLE/de-vig/3-fold 実装を流用。
本番コード/モデルは一切変更しない (このスクリプト内で特徴量を構築)。

使い方: .venv/bin/python scripts/test_pace_projection.py [--segment nar|jra|all] [--seeds 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ev import power_method_overround  # noqa: E402

ALL = ROOT / "data" / "datasets" / "all.parquet"
RAW = ROOT / "data" / "raw"
CACHE = ROOT / "data" / "cache" / "pace_features.parquet"
META = json.loads((ROOT / "data" / "models" / "lgbm_metadata.json").read_text())
BASE_FEATS = META["feature_cols"]
PACE_FEATS = ["pace_self_pos", "pace_front_count", "pace_pressure",
              "pace_lone_front", "pace_advantage"]
PARAMS = dict(META["params"])
JRA = {f"{i:02d}" for i in range(1, 11)}

# 脚質しきい値 (early-position fraction = corner1 位置 / field_size, 小=前)。
FRONT_THRESH = 0.25     # この値以下を「逃げ・先行」とみなす
CLOSER_THRESH = 0.60    # この値以上を「差し・追込」とみなす
MIN_RUNS_FOR_STYLE = 1  # 脚質推定に必要な最低 past run 数


# ----------------------------------------------------------------------------
# 特徴量構築 (per race, 過去走の passing のみ使用 = 発走前に既知)
# ----------------------------------------------------------------------------
def _horse_pos_score(past_runs) -> tuple[float | None, int]:
    """馬の脚質スコア = 過去走の (corner1 位置 / field_size) の平均。

    小さい=前 (逃げ/先行), 大きい=後ろ (差し/追込)。
    通過順が無い/頭数 0 の走を除外。直近5走 (馬柱の窓) のみ。
    return (pos_score or None, 使った run 数)。
    """
    fracs = []
    for pr in (past_runs or [])[:5]:
        passing = getattr(pr, "passing", "") or ""
        fs = getattr(pr, "field_size", 0) or 0
        nums = [int(x) for x in passing.replace("-", " ").split() if x.isdigit()]
        if not nums or fs <= 1:
            continue
        early = nums[0]
        # corner1 が field_size を超える壊れ値はスキップ
        if early < 1 or early > fs:
            continue
        # (位置-1)/(field-1) で 0..1 正規化 (1番手→0, 最後方→1)
        fracs.append((early - 1) / (fs - 1))
    if len(fracs) < MIN_RUNS_FOR_STYLE:
        return None, 0
    return float(np.mean(fracs)), len(fracs)


def _one(rid: str):
    """1 レースの全馬 pace 特徴量を作る。

    手順:
      1. 各馬の pos_score (脚質) を過去走から算出。
      2. field 全体の脚質分布から today のペース展開を推定
         (front_count, pace_pressure)。
      3. 各馬の pace_advantage / pace_lone_front を交互作用で作る。
    """
    from src.dataset import load_race
    try:
        loaded = load_race(rid)
    except Exception:
        return []
    if loaded is None:
        return []
    rd, _ = loaded

    horses = [h for h in rd.race.horses if not h.absent]
    if len(horses) < 3:
        return []

    # --- step 1: 各馬の脚質スコア ---
    scores: dict[int, float | None] = {}
    for h in horses:
        ps, _n = _horse_pos_score(h.past_runs)
        scores[h.number] = ps

    known = [v for v in scores.values() if v is not None]
    field_mean = float(np.mean(known)) if known else 0.5

    # --- step 2: レースのペース展開を推定 (出走馬の脚質構成から) ---
    # 前に行きたい馬 (pos_score <= FRONT_THRESH) を数える。
    front_count = sum(1 for v in known if v <= FRONT_THRESH)
    closer_count = sum(1 for v in known if v >= CLOSER_THRESH)

    # pace_pressure: 「前を取りたい馬がどれだけ密集するか」。
    # 前方適性 (1 - pos_score, clip>=0) の合計 = 先頭争いの圧。
    # 前に行く馬が多い/前々過剰 → ハイペース → 差し有利。
    front_aff = [max(0.0, FRONT_THRESH * 2 - v) for v in known]  # 前ほど大
    pace_pressure = float(np.sum(front_aff))

    rows = []
    for h in horses:
        ps = scores[h.number]
        s = field_mean if ps is None else ps   # 脚質不明は field 平均で埋める
        # この馬以外の前馬数 (自分を二重カウントしない)
        others_front = front_count - (1 if (ps is not None and ps <= FRONT_THRESH) else 0)

        # pace_lone_front: 自分が前 (s 小) かつ 他に前馬が少ない → 単騎/番手の前残り利。
        # front_ness = max(0, FRONT_THRESH - s)/FRONT_THRESH ∈[0,1]、others_front 少で増。
        front_ness = max(0.0, (FRONT_THRESH - s)) / FRONT_THRESH
        lone = front_ness / (1.0 + others_front)

        # pace_advantage: 推定ペース × 自分の脚質の符号付き向き。
        #   pace_z>0 = ハイペース (前過剰) → 差し馬 (s 大) に +、逃げ馬 (s 小) に -。
        #   pace_z<0 = スローペース (前少) → 逃げ馬 (s 小) に +、差し馬 (s 大) に -。
        # pace_z は「想定先行頭数の field 比」を中心 0 に。
        n = len(horses)
        pace_z = (front_count / n) - 0.30   # 30% 程度が中庸という事前
        style_centered = s - 0.5            # +で後方, -で前方
        # ハイペース(+)×後方(+)=+、ハイペース(+)×前方(-)=-、スロー(-)×前方(-)=+
        pace_advantage = pace_z * style_centered * 4.0

        rows.append({
            "race_id": rid,
            "horse_number": h.number,
            "pace_self_pos": round(s, 4),
            "pace_front_count": float(front_count),
            "pace_pressure": round(pace_pressure, 3),
            "pace_lone_front": round(lone, 4),
            "pace_advantage": round(pace_advantage, 4),
            # 補助 (使わないが診断用): closer_count
            "_closer_count": float(closer_count),
        })
    return rows


def build_pace_features(workers: int = 8, force: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not force:
        return pd.read_parquet(CACHE)
    rids = sorted({p.name.split("-shutuba")[0] for p in RAW.glob("*-shutuba.html.gz")})
    print(f"building pace features for {len(rids):,} races ...", flush=True)
    all_rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(_one, rids, chunksize=20):
            all_rows.extend(rows)
    df = pd.DataFrame(all_rows)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE, index=False)
    print(f"saved {CACHE} — rows={len(df):,}", flush=True)
    return df


# ----------------------------------------------------------------------------
# β-MLE / de-vig / 3-fold (test_speed_v2.py から流用)
# ----------------------------------------------------------------------------
def _ri(rid):
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _softmax(x, t):
    z = np.asarray(x) / max(t, 1e-6)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def _devig(odds):
    raw = 1.0 / np.asarray(odds, float)
    raw = raw / raw.sum()
    d = power_method_overround({i: float(raw[i]) for i in range(len(raw))})
    v = np.array([d[i] for i in range(len(raw))], float)
    s = v.sum()
    return v / s if s > 0 else raw


def _races(df, feats):
    out = []
    for _rid, g in df.groupby("race_id", sort=False):
        g = g[g["win_odds"] > 0]
        if len(g) < 3 or g["target_top1"].sum() != 1:
            continue
        out.append({
            "X": g[feats].values,
            "odds": g["win_odds"].to_numpy(float),
            "winner": int(np.argmax(g["target_top1"].to_numpy())),
        })
    return out


def _fit_T(b, races):
    best_T, best = 0.5, 1e18
    for T in [0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.5]:
        ll = sum(-np.log(max(_softmax(b.predict(r["X"]), T)[r["winner"]], 1e-12)) for r in races)
        if ll < best:
            best, best_T = ll, T
    return best_T


def _fit_beta(b, races, T):
    pre = [(_softmax(b.predict(r["X"]), T), _devig(r["odds"]), r["winner"]) for r in races]

    def neg(beta):
        s = 0.0
        for mp, mk, w in pre:
            z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
            z = z - z.max()
            e = np.exp(z)
            bp = e / e.sum()
            s -= np.log(max(bp[w], 1e-12))
        return s
    return float(minimize_scalar(neg, bounds=(0, 1), method="bounded").x)


def _eval(b, races, T, beta):
    sw_p = sw_s = mk_p = mk_s = sw_hit = mk_hit = ll = mll = 0.0
    for r in races:
        mp = _softmax(b.predict(r["X"]), T)
        mk = _devig(r["odds"])
        z = (1 - beta) * np.log(np.clip(mp, 1e-9, None)) + beta * np.log(np.clip(mk, 1e-9, None))
        z = z - z.max()
        bp = np.exp(z)
        bp = bp / bp.sum()
        w = r["winner"]
        odds = r["odds"]
        ll -= np.log(max(bp[w], 1e-12))
        mll -= np.log(max(mk[w], 1e-12))
        top = int(np.argmax(bp))
        sw_s += 100
        if top == w:
            sw_hit += 1
            sw_p += 100 * odds[w]
        tm = int(np.argmax(mk))
        mk_s += 100
        if tm == w:
            mk_hit += 1
            mk_p += 100 * odds[w]
    n = len(races)
    return {"roi": sw_p / sw_s * 100 if sw_s else 0, "hit": int(sw_hit), "n": n,
            "ll": ll / max(n, 1), "mll": mll / max(n, 1),
            "mkt_roi": mk_p / mk_s * 100 if mk_s else 0, "mkt_hit": int(mk_hit)}


def run(df, feats, label, seed):
    rids = sorted(df["race_id"].unique().tolist(), key=_ri)
    n = len(rids)
    A = set(rids[:int(n * .6)])
    B = set(rids[int(n * .6):int(n * .8)])
    C = set(rids[int(n * .8):])
    da = df[df.race_id.isin(A)].sort_values(["race_id", "horse_number"])
    db = df[df.race_id.isin(B)].sort_values(["race_id", "horse_number"])
    dc = df[df.race_id.isin(C)].sort_values(["race_id", "horse_number"])
    ga = da.groupby("race_id", sort=False).size().to_numpy()
    gb = db.groupby("race_id", sort=False).size().to_numpy()
    p = dict(PARAMS)
    p["seed"] = seed
    dtr = lgb.Dataset(da[feats].values, label=da["target_rank"].values, group=ga)
    dva = lgb.Dataset(db[feats].values, label=db["target_rank"].values, group=gb, reference=dtr)
    b = lgb.train(p, dtr, num_boost_round=800, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)])
    rb, rc = _races(db, feats), _races(dc, feats)
    T = _fit_T(b, rb)
    beta = _fit_beta(b, rb, T)
    ev = _eval(b, rc, T, beta)
    # pace 特徴量の gain (重要度) を出す
    gains = dict(zip(b.feature_name(), b.feature_importance(importance_type="gain")))
    pace_gain = sum(gains.get(f"Column_{feats.index(f)}", gains.get(f, 0)) for f in PACE_FEATS if f in feats)
    total_gain = sum(gains.values()) or 1.0
    pace_share = pace_gain / total_gain * 100
    print(f"  {label:>9}: β-MLE={beta:.3f} T={T:.2f} | holdout 単勝 {ev['roi']:.1f}% "
          f"vs 市場 {ev['mkt_roi']:.1f}% (hit {ev['hit']}/{ev['n']}) | "
          f"ll {ev['ll']:.4f} (市場 {ev['mll']:.4f})"
          + (f" | pace gain {pace_share:.1f}%" if any(f in feats for f in PACE_FEATS) else ""),
          flush=True)
    return beta, ev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default="nar", choices=["all", "nar", "jra"])
    ap.add_argument("--seeds", type=int, default=3, help="LGBM seed を変えて平均 (β安定性)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true", help="pace 特徴量を再計算")
    args = ap.parse_args()

    pace = build_pace_features(workers=args.workers, force=args.force)
    df = pd.read_parquet(ALL)
    df = df.merge(pace.drop(columns=[c for c in pace.columns if c.startswith("_")]),
                  on=["race_id", "horse_number"], how="left")
    for c in PACE_FEATS:
        df[c] = df[c].fillna(0.0)

    if args.segment != "all":
        isj = df.race_id.astype(str).str[4:6].isin(JRA)
        df = df[isj if args.segment == "jra" else ~isj]
        if args.segment == "nar":
            df = df[df["surface"] == "ダート"]   # 課題指定: NAR ダート

    df = df[df.race_id.isin(
        df.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index)]
    nr = df.race_id.nunique()
    print(f"\n=== 展開(ペース)予測 検証 [{args.segment}{'/ダート' if args.segment=='nar' else ''}] "
          f"races={nr:,} seeds={args.seeds} ===")
    print("  β-MLE が 1.0 = モデルは市場に何も足せない。1.0 未満 = 直交情報で市場を上回る。")
    # pace 特徴量の分布診断
    fc = pace["pace_front_count"]
    print(f"  pace_front_count 分布: mean={fc.mean():.2f} "
          f"(0頭 {(fc==0).mean()*100:.0f}% / 1頭 {(fc==1).mean()*100:.0f}% / "
          f"2頭 {(fc==2).mean()*100:.0f}% / 3+頭 {(fc>=3).mean()*100:.0f}%)\n", flush=True)

    bb_list, bv_list = [], []
    base_ev, pace_ev = None, None
    for sd in range(args.seeds):
        if args.seeds > 1:
            print(f"  --- seed {sd} ---", flush=True)
        bb, eb = run(df, BASE_FEATS, "baseline", sd)
        bv, ev = run(df, BASE_FEATS + PACE_FEATS, "+pace", sd)
        bb_list.append(bb)
        bv_list.append(bv)
        base_ev, pace_ev = eb, ev

    mb, mv = float(np.mean(bb_list)), float(np.mean(bv_list))
    sb, sv = float(np.std(bb_list)), float(np.std(bv_list))
    print(f"\n  → β-MLE (seed 平均): baseline {mb:.3f}±{sb:.3f} → +pace {mv:.3f}±{sv:.3f}")
    verdict = ("改善 (市場超え方向 = 展開予測が edge)" if mv < mb - 0.02
               else "変化なし = 展開予測は市場に織り込み済 (edge にならず)")
    print(f"     {verdict}")
    print(f"  → hold-out 単勝 ROI (最終 seed): baseline {base_ev['roi']:.1f}% / "
          f"+pace {pace_ev['roi']:.1f}% / 市場 {base_ev['mkt_roi']:.1f}%")
    print(f"  → hold-out log-loss: baseline {base_ev['ll']:.4f} / +pace {pace_ev['ll']:.4f} / "
          f"市場 {base_ev['mll']:.4f} (低いほど良)")
    print(f"\n  注: N(hold-out)={base_ev['n']} race。β は別 partition(B) で MLE 凍結 = overfit 回避済だが、")
    print("  単発の ROI 差は分散大。β-MLE が baseline と同じ ~1.0 なら展開特徴は市場の直交情報でない。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
