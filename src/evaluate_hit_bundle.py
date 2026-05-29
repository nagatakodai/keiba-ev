"""的中優先 bundle の Claude 選定 (detached subprocess 用エントリポイント)。

analyze.py / scrape_keibago / scrape_jra / scrape_oddspark は分析の最後に:

    python -m src.evaluate_hit_bundle <race_id> &

を detached で起動する (`_spawn_hit_bundle_claude`)。本モジュールは:

  ① `data/cache/analyze_state/<race_id>.pkl` を読み込み rd/probs/rows/bet_tables を復元
  ② `_validate_and_update_bundle(..., prioritize="hit")` を呼び claude -p で 的中優先 picks を選定
  ③ snapshot の `recommended_bundle_hit` を更新
  ④ state pickle は完了後に削除 (積み残しを防ぐ)

回収優先 (`recommended_bundle`) が先に確定して daemon の購入が始まっている前提なので、
ここでは時間制約なし。Claude の web 検索で per-leg 補強根拠を集めてゆっくり選定してよい。
**実弾は買わない (おまけ計測)**。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m src.evaluate_hit_bundle <race_id>", file=sys.stderr)
        return 2
    race_id = sys.argv[1].strip()
    if not race_id:
        return 2

    # analyze.py 由来の helper を流用 (循環 import 回避で関数内 import)
    from . import analyze as az_mod
    state = az_mod._load_analyze_state_for_hit(race_id)
    if state is None:
        print(f"[evaluate_hit_bundle] state 不在: {race_id} (analyze が失敗 or 旧 snapshot)",
              file=sys.stderr)
        return 1
    try:
        az_mod._validate_and_update_bundle(
            race_id,
            state["rd"], state["probs"], state["rows"], state["bet_tables"],
            aptitudes=state.get("aptitudes"),
            market_signals=state.get("market_signals"),
            horse_best_times=state.get("horse_best_times"),
            prioritize="hit",
        )
    except Exception as ex:  # noqa: BLE001
        print(f"[evaluate_hit_bundle] {race_id} 的中優先 Claude 失敗: {ex}", file=sys.stderr)
        return 1
    finally:
        # 成功/失敗とも state pickle を削除 (積み残し防止)
        try:
            (ROOT / "data" / "cache" / "analyze_state" / f"{race_id}.pkl").unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
