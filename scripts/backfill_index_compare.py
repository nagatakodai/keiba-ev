"""既存 snapshot に market_win_index / index_compare を後付けする。

`src/analyze.py` の `_market_win_index` / `_build_index_compare` は rd (出馬表) から
計算するが、保存済 snapshot には rd が無い。代わりに snapshot 内の `market_signals`
(win_odds を持つ) と `llm_win_index` (Claude 指数) だけから同じ指数を再構築する。

市場指数の定義は analyze.py と同一: 単勝オッズ由来で、オッズによって変動し 1.0 倍で 100
になる Claude 独立な指数 (市場指数 = 100 / 単勝オッズ = 市場暗黙1着率 %)。

使い方: python scripts/backfill_index_compare.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRED_DIR = ROOT / "data" / "predictions"


def _market_index_from_signals(market_signals: list[dict]) -> dict[int, float]:
    out: dict[int, float] = {}
    for s in market_signals:
        wo = s.get("win_odds")
        if not wo or float(wo) <= 0:
            continue
        out[int(s["number"])] = round(max(0.0, min(100.0, 100.0 / float(wo))), 1)
    return out


def _build(
    market_signals: list[dict], llm_win_index: dict | None, llm_support: dict | None
) -> tuple[dict, list]:
    market = _market_index_from_signals(market_signals)
    claude = {int(k): float(v) for k, v in (llm_win_index or {}).items()}
    support = {}
    for k, v in (llm_support or {}).items():
        try:
            support[int(k)] = max(0, int(float(v)))
        except (ValueError, TypeError):
            continue
    names = {int(s["number"]): (s.get("name") or "") for s in market_signals}
    nums = (set(claude) | set(market)) & set(names)
    rows = []
    for n in nums:
        c = claude.get(n)
        mk = market.get(n)
        rows.append({
            "number": n,
            "name": names.get(n, ""),
            "claude_index": (round(c, 1) if c is not None else None),
            "market_index": (mk if mk is not None else None),
            "diff": (round(c - mk, 1) if (c is not None and mk is not None) else None),
            "support": (support[n] if n in support else None),
        })
    rows.sort(
        key=lambda r: (
            r["claude_index"] if r["claude_index"] is not None
            else (r["market_index"] if r["market_index"] is not None else -1.0)
        ),
        reverse=True,
    )
    return {str(k): v for k, v in market.items()}, rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = [f for f in sorted(PRED_DIR.glob("*.json")) if not f.name.endswith(".llm.json")]
    n_written = n_skip = n_claude = 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ms = d.get("market_signals")
        if not ms:
            n_skip += 1
            continue
        market_idx, index_compare = _build(ms, d.get("llm_win_index"), d.get("llm_support"))
        if not index_compare:
            n_skip += 1
            continue
        d["market_win_index"] = market_idx or None
        d["index_compare"] = index_compare
        if d.get("llm_win_index"):
            n_claude += 1
        if not args.dry_run:
            f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        n_written += 1
    print(f"{'(dry-run) ' if args.dry_run else ''}backfilled {n_written} snapshots "
          f"({n_claude} with Claude index, {n_skip} skipped/no market_signals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
