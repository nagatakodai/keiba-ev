"""te1 (タイム換算比較) は市場 (確定単勝オッズ) を超える勝ち馬情報を持つか — 条件付きロジットの検定 (読み取り専用)。

入力:
  - scripts/time_eval_official_backtest.py --dump の JSONL (レースごとの te1 指数・順位・出走馬・払戻)
  - 地方競馬全国協会の確定オッズ data/cache/nar_official/odds-YYYYMM.zip (2026-03〜, `python -m src.nar_official sync-odds`)

モデル (レース内の条件付きロジット, 同着は勝ち馬で 1 を等分):
    P(i が勝つ) ∝ exp(β·log q_i + α·z_i + γ·d_i)
  q = 確定単勝オッズの逆数をレース内で正規化した市場の勝率、z = te1 指数のレース内標準化 (推定なしの馬は 0)、
  d = te1 の推定なし指示子。α > 0 なら te1 は市場に無い勝ち馬情報を持つ。

出力: 学習期間の α/β (Hessian 95% CI)・検証期間の 1 レースあたり対数尤度の改善 (α の有無だけの対比 =
[β, γ] モデル比、参考に β のみ比も。レース単位 bootstrap CI)・全期間の α (CI + 尤度比検定・片側 p)・
単勝 ¥100 均等買いの描写 (事後の切り分け = 多重比較)。

  python scripts/time_eval_official_backtest.py --te <版> --variants nk+off --months <月,...> --dump <dump.jsonl>
  python scripts/te1_market_logit.py --dump <dump.jsonl> --te <版> [--since YYYYMMDD] [--variant nk+off]
        [--fit-months 202606,202607] [--test-months 202608,202609,202610] [--boot 2000]
  事前登録の判定: te1 = `--te te1 --since 20261013` / te2 = `--te te2 --since 20261015` (backtest も同じ --te で)。

事前登録 (2026-10-13 凍結): 地方平地・te1 推定 3 頭以上・確定オッズが全出走馬にあるレース、上の式。
2026-06〜10-12 (5,083R, 発見に使った期間) は 全期間 α=+0.076 [+0.025,+0.126] (片側 p=0.002)・学習 6-7 月 α=+0.099
だが、検証 8-10 月の対数尤度の改善は CI が 0 を跨ぐ。→ **判定は 2026-10-13 以降の新しいレースだけで行う**:
新しいレース (学習も検証も 10-13 以降) で片側 α>0 (p<0.05) **かつ** 検証の改善 (α の対比) の点推定 > 0 なら
「市場を超える情報あり」として確率モデルへの接続を検討。現在の効果量なら検出力 80% に新規 ~3,600R (2027-01 末頃)。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import nar_official as no  # noqa: E402


def _int(s: str) -> int:
    s = (s or "").strip()
    return int(s) if s.isdigit() else 0


def load_win_odds(months: list[str]) -> dict[tuple, dict[int, float]]:
    """(場, 日付, R) → {馬番: 確定単勝オッズ}。"""
    out: dict[tuple, dict[int, float]] = defaultdict(dict)
    for ym in months:
        p = no.monthly_zip_path(ym, "odds")
        if not p.exists():
            print(f"  (skip {ym}: {p} がありません — python -m src.nar_official sync-odds)")
            continue
        with zipfile.ZipFile(p) as z:
            for name in z.namelist():
                with z.open(name) as fh:
                    for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")):
                        if (r.get("賭式") or "").strip() != "単勝":
                            continue
                        o = (r.get("オッズ") or "").strip()
                        try:
                            odds = float(o)
                        except ValueError:
                            continue
                        key = ((r.get("競馬場") or "").strip(), _int(r.get("競走年月日", "")), _int(r.get("レース番号", "")))
                        out[key][_int(r.get("番号1", ""))] = odds
    return out


def build(dump: Path, variant: str, odds: dict, te: str | None = None, since: int = 0) -> list[dict]:
    """dump × オッズ → レースごとの {month, X (n×3: log q, z, d), y, odds, pay, te1_top, fav}。

    te = 換算の版 (dump の "te"。2026-10-29 以前の dump には無い = None 扱い)、since = この日 (YYYYMMDD) 以降のレースだけ。
    1 つの dump に版が混ざっているのに te を指定しないと、版の違う指数を混ぜて当てはめてしまうので止める。"""
    races = []
    seen_te: set = set()
    with open(dump, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("variant") != variant:
                continue
            seen_te.add(d.get("te"))
            if te is not None and d.get("te") != te:
                continue
            key = tuple(d["key"])
            if since and int(key[1]) < since:
                continue
            od = odds.get(key)
            runners = d["runners"]
            if not od or len(runners) < 5:
                continue
            nums = [x["num"] for x in runners]
            if any(n not in od or od[n] <= 0 for n in nums):
                continue
            if set(od) - set(nums):
                continue        # オッズはあるのに出走馬に居ない馬 (旧 dump の競走中止など) — 市場の正規化が狂う
            idx = {int(k): v for k, v in (d.get("index") or {}).items() if v is not None}
            if sum(1 for n in nums if n in idx) < 3:
                continue
            winners = [x["num"] for x in runners if x.get("fin") == 1]
            if not winners:
                continue
            inv = np.array([1.0 / od[n] for n in nums])
            q = inv / inv.sum()
            vals = np.array([idx.get(n, np.nan) for n in nums], dtype=float)
            have = ~np.isnan(vals)
            mu, sd = vals[have].mean(), vals[have].std()
            z = np.where(have, (vals - mu) / sd if sd > 0 else 0.0, 0.0)
            y = np.array([1.0 / len(winners) if n in winners else 0.0 for n in nums])
            rank = {int(k): v for k, v in (d.get("rank") or {}).items() if v}
            top = min(rank, key=rank.get) if rank else None
            pays = {int(k): v for k, v in (d.get("pays") or {}).items()}
            races.append({"month": f"{key[1] // 100:06d}", "nums": nums,
                          "X": np.column_stack([np.log(q), z, (~have).astype(float)]), "y": y,
                          "odds": np.array([od[n] for n in nums]), "pays": pays, "winners": set(winners),
                          "top": top, "fav": nums[int(np.argmax(q))]})
    if te is None and len(seen_te) > 1:
        raise SystemExit(f"dump に版が混ざっています {sorted(map(str, seen_te))} — --te で 1 つ選んでください")
    return races


class CLogit:
    """レース内の条件付きロジット (Newton 法)。races: [{X, y}]。"""

    def __init__(self, races: list[dict], cols: list[int]):
        self.X = np.vstack([r["X"][:, cols] for r in races])
        self.y = np.concatenate([r["y"] for r in races])
        counts = np.array([len(r["y"]) for r in races])
        self.starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
        self.rid = np.repeat(np.arange(len(races)), counts)
        self.R = len(races)

    def race_ll(self, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        eta = self.X @ b
        m = np.maximum.reduceat(eta, self.starts)
        e = np.exp(eta - m[self.rid])
        s = np.add.reduceat(e, self.starts)
        p = e / s[self.rid]
        ll = np.add.reduceat(self.y * eta, self.starts) - (m + np.log(s))
        return ll, p

    def fit(self, wr: np.ndarray | None = None, b0: np.ndarray | None = None) -> tuple[np.ndarray, float, np.ndarray]:
        K = self.X.shape[1]
        wr = np.ones(self.R) if wr is None else wr
        b = np.zeros(K) if b0 is None else np.array(b0, dtype=float)
        ll_r, p = self.race_ll(b)
        LL = float(wr @ ll_r)
        for _ in range(100):
            g, H = self._grad_hess(p, wr)
            step = np.linalg.lstsq(H, g, rcond=None)[0]
            t = 1.0
            while True:
                bn = b - t * step
                ll_n, pn = self.race_ll(bn)
                LLn = float(wr @ ll_n)
                if LLn >= LL - 1e-12 or t < 1e-8:
                    break
                t *= 0.5
            done = np.max(np.abs(bn - b)) < 1e-10
            b, p, LL = bn, pn, LLn
            if done:
                break
        _g, H = self._grad_hess(p, wr)
        return b, LL, H

    def _grad_hess(self, p: np.ndarray, wr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        w = wr[self.rid]
        g = ((self.y - p) * w) @ self.X
        xbar = np.add.reduceat(p[:, None] * self.X, self.starts)
        sxx = np.add.reduceat(p[:, None, None] * self.X[:, :, None] * self.X[:, None, :], self.starts)
        H = -np.einsum("r,rij->ij", wr, sxx - xbar[:, :, None] * xbar[:, None, :])
        return g, H


def _ci(b: np.ndarray, H: np.ndarray) -> np.ndarray:
    se = np.sqrt(np.clip(np.diag(np.linalg.inv(-H)), 0, None))
    return np.column_stack([b - 1.96 * se, b + 1.96 * se])


def _boot_mean(x: np.ndarray, B: int, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(x)
    ms = np.array([x[rng.integers(0, n, n)].mean() for _ in range(B)])
    return float(np.percentile(ms, 2.5)), float(np.percentile(ms, 97.5))


def flat_bets(races: list[dict], pick, B: int) -> dict:
    hits, pay = [], []
    for r in races:
        n = pick(r)
        if n is None:
            continue
        won = n in r["winners"]
        hits.append(1.0 if won else 0.0)
        pay.append(float(r["pays"].get(n, 0)) if won else 0.0)
    if not hits:
        return {"n": 0}
    pay_a = np.array(pay) / 100.0
    lo, hi = _boot_mean(pay_a, B)
    return {"n": len(hits), "hit": float(np.mean(hits)), "roi": float(pay_a.mean() * 100), "roi_ci": (lo * 100, hi * 100)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--variant", default="nk+off")
    ap.add_argument("--fit-months", default="202606,202607")
    ap.add_argument("--test-months", default="202608,202609,202610")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--te", default=None, help="換算の版 (dump の te: te1 / te2 / te2.1)。事前登録は登録した版で")
    ap.add_argument("--since", type=int, default=0,
                    help="この日 (YYYYMMDD) 以降のレースだけ (事前登録: te1 は 20261013・te2 は 20261015 以降)")
    args = ap.parse_args()
    fit_m, test_m = args.fit_months.split(","), args.test_months.split(",")
    odds = load_win_odds(sorted(set(fit_m + test_m)))
    races = build(Path(args.dump), args.variant, odds, args.te, args.since)
    fit_r = [r for r in races if r["month"] in fit_m]
    test_r = [r for r in races if r["month"] in test_m]
    have = {r["month"] for r in races}
    for m in fit_m + test_m:
        if m not in have:
            print(f"  [注意] {m} のレースが dump に無い (backtest の --months / オッズの sync-odds を確認)")
    print(f"races: fit {len(fit_r):,} / test {len(test_r):,} (variant {args.variant}, te {args.te or '-'}, "
          f"since {args.since or '-'})")
    if not fit_r or not test_r:
        print("データ不足")
        return 1

    full = CLogit(fit_r, [0, 1, 2])
    b, ll, H = full.fit()
    ci = _ci(b, H)
    mk = CLogit(fit_r, [0])
    b0, ll0, _ = mk.fit()
    print(f"\n[学習 {','.join(fit_m)}] β={b[0]:.3f} [{ci[0,0]:.3f},{ci[0,1]:.3f}]  "
          f"α={b[1]:+.3f} [{ci[1,0]:+.3f},{ci[1,1]:+.3f}]  γ(推定なし)={b[2]:+.3f}  市場のみ β={b0[0]:.3f}")

    # 検証: α の有無だけを比べる (帰無 = 市場 + 推定なし指示子 [β, γ] — 全期間の尤度比検定と同じ帰無)
    nul = CLogit(fit_r, [0, 2])
    bn, _lln, _Hn = nul.fit()
    ll_full, _ = CLogit(test_r, [0, 1, 2]).race_ll(b)
    ll_nul, _ = CLogit(test_r, [0, 2]).race_ll(bn)
    ll_mk, _ = CLogit(test_r, [0]).race_ll(b0)
    for label, diff in (("te1 の有無 (主)", ll_full - ll_nul), ("参考: te1+推定なし指示子 vs 市場のみ", ll_full - ll_mk)):
        lo, hi = _boot_mean(diff, args.boot)
        print(f"[検証 {','.join(test_m)}] 1R あたり対数尤度の改善 {label} = {diff.mean():+.5f} "
              f"[{lo:+.5f},{hi:+.5f}]  z={diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff))):+.2f}")

    allr = fit_r + test_r
    fa = CLogit(allr, [0, 1, 2])
    ba, lla, Ha = fa.fit()
    cia = _ci(ba, Ha)
    fa0 = CLogit(allr, [0, 2])
    _b, lla0, _H = fa0.fit()
    lr = 2 * (lla - lla0)
    p = math.erfc(math.sqrt(max(lr, 0) / 2))                   # 自由度 1 の χ² 上側 (両側)
    p1 = p / 2 if ba[1] > 0 else 1 - p / 2                     # 事前登録の片側 α > 0
    print(f"[全期間 {len(allr):,}R] α={ba[1]:+.3f} [{cia[1,0]:+.3f},{cia[1,1]:+.3f}]  LR={lr:.2f} "
          f"p(両側)={p:.4f} p(片側 α>0)={p1:.4f}")

    # EV: 検証期間で te1+市場のモデル確率 × 確定オッズ の最大
    _ll, pt = CLogit(test_r, [0, 1, 2]).race_ll(b)
    ev = pt * np.concatenate([r["odds"] for r in test_r])
    print(f"[検証] モデル確率 × 確定オッズ の最大 {ev.max():.3f} (≥1.0 の馬 {int((ev >= 1.0).sum())} 頭)")

    print("\n単勝 ¥100 均等買い (事後の描写・多重比較あり):")
    cells = [
        ("1番人気", lambda r: r["fav"]),
        ("te1 1位", lambda r: r["top"]),
        ("te1 1位 = 1番人気", lambda r: r["top"] if r["top"] == r["fav"] else None),
        ("te1 1位 ≠ 1番人気", lambda r: r["top"] if r["top"] is not None and r["top"] != r["fav"] else None),
    ]
    for label, pick in cells:
        m = flat_bets(allr, pick, args.boot)
        if m.get("n"):
            print(f"  {label:<16} n={m['n']:>5}  的中 {m['hit']:.1%}  ROI {m['roi']:.1f}% "
                  f"[{m['roi_ci'][0]:.1f},{m['roi_ci'][1]:.1f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
