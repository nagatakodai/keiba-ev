"""score/bet 各ステージで取得済みのオッズを時系列 (JSONL) に記録する。

Step 1 (2026-06-06): **追加 fetch ゼロ**でオッズ変動データを蓄積する。
- score ステージ (締切5-7分前) と bet ステージ (締切1-2.5分前) は既に fresh odds を
  取得しているので、その場の rd のオッズを `data/cache/odds_timeline/<race_id>.jsonl`
  に append するだけ。result fetch が保存する `final_odds` (束の脚のみ) と合わせて
  1 レースあたり最大 3 点の時系列になる。
- 用途: 締切直前ドリフトの実測 (TORIGAMI_MARGIN の券種別較正) /
  late-money momentum (score→bet のオッズ変化) を特徴量にできるかの検証。
- **失敗しても解析パイプラインを止めない** (capture は全て best-effort、例外を呑む)。
- 同一オッズの重複 append は odds_hash で skip (netkeiba 経路の score phase は
  score 段 capture 後にそのまま snapshot 保存へ fall-through するため、同じ rd で
  bet 側 hook が再度呼ばれる)。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMELINE_DIR = ROOT / "data" / "cache" / "odds_timeline"


def _build_payload(rd) -> dict:
    """rd から bet type 別の {label: odds} dict を組む。0/None オッズは除外。"""
    out: dict[str, dict[str, float]] = {}
    for bt, bets in (rd.other_bets or {}).items():
        d = {b.label: b.odds for b in bets if b.odds and b.odds > 0 and not b.absent}
        if d:
            out[bt] = d
    tri = {t.label: t.odds for t in (rd.trifecta or []) if t.odds and t.odds > 0 and not t.absent}
    if tri:
        out["trifecta"] = tri
    return out


def _odds_hash(payload: dict) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _last_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        last = None
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return None
        return json.loads(last).get("odds_hash")
    except Exception:  # noqa: BLE001
        return None


def capture(race_id: str, rd, stage: str) -> bool:
    """rd の現オッズを timeline に 1 行 append する。

    stage: "score" | "bet"。直前行と同一オッズなら skip (False)。
    例外は全て呑んで False (解析を止めない)。
    """
    try:
        payload = _build_payload(rd)
        if not payload:
            return False
        h = _odds_hash(payload)
        path = TIMELINE_DIR / f"{race_id}.jsonl"
        if _last_hash(path) == h:
            return False
        TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
        line = {
            "stage": stage,
            "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
            # 締切/発走/オッズ更新 unix (0 = 不明)。ドリフト解析の x 軸用。
            "close_at": getattr(rd.race, "close_at", 0) or 0,
            "start_at": getattr(rd.race, "start_at", 0) or 0,
            "odds_updated_at": getattr(rd.race, "odds_updated_at", 0) or 0,
            "n_horses": len([h2 for h2 in rd.race.horses if not h2.absent]),
            "odds_hash": h,
            "odds": payload,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False
