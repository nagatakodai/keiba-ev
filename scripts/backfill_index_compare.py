"""既存 snapshot に market_win_index / index_compare を後付けする。

`src/analyze.py` の `_market_win_index` / `_build_index_compare` は rd (出馬表) から
計算するが、保存済 snapshot には rd が無い。代わりに snapshot 内の `market_signals`
(win_odds を持つ) と `llm_win_index` (Claude 指数) だけから同じ指数を再構築する。

市場指数の定義は analyze.py の _market_win_index と同一: 単勝オッズ由来で 1.0 倍が 100 に
なる Claude 独立な指数。市場指数 = 100·(1/オッズ)^(1/MARKET_INDEX_T) (T=1.5)。

使い方: python scripts/backfill_index_compare.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRED_DIR = ROOT / "data" / "predictions"

# analyze.MARKET_INDEX_T と同期させること (重い src.analyze を import せず複製)。
MARKET_INDEX_T = 1.5


def _market_index_from_signals(market_signals: list[dict]) -> dict[int, float]:
    exp = 1.0 / MARKET_INDEX_T
    out: dict[int, float] = {}
    for s in market_signals:
        wo = s.get("win_odds")
        if not wo or float(wo) <= 0:
            continue
        p = 1.0 / float(wo)
        out[int(s["number"])] = round(max(0.0, min(100.0, 100.0 * (p ** exp))), 1)
    return out


def _num_str_map(raw: dict | None) -> dict[int, list[str]]:
    """{"3": ["…","…"]} を {3: [...]} に正規化 (alerts / evidence 共用)。"""
    out: dict[int, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            n = int(k)
        except (ValueError, TypeError):
            continue
        items = [v] if isinstance(v, str) else (list(v) if isinstance(v, (list, tuple)) else [])
        labels = [str(x).strip() for x in items if x is not None and str(x).strip()]
        if labels:
            out[n] = labels
    return out


def _build(
    market_signals: list[dict], llm_win_index: dict | None, llm_support: dict | None,
    *, alerts: dict | None = None, evidence: dict | None = None,
) -> tuple[dict, list]:
    market = _market_index_from_signals(market_signals)
    claude = {int(k): float(v) for k, v in (llm_win_index or {}).items()}
    support = {}
    for k, v in (llm_support or {}).items():
        try:
            support[int(k)] = max(0, int(float(v)))
        except (ValueError, TypeError):
            continue
    al = _num_str_map(alerts)
    ev = _num_str_map(evidence)
    names = {int(s["number"]): (s.get("name") or "") for s in market_signals}
    # alerts/evidence は指数の無い取消馬でも残せるよう nums に含める (analyze._build_index_compare と同様)。
    nums = (set(claude) | set(market) | set(al) | set(ev)) & set(names)
    rows = []
    for n in nums:
        c = claude.get(n)
        mk = market.get(n)
        ev_n = ev.get(n, [])
        # 「根」は evidence 件数に一致させる (analyze._build_index_compare と同じ contract)。
        sup_n = len(ev_n) if ev_n else (support[n] if n in support else None)
        rows.append({
            "number": n,
            "name": names.get(n, ""),
            "claude_index": (round(c, 1) if c is not None else None),
            "market_index": (mk if mk is not None else None),
            "diff": (round(c - mk, 1) if (c is not None and mk is not None) else None),
            "support": sup_n,
            "alerts": al.get(n, []),
            "evidence": ev_n,
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
        # alerts/evidence は ①既存 index_compare 行 (再実行での退行防止) と ②<race_id>.llm.json
        # (score キャッシュ) から拾い、再構築で落とさない (analyze.py の保存と同じ表示フィールド)。
        alerts: dict[str, list] = {}
        evidence: dict[str, list] = {}
        for row in (d.get("index_compare") or []):
            num = row.get("number")
            if num is None:
                continue
            if row.get("alerts"):
                alerts[str(num)] = list(row["alerts"])
            if row.get("evidence"):
                evidence[str(num)] = list(row["evidence"])
        llm_path = f.with_name(f.stem + ".llm.json")
        if llm_path.exists():
            try:
                lj = json.loads(llm_path.read_text(encoding="utf-8"))
                alerts.update({str(k): v for k, v in (lj.get("alerts") or {}).items() if v})
                evidence.update({str(k): v for k, v in (lj.get("evidence") or {}).items() if v})
            except (json.JSONDecodeError, OSError):
                pass
        # Claude 列は指数 (0-100) として表示。strength/prob どちらの snapshot でも出す。
        market_idx, index_compare = _build(ms, d.get("llm_win_index"), d.get("llm_support"),
                                           alerts=alerts, evidence=evidence)
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
