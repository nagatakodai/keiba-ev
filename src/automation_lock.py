"""自動予想・自動投票の封鎖スイッチ。

ユーザ指示 (2026-10-10):「オートで全レース予想したり自動で投票するのは誤作動を防ぐために
一旦ふさいでください」。`make dev` 起動直後に当日キャッチアップスキャン (全レースを Claude で
予想) が自動で走り出した実障害を受けて、自動系を **既定で封鎖** する。

解除は環境変数 `KEIBA_AUTOMATION=1` を明示したときだけ (シェルで
`KEIBA_AUTOMATION=1 make dev` のように付けるか、リポジトリ直下の .env に書く)。
シェルの値が .env より優先 (`KEIBA_AUTOMATION=0` をシェルで付ければ .env の 1 も打ち消せる)。

封鎖中に止まるもの:
  - API 常駐: watch-auto の自動 resume / 当日キャッチアップ・前夜の全レーススキャン /
    推奨レースの締切前自動再score
  - watch-auto の開始 (Web UI・POST /api/watch-auto/start)
  - CLI の自動予想・投票: auto_watch / bet_scheduler / oddspark_bet / ipat_bet
    (Makefile の watch-auto / watch-auto-bet / watch-auto-ipat-bet / bet も入口で止まる)
止まらないもの: 結果の自動取得 (着順・払戻の取得のみ)、手動の分析・勝負レーススキャン
(ボタン/CLI)、odds_capture (オッズの記録のみ)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_KEY = "KEIBA_AUTOMATION"
ROOT = Path(__file__).resolve().parents[1]
BLOCKED_MESSAGE = (
    "自動予想・自動投票は一時停止中です (誤作動防止)。"
    f"解除するには環境変数 {ENV_KEY}=1 を付けて起動してください (例: {ENV_KEY}=1 make dev)。"
)


def _dotenv_value() -> str:
    """リポジトリ直下 .env の KEIBA_AUTOMATION (os.environ は汚さない)。無ければ ""。"""
    try:
        from dotenv import dotenv_values
        return str(dotenv_values(ROOT / ".env").get(ENV_KEY) or "")
    except Exception:  # noqa: BLE001 - dotenv 不在/読めない .env は「未設定」= 封鎖側
        return ""


def automation_enabled() -> bool:
    """自動予想・自動投票が解除されているか。既定 False (封鎖)。"""
    raw = os.environ.get(ENV_KEY)
    if raw is None:
        raw = _dotenv_value()
    return raw.strip() == "1"


def exit_if_blocked(what: str) -> None:
    """封鎖中なら理由を stderr に出して終了 (rc=3)。CLI の自動予想・投票の入口で呼ぶ。"""
    if not automation_enabled():
        print(f"[{what}] {BLOCKED_MESSAGE}", file=sys.stderr, flush=True)
        raise SystemExit(3)


if __name__ == "__main__":   # Makefile の入口ガード: 封鎖中なら rc=3 で make を止める
    exit_if_blocked(sys.argv[1] if len(sys.argv) > 1 else "automation")
