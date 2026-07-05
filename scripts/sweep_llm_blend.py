"""T_LLM (指数→確率の温度) と llm_blend (Claude 合成重み) の較正 sweep — 読み取り専用。

ユーザ指示 2026-07-05。現行値 (T=25, blend=0.75) は指示ベースの初期値で一度も較正して
いない (CLAUDE.md「arm 前にレース蓄積で sweep 推奨」の宿題)。結果確定済みの指数付き
レースで **勝者 log-loss (proper scoring rule)** を最小化する (T, w) を探す。

再現する合成 (ev._combine_llm_index と同一の数学):
    L = softmax(指数 / T)         (レース内・floor 付き)
    w_i = w · support_mult(support_i)   (Claude 未スコア馬は w_i=0)
    p ∝ exp((1-w_i)·ln f_i + w_i·ln L_i)
fundamental f は **市場 implied (単勝 100/odds 正規化)** で近似する — クリーン MLE で
α≈0 (モデル成分の独立情報はゼロ・モデル≒市場) が確定しているため、市場≈本番の
market-blended fundamental とみなせる。

フィルタ: 市場非依存 era (2026-06-21T19:04:27 以降の採点) + hindsight ガード
(採点時刻 < 発走時刻, start_at 不明は除外) + 指数/市場が 3 頭以上 + 勝者が母集団内。

使い方: .venv/bin/python scripts/sweep_llm_blend.py
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRED_DIR = ROOT / "data" / "predictions"
RESULT_DIR = ROOT / "data" / "results"
JST = ZoneInfo("Asia/Tokyo")
CUTOFF = "2026-06-21T19:04:27"          # 市場非依存 era の開始 (api/store と同値)
V2_SINCE, V3_SINCE = "2026-06-28", "2026-07-01T15:13:17"

T_GRID = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0, 100.0]
W_GRID = [0.0, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0]
_SUPPORT_WEIGHT = {0: 0.0, 1: 0.5, 2: 0.8}   # ev._SUPPORT_WEIGHT のミラー


def _support_mult(s) -> float:
    if s is None:
        return 1.0
    try:
        return _SUPPORT_WEIGHT.get(max(0, int(s)), 1.0)
    except (TypeError, ValueError):
        return 1.0


def _combine(f: dict[int, float], idx: dict[int, float], support: dict[int, int],
             T: float, w: float, floor: float = 1e-3) -> dict[int, float]:
    """ev._combine_llm_index (scale=strength) の (T, w) パラメタ化版。"""
    keys = list(f)
    if not idx or w <= 0:
        return f
    rm = max(idx.values())
    exps = {k: math.exp((idx.get(k, 0.0) - rm) / T) if k in idx else 0.0 for k in keys}
    z = sum(exps.values()) or 1.0
    L = {k: max(exps[k] / z, floor) for k in keys}
    ls = sum(L.values()) or 1.0
    L = {k: v / ls for k, v in L.items()}
    logs = {}
    for k in keys:
        wi = 0.0 if k not in idx else max(min(w * _support_mult(support.get(k)), 1.0), 0.0)
        logs[k] = (1 - wi) * math.log(max(f[k], 1e-9)) + wi * math.log(max(L[k], 1e-9))
    mm = max(logs.values())
    e = {k: math.exp(v - mm) for k, v in logs.items()}
    s = sum(e.values()) or 1.0
    return {k: v / s for k, v in e.items()}


def _load_races() -> list[dict]:
    races = []
    for f in sorted(PRED_DIR.glob("*.json")):
        if f.name.endswith(".llm.json"):
            continue
        rp = RESULT_DIR / f.name
        if not rp.exists():
            continue
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
            result = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        scored = snap.get("llm_scored_at") or ""
        if not scored or scored < CUTOFF:
            continue
        start_at = snap.get("start_at") or 0
        if not start_at:
            continue
        start_iso = dt.datetime.fromtimestamp(start_at, JST).replace(
            tzinfo=None).isoformat(timespec="seconds")
        if scored >= start_iso:
            continue          # hindsight ガード (発走後採点は除外)
        if (snap.get("llm_scale") or "strength") != "strength":
            continue
        idx = {int(k): float(v) for k, v in (snap.get("llm_win_index") or {}).items()}
        mkt = {int(k): float(v) for k, v in (snap.get("market_win_index") or {}).items()
               if float(v) > 0}
        support = {int(k): v for k, v in (snap.get("llm_support") or {}).items()}
        if len(idx) < 3 or len(mkt) < 3:
            continue
        pos = result.get("finish_positions") or {}
        winners = [int(k) for k, r in pos.items() if r == 1] if pos else []
        if not winners:
            fo = [x for x in (result.get("finish_order") or []) if isinstance(x, int)]
            winners = fo[:1]
        if not winners or any(wn not in mkt for wn in winners):
            continue
        z = sum(mkt.values())
        f_mkt = {k: v / z for k, v in mkt.items()}
        version = ("v3" if scored >= V3_SINCE else "v2" if scored >= V2_SINCE else "v1")
        races.append({"f": f_mkt, "idx": idx, "support": support,
                      "winners": winners, "ts": start_at, "version": version})
    races.sort(key=lambda r: r["ts"])
    return races


def _eval(races: list[dict], T: float, w: float) -> tuple[float, float]:
    """(mean winner log-loss, top1 hit rate)。"""
    ll = hits = 0.0
    for r in races:
        p = _combine(r["f"], r["idx"], r["support"], T, w)
        pw = sum(p.get(wn, 0.0) for wn in r["winners"])
        ll += -math.log(max(pw, 1e-9))
        top1 = min(p.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        hits += 1.0 if top1 in r["winners"] else 0.0
    n = len(races) or 1
    return ll / n, hits / n


def main() -> int:
    races = _load_races()
    print(f"対象レース (市場非依存 era・発走前採点・結果確定・指数3頭+): {len(races)}")
    by_v = {}
    for r in races:
        by_v[r["version"]] = by_v.get(r["version"], 0) + 1
    print("バージョン内訳:", dict(sorted(by_v.items())))
    if len(races) < 30:
        print("n が小さすぎる — 蓄積後に再実行を推奨")

    base_ll, base_hit = _eval(races, 25.0, 0.0)
    print(f"\n市場のみ (w=0):        log-loss {base_ll:.4f} / top1 {base_hit:.1%}")
    cur_ll, cur_hit = _eval(races, 25.0, 0.75)
    print(f"現行 (T=25, w=0.75):   log-loss {cur_ll:.4f} / top1 {cur_hit:.1%}")

    print(f"\n== grid (行=T, 列=w) — 勝者 log-loss (小さいほど良い) ==")
    header = "T\\w  " + "".join(f"{w:>8.2f}" for w in W_GRID)
    print(header)
    best = (None, None, float("inf"))
    for T in T_GRID:
        row = [f"{T:>4.0f} "]
        for w in W_GRID:
            ll, _hit = _eval(races, T, w)
            row.append(f"{ll:>8.4f}")
            if ll < best[2]:
                best = (T, w, ll)
        print("".join(row))
    bT, bw, bll = best
    bll2, bhit = _eval(races, bT, bw)
    print(f"\n最良: T={bT:.0f}, w={bw:.2f} → log-loss {bll:.4f} / top1 {bhit:.1%} "
          f"(市場のみ比 {bll - base_ll:+.4f})")

    # 安定性: 前半/後半・バージョン別で最良セルと現行を比較 (bin-selection ガード)。
    half = len(races) // 2
    for name, sub in (("前半", races[:half]), ("後半", races[half:]),
                      *((f"版{v}", [r for r in races if r["version"] == v])
                        for v in sorted(by_v))):
        if len(sub) < 15:
            continue
        b_ll, _ = _eval(sub, bT, bw)
        c_ll, _ = _eval(sub, 25.0, 0.75)
        m_ll, _ = _eval(sub, 25.0, 0.0)
        print(f"  {name:>4} (n={len(sub):>3}): 最良セル {b_ll:.4f} / 現行 {c_ll:.4f} / "
              f"市場 {m_ll:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
