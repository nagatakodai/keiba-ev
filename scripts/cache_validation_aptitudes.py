"""Validation 291 races の aptitude top horses を計算してキャッシュ。

src/eval_holdout.py の Plan G / Plan F 評価のために、validation set 全体について
適性指数 top N 頭の集合をキャッシュ JSON に保存する。

処理:
  data/raw/{race_id}-shutuba.html.gz から RaceData 構築
  data/raw/{race_id}-past.html.gz から past_runs を取得して horses に紐付け
  compute_aptitudes(rd) で適性指数算出
  total スコア降順で top N 頭の馬番リストを保存

出力:
  data/cache/aptitudes/{race_id}.json
    { "race_id": "...", "aptitude_top_horses": [3, 7, 1, 12, 4, 6], "n": 6 }
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.aptitude import compute_aptitudes  # noqa: E402
from src.parse import parse_past_runs, parse_shutuba  # noqa: E402

CACHE_DIR = ROOT / "data" / "cache" / "aptitudes"
RAW_DIR = ROOT / "data" / "raw"


def _race_id_to_int(rid: str) -> int:
    try:
        return int(rid)
    except (ValueError, TypeError):
        return 0


def get_holdout_race_ids(valid_frac: float = 0.2) -> list[str]:
    df = pd.read_parquet(ROOT / "data" / "datasets" / "all.parquet")
    rids = df["race_id"].unique().tolist()
    rids.sort(key=_race_id_to_int)
    n_valid = max(int(len(rids) * valid_frac), 1)
    valid_rids = rids[-n_valid:]
    df_v = df[df["race_id"].isin(valid_rids)]
    has_result = (
        df_v.groupby("race_id")["target_top1"].sum().pipe(lambda s: s[s > 0]).index
    )
    return [r for r in valid_rids if r in set(has_result)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--top-n", type=int, default=6, help="aptitude top N (Plan G の集合サイズ)")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    race_ids = get_holdout_race_ids()
    if args.limit:
        race_ids = race_ids[: args.limit]
    print(f"target races: {len(race_ids)}", flush=True)

    t0 = time.time()
    n_done = 0
    n_skip = 0
    n_err = 0
    for i, rid in enumerate(race_ids, 1):
        out = CACHE_DIR / f"{rid}.json"
        if out.exists():
            n_skip += 1
            continue
        sh_path = RAW_DIR / f"{rid}-shutuba.html.gz"
        past_path = RAW_DIR / f"{rid}-past.html.gz"
        if not sh_path.exists():
            n_err += 1
            print(f"  ERROR {rid}: no shutuba HTML", flush=True)
            continue
        try:
            sh_html = gzip.open(sh_path, "rt", encoding="utf-8").read()
            rd = parse_shutuba(sh_html, race_id=rid)
            if past_path.exists():
                past_html = gzip.open(past_path, "rt", encoding="utf-8").read()
                runs = parse_past_runs(past_html)
                for h in rd.race.horses:
                    h.past_runs = runs.get(h.number, [])
            apts = compute_aptitudes(rd)
            ranked = sorted(apts.items(), key=lambda kv: kv[1].total, reverse=True)
            top = [int(n) for n, _ in ranked[: args.top_n]]
            data = {
                "race_id": rid,
                "n": args.top_n,
                "aptitude_top_horses": top,
            }
            out.write_text(json.dumps(data), encoding="utf-8")
            n_done += 1
        except Exception as e:
            n_err += 1
            print(f"  ERROR {rid}: {type(e).__name__}: {e}", flush=True)
            continue
        if (i % 50) == 0:
            elapsed = time.time() - t0
            print(
                f"  [{i}/{len(race_ids)}] last={rid} "
                f"avg={elapsed/max(n_done, 1):.2f}s/race "
                f"(done={n_done} skip={n_skip} err={n_err})",
                flush=True,
            )

    total = time.time() - t0
    print(
        f"finished: {n_done} computed, {n_skip} cached, {n_err} errors. "
        f"total {total:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
