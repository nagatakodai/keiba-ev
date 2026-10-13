"""タイム換算比較 (src/time_eval.py, te1) の walk-forward バックテスト (読み取り専用)。

本番と同じコード (time_model.fit + time_eval.evaluate) を、data/raw の netkeiba キャッシュ
(2026-01〜05, JRA/地方平地) で月単位に検証する。月 M のレースは「M の 1 日より前」の ledger だけで
学習したモデルで換算する (評価レース自身の時計は学習に入らない)。

  python scripts/time_eval_backtest.py [--workers 12] [--no-day]

指標 (推定タイムが小さいほど上位):
  AUCw  レース内で勝ち馬が他馬より速く推定された割合 (0.5=ランダム)
  AUC3  同 3 着以内
  ρtime 推定タイムと実際の走破時計の順位相関 (レース平均)
  MADdm レース中央値を除いた推定と実時計の差の中央値 (秒, 推定は 0.1 秒丸めなので 0.05 秒刻み)
  MAE   レース平均を除いた推定と実時計の差の平均 (秒, 連続量 — 条件を比べるときはこちら)
  top1  推定 1 位の勝率 / ROI 推定 1 位の単勝 ¥100 均等買い (確定オッズ)
  fav   市場 1 番人気 (確定オッズ最小) の勝率 / ROI
2026-10-10 実測 (選定 1-3月 / 確認 4-5月): 地方 AUCw 0.79/0.77, JRA 0.72/0.72。市場 1 番人気の top1
(地方 47% / JRA 32%) には届かない = 単独で市場に勝つ指標ではなく、比較・補強用。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src import time_eval as te_mod  # noqa: E402
from src import time_model as tm  # noqa: E402

MONTHS = [(20260101, 20260201), (20260201, 20260301), (20260301, 20260401),
          (20260401, 20260501), (20260501, 20260601)]
TUNE = {20260101, 20260201, 20260301}
JRA_CODES = {f"{i:02d}" for i in range(1, 11)}

_MODEL: dict | None = None


def _init(model: dict) -> None:
    global _MODEL
    _MODEL = model


def _one(rid: str) -> dict | None:
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
    fo = (res or {}).get("final_odds") or {}
    if not times or not res or not res.get("finish_order"):
        return None
    te = te_mod.evaluate(rd, _MODEL)
    if not te.get("available"):
        return {"rid": rid, "available": False}
    est = {h["number"]: h["est_sec"] for h in te["horses"] if h["est_sec"] is not None}
    # 推定 1 位は time_eval の順位 (丸める前の推定で付けた rank==1)。丸めた est の最小 + 馬番で選ぶと
    # 0.1 秒単位の同タイムで別の馬になる (1 割のレース)
    top = next((h["number"] for h in te["horses"] if h.get("rank") == 1), None)
    odds = {h.number: h.win_odds for h in rd.race.horses if not h.absent and h.win_odds}
    runners = [h.number for h in rd.race.horses if not h.absent and h.number in times]
    return {"rid": rid, "available": True, "est": est, "top": top, "times": times, "runners": runners,
            "winner": res["finish_order"][0], "top3": res["finish_order"][:3],
            "win_payout": fo.get(f"win:{res['finish_order'][0]}"), "odds": odds}


def _auc(sc, lab):
    w = [s for s, f in zip(sc, lab) if f]
    o = [s for s, f in zip(sc, lab) if not f]
    if not w or not o:
        return None
    return sum((1.0 if a < b else 0.5 if a == b else 0.0) for a in w for b in o) / (len(w) * len(o))


def _metrics(rows: list[dict]) -> dict:
    A = defaultdict(list)
    for r in rows:
        if not r or not r.get("available"):
            continue
        est, runners = r["est"], r["runners"]
        if len(runners) < 5 or len(est) < 3:
            continue
        med = float(np.median(list(est.values())))
        sc = [est.get(n, med) for n in runners]
        a = _auc(sc, [n == r["winner"] for n in runners])
        if a is not None:
            A["aucw"].append(a)
        a3 = _auc(sc, [n in r["top3"] for n in runners])
        if a3 is not None:
            A["auc3"].append(a3)
        pairs = [(est[n], r["times"][n]) for n in runners if n in est]
        if len(pairs) >= 4:
            pe = np.array([p[0] for p in pairs])
            ac = np.array([p[1] for p in pairs])
            A["mad"].extend(np.abs((pe - np.median(pe)) - (ac - np.median(ac))).tolist())
            A["mae"].extend(np.abs((pe - pe.mean()) - (ac - ac.mean())).tolist())
            ra, rb = np.argsort(np.argsort(pe)), np.argsort(np.argsort(ac))
            if np.std(ra) and np.std(rb):
                A["rho"].append(float(np.corrcoef(ra, rb)[0, 1]))
        top = r.get("top") or min(est, key=lambda n: (est[n], n))
        A["top1"].append(1.0 if top == r["winner"] else 0.0)
        A["pay"].append(100.0 * (r["win_payout"] or 0.0) if top == r["winner"] else 0.0)
        if r["odds"]:
            fav = min(r["odds"], key=lambda n: r["odds"][n])
            A["fav"].append(1.0 if fav == r["winner"] else 0.0)
            A["favpay"].append(100.0 * (r["win_payout"] or 0.0) if fav == r["winner"] else 0.0)
        A["races"].append(1)
    m = lambda k: float(np.mean(A[k])) if A[k] else float("nan")  # noqa: E731
    return {"races": len(A["races"]), "aucw": m("aucw"), "auc3": m("auc3"), "rho": m("rho"),
            "mad": float(np.median(A["mad"])) if A["mad"] else float("nan"), "mae": m("mae"), "top1": m("top1"),
            "roi": float(np.sum(A["pay"]) / max(1, 100 * len(A["pay"])) * 100), "fav": m("fav"),
            "favroi": float(np.sum(A["favpay"]) / max(1, 100 * len(A["favpay"])) * 100)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--no-day", action="store_true", help="開催日の馬場差 δ を使わない (比較用)")
    ap.add_argument("--with-official", action="store_true",
                    help="学習 ledger に地方の公式データ (nar_official-*.jsonl) も入れる (中央・地方とも評価月の直前まで"
                         "揃った状態。本番の中央データ停止 2026-05-24〜 = 中央の馬場差なしは再現しない)")
    args = ap.parse_args()
    from src.dataset import _iter_race_ids
    raw = tm.LEDGER_DIR / "netkeiba_raw.jsonl"
    if not raw.exists():
        print("netkeiba_raw.jsonl がありません: python scripts/build_time_model.py --extract-raw")
        return 1
    # live は使わない (data/raw の期間内で完結させる)。--with-official は公式データ (評価月より前の行だけが
    # 学習に入る) を足す。公式は netkeiba と重なる走で時計・馬場・勝ち時計が完全一致 (2026-10-13 確認)。
    files = ([*sorted(tm.LEDGER_DIR.glob("nar_official-*.jsonl"))] if args.with_official else []) + [raw]
    ledger = tm.load_ledger(files)
    rids = [r for r in _iter_race_ids() if r[4:6] != "65"]
    by_month: dict[tuple, list[str]] = defaultdict(list)
    for rid in rids:
        d = int(rid[:4]) * 10000
        if rid[4:6] not in JRA_CODES:       # NAR rid は日付入り
            d = int(rid[:4] + rid[6:10])
            by_month[next((m for m in MONTHS if m[0] <= d < m[1]), None)].append(rid)
    # JRA rid は日付を持たないので load して日付を取る (軽量: shutuba だけで足りるが load_race を流用)
    from src.dataset import _race_date, load_race
    for rid in rids:
        if rid[4:6] in JRA_CODES:
            try:
                loaded = load_race(rid)
                d = int(_race_date(rid, loaded[0]) or 0) if loaded else 0
            except Exception:  # noqa: BLE001
                d = 0
            by_month[next((m for m in MONTHS if m[0] <= d < m[1]), None)].append(rid)
    out: dict[tuple, list] = defaultdict(list)
    for lo, hi in MONTHS:
        sub = [r for r in ledger if r["d"] < lo]
        model = tm.fit(sub, day_effects=not args.no_day)
        month_rids = by_month.get((lo, hi), [])
        with ProcessPoolExecutor(max_workers=args.workers, initializer=_init, initargs=(model,)) as ex:
            for r in ex.map(_one, month_rids, chunksize=8):
                if r:
                    seg = "jra" if r["rid"][4:6] in JRA_CODES else "nar"
                    out[("tune" if lo in TUNE else "test", seg)].append(r)
        print(f"month {lo}: {len(month_rids)} races (model runs={model.get('n_runs'):,})", flush=True)
    print(f"\n{'win':<5}{'seg':<4}{'races':>6}{'AUCw':>7}{'AUC3':>7}{'ρtime':>7}{'MADdm':>7}{'MAE':>7}{'top1':>7}{'ROI':>7}{'fav1':>7}{'favROI':>8}")
    for (win, seg), rows in sorted(out.items(), key=lambda kv: (kv[0][0] != "tune", kv[0][1])):
        m = _metrics(rows)
        print(f"{win:<5}{seg:<4}{m['races']:>6}{m['aucw']:>7.3f}{m['auc3']:>7.3f}{m['rho']:>7.3f}{m['mad']:>7.3f}"
              f"{m['mae']:>7.3f}{m['top1']:>7.3f}{m['roi']:>7.1f}{m['fav']:>7.3f}{m['favroi']:>8.1f}")
    unav = sum(1 for rows in out.values() for r in rows if not r.get("available"))
    print(f"(available=False races: {unav})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
