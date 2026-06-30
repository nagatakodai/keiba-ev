#!/usr/bin/env python3
"""score 段のツール利用 (data/cache/tool_usage/*.jsonl) を集計する。

ユーザ指示 (2026-06-30): score 段の tool_use を永続化し「Tavily vs WebFetch 等」をどれだけ使ったか
後から比較できるようにする (まずはログ蓄積→可視化)。score 段の allowlist は Tavily(search/extract)
+ WebFetch + Read で **WebSearch は未許可** (将来 A/B 用)。本レポートは「どのツールをどれだけ使ったか」
の実態を出す。質 (的中寄与) の比較は outcome 紐付け / A/B が必要だが、まずは使用頻度を可視化する。

    .venv/bin/python scripts/tool_usage_report.py

読み取り専用。
"""
from __future__ import annotations

import collections
import glob
import json
import os

DIR = "data/cache/tool_usage"


def main() -> None:
    files = sorted(glob.glob(f"{DIR}/*.jsonl"))
    if not files:
        print("ツール利用ログがまだありません。score 段 (勝負レース解析 / watch-auto / 自動再score) を"
              " 回すと data/cache/tool_usage/<race_id>.jsonl に蓄積されます。")
        return
    by_kind: collections.Counter = collections.Counter()
    by_tool: collections.Counter = collections.Counter()
    races_with_kind: dict[str, set] = collections.defaultdict(set)
    per_race_calls: list[int] = []
    for f in files:
        rid = os.path.basename(f)[:-6]
        n = 0
        try:
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = row.get("kind", "other")
                t = row.get("tool", "?")
                by_kind[k] += 1
                by_tool[t] += 1
                races_with_kind[k].add(rid)
                n += 1
        except OSError:
            continue
        per_race_calls.append(n)
    total = sum(by_kind.values())
    nr = len(files)
    if total == 0:
        print("ログはあるが tool_use が 0 件。")
        return
    print(f"ツール利用ログ: {nr} レース / 総 tool_use {total} 回 "
          f"(1レース平均 {total / nr:.1f} 回)\n")
    print(f"  {'種別':<12}{'回数':>7}{'割合':>7}{'使ったレース':>12}")
    for k, c in by_kind.most_common():
        print(f"  {k:<12}{c:>7}{c / total * 100:>6.0f}%{len(races_with_kind[k]):>12}")
    print(f"\n  {'ツール名':<34}{'回数':>7}")
    for t, c in by_tool.most_common():
        print(f"  {t:<34}{c:>7}")
    print("\n※ Tavily(search/extract) と WebFetch は役割分担 (検索 vs 既知URL取得)・WebSearch は"
          " 現状 allowlist 外。\n  「どちらが優れているか」の質比較は outcome 紐付け or A/B が要る"
          " (本ログを蓄積して次段で検証)。")


if __name__ == "__main__":
    main()
