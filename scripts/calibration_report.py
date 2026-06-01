"""確率モデルの calibration 分析と再校正。

data/datasets/all.parquet の chronological split (last 20% = valid、残り = train)
で、LightGBM lambdarank の race 内 softmax win 確率 (model-only) と
β=0.78 loglinear 市場ブレンド win 確率を計算し、

  1. reliability diagram (10 bin) + ECE + Brier (model-only / blended)
  2. train で isotonic regression / Platt scaling を予測確率→実勝率にフィット
     → valid に適用 (race 内で再正規化 Σ=1) → ECE/Brier の before/after
  3. top-1 単勝 ROI の校正前後比較

を出す。良い方を data/models/win_calibrator.pkl に保存。

⚠ **重要 (検証レビュー指摘)**:
- win_calibrator.pkl は **本番 (ev.py) から未参照の実験成果物** (orphan)。estimate_probs に
  校正を挟む配線はまだ無い。本番投入は base='blend_p' を本番の市場ブレンド経路
  (trifecta-marginalized) で再現できると確認してから別途行う。
- Platt/isotonic は **単調変換**なので各 race の argmax (= 単勝 top-1 選抜) を変えない →
  単勝 ROI は不変。効くのは確率の絶対値 = 3連単 sharpness / Kelly 配分 / トリガミ判定のみ。
  「校正で的中率が上がる」わけではない。
- valid は本番 LGBM の early-stopping valid と同一窓のため報告 ECE/Brier はやや楽観側。
  単一窓 in-sample なので arm 前に sliding-window で再現性確認が望ましい。

本番コード (src/*.py) は触らない。読込・split・blend は src/ev.py /
src/eval_holdout.py の実装に合わせている。

  .venv/bin/python scripts/calibration_report.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT / "data" / "datasets"
MODELS_DIR = ROOT / "data" / "models"

VALID_FRAC = 0.2
BLEND_DEFAULT = 0.78  # src/ev.py BLEND_DEFAULT
EPS = 1e-9


# ---------------------------------------------------------------- split / predict
def _race_id_to_unix(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def _race_softmax(scores: np.ndarray, T: float) -> np.ndarray:
    scaled = scores / max(T, 1e-3)
    ex = np.exp(scaled - scaled.max())
    return ex / ex.sum()


def _market_prob(odds: np.ndarray) -> np.ndarray:
    """1/odds を race 内正規化 (eval_holdout の power_method 簡略版 = 単純正規化)。"""
    raw = np.divide(1.0, odds, out=np.zeros_like(odds, dtype=float), where=odds > 0)
    s = raw.sum()
    return raw / s if s > 0 else raw


def _blend_loglinear(model_p: np.ndarray, market_p: np.ndarray, beta: float) -> np.ndarray:
    alpha = max(1.0 - beta, 0.0)
    logs = alpha * np.log(np.maximum(model_p, EPS)) + beta * np.log(np.maximum(market_p, EPS))
    ex = np.exp(logs - logs.max())
    return ex / ex.sum()


def _renorm_per_race(df: pd.DataFrame, col: str) -> np.ndarray:
    out = df[col].to_numpy(dtype=float).copy()
    for _, idx in df.groupby("race_id", sort=False).groups.items():
        sub = out[df.index.get_indexer(idx)]
        s = sub.sum()
        if s > 0:
            out[df.index.get_indexer(idx)] = sub / s
    return out


# ---------------------------------------------------------------- metrics
def ece_brier(p: np.ndarray, y: np.ndarray, n_bins: int = 10):
    """Expected Calibration Error + Brier + per-bin table を返す。"""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    brier = float(np.mean((p - y) ** 2))
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
    ece = 0.0
    rows = []
    n = len(p)
    for b in range(n_bins):
        mask = bins == b
        cnt = int(mask.sum())
        if cnt == 0:
            rows.append((edges[b], edges[b + 1], 0, float("nan"), float("nan")))
            continue
        conf = float(p[mask].mean())
        acc = float(y[mask].mean())
        ece += (cnt / n) * abs(conf - acc)
        rows.append((edges[b], edges[b + 1], cnt, conf, acc))
    return ece, brier, rows


def print_reliability(label: str, p: np.ndarray, y: np.ndarray):
    ece, brier, rows = ece_brier(p, y)
    print(f"\n=== reliability: {label} ===")
    print(f"  ECE={ece:.4f}  Brier={brier:.5f}  N={len(p)}  mean_p={p.mean():.4f}  base_rate={y.mean():.4f}")
    print(f"  {'bin':>13} {'n':>6} {'pred':>7} {'actual':>7} {'gap':>7}")
    for lo, hi, cnt, conf, acc in rows:
        if cnt == 0:
            continue
        print(f"  {lo:.2f}-{hi:.2f}    {cnt:>6} {conf:>7.3f} {acc:>7.3f} {acc-conf:>+7.3f}")
    return ece, brier


def top1_roi(df: pd.DataFrame, prob_col: str, stake: int = 100):
    """各 race で prob_col 最大の馬に単勝 stake → ROI。"""
    stake_total = 0
    payout_total = 0
    hits = 0
    races = 0
    for _, g in df.groupby("race_id", sort=False):
        gi = g.reset_index(drop=True)
        top = gi[prob_col].idxmax()
        races += 1
        stake_total += stake
        if int(gi.loc[top, "target_top1"]) == 1:
            hits += 1
            payout_total += stake * float(gi.loc[top, "win_odds"])
    roi = payout_total / stake_total if stake_total else 0.0
    return roi, hits, races


# ---------------------------------------------------------------- main
def main():
    meta = json.loads((MODELS_DIR / "lgbm_metadata.json").read_text(encoding="utf-8"))
    feature_cols = list(meta["feature_cols"])
    T = float(meta.get("softmax_temperature") or 0.5)

    df = pd.read_parquet(DATASETS_DIR / "all.parquet")
    rids = sorted(df["race_id"].unique().tolist(), key=_race_id_to_unix)
    n_valid = max(int(len(rids) * VALID_FRAC), 1)
    valid_rids = set(rids[-n_valid:])
    train_rids = set(rids[:-n_valid])

    # finish_pos のある race のみ評価 (eval_holdout と同じ)。
    has_winner = df.groupby("race_id")["target_top1"].sum()
    labeled = set(has_winner[has_winner > 0].index)

    booster = lgb.Booster(model_file=str(MODELS_DIR / "lgbm_lambdarank.txt"))

    def build(rid_set):
        d = df[df["race_id"].isin(rid_set & labeled)].copy()
        d = d.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
        X = d[feature_cols].astype("float64").fillna(0.0).to_numpy()
        d["score"] = booster.predict(X, num_iteration=booster.best_iteration)
        model_p = np.zeros(len(d))
        market_p = np.zeros(len(d))
        blend_p = np.zeros(len(d))
        for _, idx in d.groupby("race_id", sort=False).groups.items():
            pos = d.index.get_indexer(idx)
            mp = _race_softmax(d["score"].to_numpy()[pos], T)
            mk = _market_prob(d["win_odds"].to_numpy()[pos])
            model_p[pos] = mp
            market_p[pos] = mk
            blend_p[pos] = _blend_loglinear(mp, mk, BLEND_DEFAULT)
        d["model_p"] = model_p
        d["market_p"] = market_p
        d["blend_p"] = blend_p
        return d

    train = build(train_rids)
    valid = build(valid_rids)

    print(f"train races={train['race_id'].nunique()} rows={len(train)} | "
          f"valid races={valid['race_id'].nunique()} rows={len(valid)} | T={T} β={BLEND_DEFAULT}")

    y_tr = train["target_top1"].to_numpy(dtype=float)
    y_va = valid["target_top1"].to_numpy(dtype=float)

    # ---- (3) baseline reliability
    ece_model, brier_model = print_reliability("valid model-only", valid["model_p"].to_numpy(), y_va)
    ece_blend, brier_blend = print_reliability("valid blended β=0.78", valid["blend_p"].to_numpy(), y_va)

    # ---- (4) 再校正: train で fit、valid に適用、race 内再正規化
    print("\n" + "#" * 70)
    print("# recalibration (fit on train, apply on valid, renormalize per race)")
    print("#" * 70)

    results = {}  # name -> (ece, brier, calibrated_col_in_valid)

    for base_col in ("model_p", "blend_p"):
        xtr = train[base_col].to_numpy(dtype=float)
        xva = valid[base_col].to_numpy(dtype=float)

        # isotonic
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(xtr, y_tr)
        valid["_iso"] = iso.predict(xva)
        valid["_iso"] = _renorm_per_race(valid, "_iso")
        e, b = print_reliability(f"valid {base_col} + isotonic (renorm)", valid["_iso"].to_numpy(), y_va)
        results[f"{base_col}+isotonic"] = (e, b, valid["_iso"].to_numpy().copy(), iso, base_col, "isotonic")

        # Platt (logistic on logit of p)
        logit = np.log(np.clip(xtr, EPS, 1 - EPS) / (1 - np.clip(xtr, EPS, 1 - EPS)))
        platt = LogisticRegression(C=1e6, solver="lbfgs")
        platt.fit(logit.reshape(-1, 1), y_tr)
        lv = np.log(np.clip(xva, EPS, 1 - EPS) / (1 - np.clip(xva, EPS, 1 - EPS)))
        valid["_platt"] = platt.predict_proba(lv.reshape(-1, 1))[:, 1]
        valid["_platt"] = _renorm_per_race(valid, "_platt")
        e, b = print_reliability(f"valid {base_col} + Platt (renorm)", valid["_platt"].to_numpy(), y_va)
        results[f"{base_col}+platt"] = (e, b, valid["_platt"].to_numpy().copy(), platt, base_col, "platt")

    # ---- (5) ROI 実利チェック (top-1 単勝)
    print("\n" + "#" * 70)
    print("# top-1 単勝 ROI (校正は top-1 の rank を race 内で変えるか)")
    print("#" * 70)
    roi_rows = []
    for name, col in [
        ("model-only", "model_p"),
        ("blended β=0.78", "blend_p"),
    ]:
        roi, h, r = top1_roi(valid, col)
        roi_rows.append((name, roi, h, r))
    # calibrated variants
    for name, (e, b, arr, *_rest) in results.items():
        valid["_tmp_cal"] = arr
        roi, h, r = top1_roi(valid, "_tmp_cal")
        roi_rows.append((name, roi, h, r))
    print(f"  {'strategy':>22} {'ROI':>7} {'hits':>6} {'races':>6}")
    for name, roi, h, r in roi_rows:
        print(f"  {name:>22} {roi*100:>6.1f}% {h:>6} {r:>6}")

    # ---- (6) 良い方を保存 (ECE 優先、tie は Brier)
    print("\n" + "#" * 70)
    print("# selection + save")
    print("#" * 70)
    baselines = {
        "model_p": (ece_model, brier_model),
        "blend_p": (ece_blend, brier_blend),
    }
    # 候補: 校正後の中で ECE 最小、かつ対応する baseline より改善しているもの
    best_name = None
    best = None
    for name, (e, b, arr, transformer, base_col, kind) in results.items():
        be, bb = baselines[base_col]
        improved = e <= be and b <= bb
        tag = "improved" if improved else "no-improve"
        print(f"  {name:>22}  ECE {be:.4f}->{e:.4f}  Brier {bb:.5f}->{b:.5f}  [{tag}]")
        if best is None or e < best[0]:
            best = (e, b, name, transformer, base_col, kind, improved)
            best_name = name

    save_path = MODELS_DIR / "win_calibrator.pkl"
    payload = {
        "kind": best[5],
        "base": best[4],            # "model_p" or "blend_p"
        "transformer": best[3],     # sklearn IsotonicRegression / LogisticRegression
        "beta": BLEND_DEFAULT,
        "softmax_temperature": T,
        "renormalize_per_race": True,
        "valid_ece": best[0],
        "valid_brier": best[1],
        "baseline_ece": baselines[best[4]][0],
        "baseline_brier": baselines[best[4]][1],
        "improved_vs_baseline": bool(best[6]),
        "note": (
            "apply: p_cal = transformer.predict(base_p) [isotonic] or "
            "predict_proba(logit(base_p)) [platt], then renormalize within race. "
            "base='model_p' uses race-softmax(score/T); 'blend_p' uses loglinear β blend."
        ),
    }
    joblib.dump(payload, save_path)
    print(f"\nsaved best='{best_name}' -> {save_path}")
    print(f"  improved_vs_baseline={best[6]}")


if __name__ == "__main__":
    main()
