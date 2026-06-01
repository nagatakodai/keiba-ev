"""data/raw/<rid>-result.html.gz から全レースの確定結果 (着順 + 全券種の確定払戻オッズ) を
復元して 1 つの parquet にする。netkeiba block 中でも手元の HTML だけで作れる。

`parse_result` の final_odds は **当たった組番だけ** (払戻テーブル) を持つ。ROI バックテスト
には十分: 外れ目の払戻は 0 なので、当たり目のオッズだけ分かれば「自分の買い目が当たりを
含めば payout、含まなければ 0」で収支が出る。EV/Kelly 選抜 (全組オッズが要る) には使えないが、
確率ベース Plan (H1/H2/G・単勝) と「当たり判定 + 払戻」のバックテストはフルにできる。

出力 `data/datasets/settled_odds.parquet`: 1 行 = (race_id, bet_type, key, odds)。
  - key は払戻の組番 ("5", "3-5", "3-5-7" 等、parse_result の final_odds キーから bet_type: を除いたもの)
  - odds = 払戻金 / 100 (= 確定オッズ)
finish_order は all.parquet (finish_pos) から復元できるので別途持たない。

使い方: python scripts/build_settled_odds.py [--workers N]
"""
from __future__ import annotations

import argparse
import gzip
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # 子プロセスが src を import できるように
RAW_DIR = ROOT / "data" / "raw"
OUT = ROOT / "data" / "datasets" / "settled_odds.parquet"


def _one(rid: str) -> list[tuple]:
    # 子プロセスで import (pickle 軽量化)
    from src.parse import parse_result
    p = RAW_DIR / f"{rid}-result.html.gz"
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return []
    try:
        r = parse_result(html)
    except Exception:
        return []
    if not r:
        return []
    fo = r.get("final_odds") or {}
    rows = []
    for k, odds in fo.items():
        if ":" not in k:
            continue
        bet_type, key = k.split(":", 1)
        try:
            o = float(odds)
        except (TypeError, ValueError):
            continue
        if o <= 0:
            continue
        rows.append((rid, bet_type, key, o))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    rids = sorted({p.name.split("-result")[0] for p in RAW_DIR.glob("*-result.html.gz")})
    print(f"parsing {len(rids):,} result HTMLs with {args.workers} workers ...", flush=True)

    all_rows: list[tuple] = []
    n_races_ok = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for rows in ex.map(_one, rids, chunksize=20):
            if rows:
                n_races_ok += 1
                all_rows.extend(rows)

    df = pd.DataFrame(all_rows, columns=["race_id", "bet_type", "key", "odds"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"saved {OUT} — rows={len(df):,} races_with_payout={n_races_ok:,}", flush=True)
    print("bet_type coverage:", df["bet_type"].value_counts().to_dict(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
