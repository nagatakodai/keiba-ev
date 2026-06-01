"""過去 NAR ダート race に Claude score ステージ (effort max + web検索 + alerts) を流す pilot。

目的: ①パイプライン動作確認 ②1レースあたりの実コスト($)/時間/検索回数 ③出力品質 (指数/alerts)。
⚠ 過去 race は web 検索で結果が漏れうる (leakage) ので予測力の純粋検証には使えない。
ネガティブ・スクリーン + コスト/品質確認用。

結果は data/cache/pilot_llm/<race_id>.json に保存。

使い方: python scripts/pilot_claude_score.py <race_id> [<race_id> ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import llm as L  # noqa: E402
from src.aptitude import compute_aptitudes  # noqa: E402
from src.dataset import load_race  # noqa: E402
from src.features import build_features  # noqa: E402
from src.market_signal import compute_market_signals  # noqa: E402

OUT = ROOT / "data" / "cache" / "pilot_llm"


def run_one(rid: str, model: str = "opus") -> dict:
    loaded = load_race(rid)
    if loaded is None:
        return {"race_id": rid, "error": "load failed"}
    rd, _ = loaded
    feats = build_features(rd)
    apts = compute_aptitudes(rd, feats=feats)
    ms = compute_market_signals(rd)
    n = len([h for h in rd.race.horses if not h.absent])
    prompt = L.build_horse_score_prompt(rd, aptitudes=apts, market_signals=ms)
    cmd = [
        "claude", "-p", prompt, "--model", model, "--effort", "max",
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", ",".join(L.ALLOWED_TOOLS), "--disallowedTools", L.DISALLOWED_TOOLS,
    ]
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1, env=L._claude_env())
    chunks, n_search, cost, usage, result_txt = [], 0, None, None, ""
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "assistant":
            for b in ev.get("message", {}).get("content", []) or []:
                if b.get("type") == "tool_use":
                    n_search += 1
                    q = (b.get("input") or {}).get("query") or ""
                    print(f"    search[{n_search}]: {q[:70]}", flush=True)
                elif b.get("type") == "text" and b.get("text"):
                    chunks.append(b["text"])
        elif et == "result":
            result_txt = ev.get("result", "") or ""
            cost = ev.get("total_cost_usd")
            usage = ev.get("usage")
    proc.wait()
    dt = time.time() - t0
    parsed = L.parse_horse_scores("".join(chunks) + "\n" + result_txt)
    rec = {
        "race_id": rid, "model": model, "n_horses": n, "seconds": round(dt, 1),
        "n_search": n_search, "cost_usd": cost, "usage": usage,
        "scores": parsed.get("scores"), "support": parsed.get("support"),
        "alerts": parsed.get("alerts"), "summary": parsed.get("summary"),
        "confidence": parsed.get("confidence"), "scale": parsed.get("scale"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{rid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def main() -> int:
    rids = sys.argv[1:]
    if not rids:
        print("usage: pilot_claude_score.py <race_id> ...")
        return 1
    total_cost = 0.0
    for rid in rids:
        print(f"\n=== {rid} ===", flush=True)
        r = run_one(rid)
        if r.get("error"):
            print("  error:", r["error"], flush=True)
            continue
        c = r.get("cost_usd") or 0.0
        total_cost += c
        print(f"  done: {r['seconds']}s, {r['n_search']} searches, cost ${c:.3f}, "
              f"{len(r['scores'] or {})} 頭スコア, alerts={sum(len(v) for v in (r['alerts'] or {}).values())}件",
              flush=True)
        if r.get("scores"):
            top = sorted((r["scores"] or {}).items(), key=lambda kv: -kv[1])[:3]
            print(f"  top3 指数: {top}", flush=True)
        if r.get("alerts"):
            print(f"  alerts: {r['alerts']}", flush=True)
    print(f"\n=== TOTAL cost: ${total_cost:.3f} for {len(rids)} race(s) "
          f"(= ${total_cost/max(len(rids),1):.3f}/race) ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
