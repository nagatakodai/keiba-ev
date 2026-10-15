"""タイム換算比較 te2 の集約パラメータ / 場別馬場差モデルの walk-forward スイープ (読み取り専用)。

2026-10-14 ユーザ指示「芝ダ・距離の換算は弱めに / 馬場込みの同条件を最重視 / 同条件の無い馬は換算で評価するが弱めに /
ダートの道悪が速い性質をしっかり考える」で設計を変えた集約 (src/time_eval.py) と、場別の馬場状態効果 γ_v
(src/time_model.py VENUE_GOING/K_GOING) を、本番と同じコードで検証する。各レースは 1 回だけ読み込み、設定ごとに
evaluate だけやり直す (設定は time_eval.set_params / time_model の定数差し替え)。

  python scripts/time_eval_agg_sweep.py --source netkeiba [--workers 12] [--configs a,b,...]
      data/raw の netkeiba キャッシュ 2026-01〜05 (選定 01-03 / 確認 04-05, JRA と地方)。学習 = その月の 1 日より前の ledger。
  python scripts/time_eval_agg_sweep.py --source official --months 202510,202511,202512
      地方の公式データ (scripts/time_eval_official_backtest.py と同じ母集団・日次再学習・地方のみ)。

出力: 設定ごとの AUCw/AUC3/MAE/top1/ROI と、基準設定 (--baseline, 既定 old) に対する差の 95%CI
(レース単位 paired bootstrap)。指標の定義は scripts/time_eval_backtest.py と同じ。

結果 (2026-10-15, 選定 01-03 → 確認 04-05 + 公式 2025-10〜12 / 2026-06〜09): 新しい集約は時計の誤差 (MAE) を
全期間で改善する一方、勝ち馬 AUC は換算を弱くするほど単調に下がる (conv-weak < conv-strong < conv-mild ≒ old)。
中央値寄せ SHRINK_W0 も 0.3 が MAE 最良で、大きくすると AUC が下がる。場別の馬場差 γ_v (old+vg) は AUC/MAE とも
中立 (当レースの γ は全馬共通なので順位に効かず、過去走の δ 既知の日は再中心化で合計が変わらないため)。
→ 既定は conv-mild (NEW_PARAMS)。数表は CLAUDE.md の te2 節。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import time_eval as te_mod  # noqa: E402
from src import time_model as tm  # noqa: E402

# ---- 集約の設定 (old = 2026-10-10 の te1 と同じ重み) ----
OLD_PARAMS = {"EST_RUNS": 5, "RECENCY_DECAY": 0.85,
              "KIND_W": {"same": 1.15, "same_course": 1.15, "same_dist": 1.0, "distance": 1.0, "surface": 0.35},
              "DIST_SCALE": 0.2, "SAME_VENUE_W": 1.15, "SHRINK_W0": 0.0, "STRONG_W": 1.0,
              "NEAR_DIST": 0.10, "FIT_SEC": 0.3}
# new = 本番の te2 既定 (2026-10-15 選定: conv-mild)。2026-10-14 の初期値 (distance 0.5 / surface 0.15 / DIST_SCALE 0.15)
# は new/conv-strong として残す。
NEW_PARAMS = {"EST_RUNS": 8, "RECENCY_DECAY": 0.85,
              "KIND_W": {"same": 1.0, "same_course": 0.8, "same_dist": 0.6, "distance": 0.7, "surface": 0.25},
              "DIST_SCALE": 0.2, "SAME_VENUE_W": 1.15, "SHRINK_W0": 0.3, "STRONG_W": 1.0,
              "NEAR_DIST": 0.10, "FIT_SEC": 0.3}


def _p(**over):
    out = json.loads(json.dumps(NEW_PARAMS))
    for k, v in over.items():
        if k == "KIND_W":
            out["KIND_W"].update(v)
        else:
            out[k] = v
    return out


CONFIGS: dict[str, dict] = {
    # 旧集約 × 旧モデル / 新モデル (モデルの効果だけを分離)
    "old": {"params": OLD_PARAMS, "model": "old"},
    "old+vg": {"params": OLD_PARAMS, "model": "v30"},
    # 新集約 (既定) とモデルの縮約 K
    "new": {"params": NEW_PARAMS, "model": "v30"},
    "new/k10": {"params": NEW_PARAMS, "model": "v10"},
    "new/k100": {"params": NEW_PARAMS, "model": "v100"},
    "new/oldmodel": {"params": NEW_PARAMS, "model": "old"},
    # 中央値寄せの強さ
    "new/w0=0": {"params": _p(SHRINK_W0=0.0), "model": "v30"},
    "new/w0=0.6": {"params": _p(SHRINK_W0=0.6), "model": "v30"},
    "new/w0=1.0": {"params": _p(SHRINK_W0=1.0), "model": "v30"},
    # 使う走数
    "new/est5": {"params": _p(EST_RUNS=5), "model": "v30"},
    "new/est12": {"params": _p(EST_RUNS=12), "model": "v30"},
    # 換算 (距離・芝ダ) をさらに弱く (conv-strong = 2026-10-14 の初期値 / conv-weak = さらに弱く)
    "new/conv-strong": {"params": _p(KIND_W={"distance": 0.5, "surface": 0.15}, DIST_SCALE=0.15), "model": "v30"},
    "new/conv-weak": {"params": _p(KIND_W={"distance": 0.35, "surface": 0.10}, DIST_SCALE=0.12), "model": "v30"},
    # 同コース (別馬場) / 他場同距離 の重さ
    "new/course0.6": {"params": _p(KIND_W={"same_course": 0.6, "same_dist": 0.45}), "model": "v30"},
    "new/course1.0": {"params": _p(KIND_W={"same_course": 1.0, "same_dist": 0.8}), "model": "v30"},
}

MODEL_VARIANTS = {"old": {"VENUE_GOING": False, "K_GOING": 30.0},
                  "v30": {"VENUE_GOING": True, "K_GOING": 30.0},
                  "v10": {"VENUE_GOING": True, "K_GOING": 10.0},
                  "v100": {"VENUE_GOING": True, "K_GOING": 100.0}}


def fit_variants(rows: list[dict], variants: set[str]) -> dict[str, dict]:
    out = {}
    for v in sorted(variants):
        for k, val in MODEL_VARIANTS[v].items():
            setattr(tm, k, val)
        out[v] = tm.fit(rows)
    for k, val in MODEL_VARIANTS["v30"].items():
        setattr(tm, k, val)
    return out


def _auc(sc, lab):
    w = [s for s, f in zip(sc, lab) if f]
    o = [s for s, f in zip(sc, lab) if not f]
    if not w or not o:
        return None
    return sum((1.0 if a < b else 0.5 if a == b else 0.0) for a in w for b in o) / (len(w) * len(o))


def score(te: dict, runners: list[int], winners: set[int], top3: set[int], times: dict[int, float],
          pay: dict[int, float]) -> dict | None:
    """1 レース × 1 設定の評価値 (est 3 頭未満 / 5 頭未満は None)。"""
    if not te.get("available"):
        return None
    est = {h["number"]: h["est_sec"] for h in te["horses"] if h["est_sec"] is not None}
    if len(runners) < 5 or len(est) < 3:
        return None
    med = float(np.median(list(est.values())))
    sc = [est.get(n, med) for n in runners]
    out = {"aucw": _auc(sc, [n in winners for n in runners]), "auc3": _auc(sc, [n in top3 for n in runners])}
    pairs = [(est[n], times[n]) for n in runners if n in est and n in times]
    if len(pairs) >= 4:
        pe, ac = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        out["mae"] = float(np.mean(np.abs((pe - pe.mean()) - (ac - ac.mean()))))
    top = next((h["number"] for h in te["horses"] if h.get("rank") == 1), None) or min(est, key=lambda n: (est[n], n))
    out["top1"] = 1.0 if top in winners else 0.0
    out["pay"] = float(pay.get(top, 0.0)) if top in winners else 0.0
    return out


# ------------------------------------------------------------ netkeiba (data/raw) ----

_G: dict = {}


def _init(models: dict, configs: dict) -> None:
    _G["models"], _G["configs"] = models, configs


def _one_multi(rid: str) -> dict | None:
    from scripts.build_time_model import _result_times
    from src.dataset import load_race
    try:
        loaded = load_race(rid)
    except Exception:  # noqa: BLE001
        return None
    if loaded is None:
        return None
    rd, res = loaded
    times = _result_times(rid)
    if not times or not res or not res.get("finish_order"):
        return None
    fo = (res or {}).get("final_odds") or {}
    runners = [h.number for h in rd.race.horses if not h.absent and h.number in times]
    winners = {res["finish_order"][0]}
    top3 = set(res["finish_order"][:3])
    pay = {res["finish_order"][0]: 100.0 * float(fo.get(f"win:{res['finish_order'][0]}") or 0.0)}
    out = {}
    for name, cfg in _G["configs"].items():
        te_mod.set_params(**cfg["params"])
        te = te_mod.evaluate(rd, _G["models"][cfg["model"]])
        out[name] = score(te, runners, winners, top3, times, pay)
    return {"rid": rid, "scores": out}


def run_netkeiba(configs: dict, workers: int) -> dict[tuple, dict[str, list]]:
    from scripts.time_eval_backtest import JRA_CODES, MONTHS, TUNE
    from src.dataset import _iter_race_ids, _race_date, load_race
    raw = tm.LEDGER_DIR / "netkeiba_raw.jsonl"
    files = [*sorted(tm.LEDGER_DIR.glob("nar_official-*.jsonl")), raw]
    ledger = tm.load_ledger(files)
    rids = [r for r in _iter_race_ids() if r[4:6] != "65"]
    by_month: dict[tuple, list[str]] = defaultdict(list)
    for rid in rids:
        if rid[4:6] not in JRA_CODES:
            d = int(rid[:4] + rid[6:10])
            by_month[next((m for m in MONTHS if m[0] <= d < m[1]), None)].append(rid)
        else:
            try:
                loaded = load_race(rid)
                d = int(_race_date(rid, loaded[0]) or 0) if loaded else 0
            except Exception:  # noqa: BLE001
                d = 0
            by_month[next((m for m in MONTHS if m[0] <= d < m[1]), None)].append(rid)
    variants = {c["model"] for c in configs.values()}
    out: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for lo, hi in MONTHS:
        t0 = time.time()
        models = fit_variants([r for r in ledger if r["d"] < lo], variants)
        month_rids = by_month.get((lo, hi), [])
        with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(models, configs)) as ex:
            for r in ex.map(_one_multi, month_rids, chunksize=8):
                if not r:
                    continue
                seg = "jra" if r["rid"][4:6] in JRA_CODES else "nar"
                win = "tune" if lo in TUNE else "test"
                for name, sc in r["scores"].items():
                    out[(win, seg)][name].append((r["rid"], sc))
        print(f"month {lo}: {len(month_rids)} races ({time.time() - t0:.0f}s)", flush=True)
    return out


# ------------------------------------------------------------ official (地方) ----

def run_official(configs: dict, months: list[str], history_from: str) -> dict[tuple, dict[str, list]]:
    from src import nar_official as no
    from scripts.time_eval_official_backtest import (build_histories, eval_races, jra_ledger_histories,
                                                      ledger_for, load_official, race_data)
    hist_months = no.months_between(history_from, max(months))
    races, entries, pays = load_official(hist_months)
    hist = build_histories(races, entries, jra_ledger_histories())
    evr = eval_races(races, entries, pays, months)
    ledger = ledger_for("nk+off")
    variants = {c["model"] for c in configs.values()}
    by_day: dict[int, list[dict]] = defaultdict(list)
    for er in evr:
        by_day[er["info"]["date"]].append(er)
    out: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    label = f"off{months[0][2:]}-{months[-1][2:]}"
    t0 = time.time()
    n_fit = -1
    models: dict[str, dict] = {}
    for k, d in enumerate(sorted(by_day)):
        sub = [r for r in ledger if r["d"] < d]
        if len(sub) != n_fit:
            models = fit_variants(sub, variants)
            n_fit = len(sub)
        for er in by_day[d]:
            rd = race_data(er, hist)
            runners = [x["num"] for x in er["runners"]]
            winners = {x["num"] for x in er["runners"] if x["fin"] == 1}
            top3 = {x["num"] for x in er["runners"] if x["fin"] is not None and x["fin"] <= 3}
            times = {x["num"]: x["sec"] for x in er["runners"] if x["sec"]}
            pay = {n: float(y) for n, y in er["pays"].items()}
            rid = "|".join(str(x) for x in er["key"])
            for name, cfg in configs.items():
                te_mod.set_params(**cfg["params"])
                te = te_mod.evaluate(rd, models[cfg["model"]])
                out[(label, "nar")][name].append((rid, score(te, runners, winners, top3, times, pay)))
        if (k + 1) % 20 == 0:
            print(f"  day {d}: {k + 1}/{len(by_day)} ({time.time() - t0:.0f}s)", flush=True)
    return out


# ------------------------------------------------------------ report ----

def _agg(rows: list[tuple[str, dict | None]]) -> dict:
    A = defaultdict(list)
    for _rid, s in rows:
        if not s:
            continue
        A["races"].append(1)
        for k in ("aucw", "auc3", "mae", "top1", "pay"):
            if s.get(k) is not None:
                A[k].append(s[k])
    m = lambda k: float(np.mean(A[k])) if A[k] else float("nan")  # noqa: E731
    return {"races": len(A["races"]), "aucw": m("aucw"), "auc3": m("auc3"), "mae": m("mae"), "top1": m("top1"),
            "roi": float(np.sum(A["pay"]) / max(1, 100 * len(A["top1"])) * 100)}


def _paired(base: list, other: list, key: str, nboot: int = 2000) -> tuple[float, float, float, int]:
    b = {rid: s[key] for rid, s in base if s and s.get(key) is not None}
    o = {rid: s[key] for rid, s in other if s and s.get(key) is not None}
    common = sorted(set(b) & set(o))
    if len(common) < 10:
        return float("nan"), float("nan"), float("nan"), len(common)
    d = np.array([o[r] - b[r] for r in common])
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(d), size=(nboot, len(d)))
    bs = d[idx].mean(axis=1)
    return float(d.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), len(common)


def report(out: dict[tuple, dict[str, list]], baseline: str) -> None:
    for (win, seg), by_cfg in sorted(out.items(), key=lambda kv: (kv[0][0] != "tune", kv[0])):
        print(f"\n== {win} {seg} ==")
        print(f"{'config':<16}{'races':>6}{'AUCw':>7}{'AUC3':>7}{'MAE':>7}{'top1':>7}{'ROI':>7}"
              f"{'ΔAUCw [95%CI]':>24}{'Δtop1pt [95%CI]':>24}{'ΔMAE [95%CI]':>22}")
        base = by_cfg.get(baseline, [])
        for name, rows in by_cfg.items():
            m = _agg(rows)
            line = (f"{name:<16}{m['races']:>6}{m['aucw']:>7.3f}{m['auc3']:>7.3f}{m['mae']:>7.3f}"
                    f"{m['top1']:>7.3f}{m['roi']:>7.1f}")
            if name != baseline and base:
                da, lo, hi, _n = _paired(base, rows, "aucw")
                dt, tlo, thi, _n = _paired(base, rows, "top1")
                dm, mlo, mhi, _n = _paired(base, rows, "mae")
                line += (f"  {da:+.4f} [{lo:+.4f},{hi:+.4f}]  {100 * dt:+.2f} [{100 * tlo:+.2f},{100 * thi:+.2f}]"
                         f"  {dm:+.3f} [{mlo:+.3f},{mhi:+.3f}]")
            print(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("netkeiba", "official"), default="netkeiba")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--configs", default="all")
    ap.add_argument("--baseline", default="old")
    ap.add_argument("--months", default="202606,202607,202608,202609")
    ap.add_argument("--history-from", default="202504")
    ap.add_argument("--dump", default=None, help="レース×設定の評価値を JSON に保存")
    args = ap.parse_args()
    names = list(CONFIGS) if args.configs == "all" else args.configs.split(",")
    configs = {n: CONFIGS[n] for n in names}
    if args.baseline not in configs:
        configs = {args.baseline: CONFIGS[args.baseline], **configs}
    t0 = time.time()
    if args.source == "netkeiba":
        out = run_netkeiba(configs, args.workers)
    else:
        out = run_official(configs, args.months.split(","), args.history_from)
    print(f"done in {time.time() - t0:.0f}s")
    report(out, args.baseline)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump({f"{w}|{s}": {c: rows for c, rows in by.items()} for (w, s), by in out.items()}, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
