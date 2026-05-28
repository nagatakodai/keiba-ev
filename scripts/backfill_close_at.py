#!/usr/bin/env python
"""既存 snapshot / watch-auto 履歴 の close_at を 発走2分前固定ルールで遡及更新する。

99032d2 「締切=発走2分前固定」以前の snapshot は `close_at == start_at` で保存されており、
フロントで「締切」「発走」が同じ時刻に見える。本スクリプトは:

  - data/predictions/*.json   : close_at == start_at なら start_at - 120 に書き換え
  - data/cache/auto_watch_history.jsonl : 同条件で行ごと書き換え

冪等 (差が既に 120 なら no-op)。start_at=0/未確定の行は触らない。--dry-run で確認後実行。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "data" / "predictions"
HISTORY = ROOT / "data" / "cache" / "auto_watch_history.jsonl"
CLOSE_LEAD_SEC = 120


def _patch(d: dict) -> bool:
    """{start_at, close_at} を持つ dict を 発走2分前固定 に揃える。変更があれば True。"""
    s = d.get("start_at") or 0
    c = d.get("close_at") or 0
    if not s or s <= CLOSE_LEAD_SEC:
        return False  # 未確定 / 異常値は触らない
    expected = s - CLOSE_LEAD_SEC
    if c == expected:
        return False  # 既に正しい (新コードで保存されたもの)
    d["close_at"] = expected
    return True


def main() -> int:
    dry = "--dry-run" in sys.argv

    # snapshots
    snap_patched = 0
    snap_total = 0
    for f in sorted(SNAPSHOTS.glob("*.json")):
        snap_total += 1
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _patch(d):
            snap_patched += 1
            if not dry:
                f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"predictions: patched {snap_patched}/{snap_total} {'(dry-run)' if dry else ''}")

    # watch-auto history (JSONL, 行ごと)
    hist_patched = 0
    hist_total = 0
    if HISTORY.exists():
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
        out_lines = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                out_lines.append("")
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                out_lines.append(ln)
                continue
            hist_total += 1
            if _patch(d):
                hist_patched += 1
                out_lines.append(json.dumps(d, ensure_ascii=False))
            else:
                out_lines.append(ln)
        if not dry:
            HISTORY.write_text("\n".join(out_lines) + ("\n" if out_lines else ""),
                               encoding="utf-8")
    print(f"watch_auto_history: patched {hist_patched}/{hist_total} {'(dry-run)' if dry else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
