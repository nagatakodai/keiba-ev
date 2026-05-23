"""既存 LGBM model から feature importance を抽出して metadata.json に追記する。

train.py の最新版は metadata に `top_features_by_gain` を保存するが、
本リポジトリの現 production model は metadata 更新前に保存されたもの。
再訓練せずに既存 model から gain を取り出して metadata を補強する。

使い方:
  python scripts/augment_metadata.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402

MODEL = ROOT / "data" / "models" / "lgbm_lambdarank.txt"
META = ROOT / "data" / "models" / "lgbm_metadata.json"


def main() -> int:
    if not MODEL.exists() or not META.exists():
        print(f"model or meta not found: {MODEL} / {META}")
        return 1
    meta = json.loads(META.read_text(encoding="utf-8"))
    feature_cols = meta.get("feature_cols", [])
    if not feature_cols:
        print("metadata に feature_cols が無い")
        return 1
    booster = lgb.Booster(model_file=str(MODEL))
    gains = booster.feature_importance(importance_type="gain")
    pairs = sorted(zip(feature_cols, gains), key=lambda x: -x[1])
    top = [{"name": n, "gain": float(g)} for n, g in pairs[:10]]
    meta["top_features_by_gain"] = top
    # 既存 fields を保持しつつ追記
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"updated {META}: top 10 features by gain")
    for f in top:
        print(f"  {f['name']:30s} gain={f['gain']:10.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
