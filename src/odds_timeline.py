"""オッズ変動を時系列 (JSONL) に記録する。

Step 1 (2026-06-06): **追加 fetch ゼロ**でオッズ変動データを蓄積する。
- score ステージ (締切5-7分前) と bet ステージ (締切1-2.5分前) は既に fresh odds を
  取得しているので、その場の rd のオッズを `data/cache/odds_timeline/<race_id>.jsonl`
  に append するだけ (`capture`)。
Step 2 (2026-06-06): `src/odds_capture.py` の poll daemon が締切前 N 分のオッズを
  keiba.go.jp (NAR) / JRA 公式 (JRA) から取得して同じファイルに stage="poll" で append
  (`append_line` を直接使う)。netkeiba は polling しない (IP 規制)。
- result fetch が保存する `final_odds` (束の脚のみ) と合わせて時系列が閉じる。
- 用途: 締切直前ドリフトの実測 (TORIGAMI_MARGIN の券種別較正) /
  late-money momentum (score→bet のオッズ変化) を特徴量にできるかの検証。
- **失敗しても解析パイプラインを止めない** (capture/append_line は例外を呑む)。
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


def build_payload(other_bets: dict | None, trifecta: list | None) -> dict:
    """other_bets / trifecta から bet type 別の {label: odds} dict を組む。

    0/None オッズ・取消馬は除外。other_bets は {bet_type: [BetOdds]}、trifecta は
    [TrifectaOdds] (rd の形式そのまま)。
    """
    out: dict[str, dict[str, float]] = {}
    for bt, bets in (other_bets or {}).items():
        d = {b.label: b.odds for b in bets if b.odds and b.odds > 0 and not b.absent}
        if d:
            out[bt] = d
    tri = {t.label: t.odds for t in (trifecta or []) if t.odds and t.odds > 0 and not t.absent}
    if tri:
        out["trifecta"] = tri
    return out


def _odds_hash(payload: dict) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _last_line(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        last = None
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last) if last else None
    except Exception:  # noqa: BLE001
        return None


def last_captured_at(race_id: str) -> float | None:
    """最後の capture の unix 時刻 (throttle 用)。記録なし/読めなければ None。"""
    line = _last_line(TIMELINE_DIR / f"{race_id}.jsonl")
    if not line:
        return None
    try:
        return dt.datetime.fromisoformat(line["captured_at"]).timestamp()
    except Exception:  # noqa: BLE001
        return None


def append_line(
    race_id: str, payload: dict, stage: str, *,
    close_at: int = 0, start_at: int = 0, odds_updated_at: int = 0,
    n_horses: int = 0, source: str | None = None,
) -> bool:
    """オッズ payload を timeline に 1 行 append する (低レベル API)。

    stage: "score" | "bet" | "poll"。直前行と同一オッズなら skip (False)。
    例外は全て呑んで False (解析を止めない)。
    """
    try:
        if not payload:
            return False
        h = _odds_hash(payload)
        path = TIMELINE_DIR / f"{race_id}.jsonl"
        last = _last_line(path)
        if last and last.get("odds_hash") == h:
            return False
        TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
        line = {
            "stage": stage,
            "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
            # 締切/発走/オッズ更新 unix (0 = 不明)。ドリフト解析の x 軸用。
            "close_at": close_at or 0,
            "start_at": start_at or 0,
            "odds_updated_at": odds_updated_at or 0,
            "n_horses": n_horses,
            "odds_hash": h,
            "odds": payload,
        }
        if source:
            line["source"] = source
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def capture(race_id: str, rd, stage: str) -> bool:
    """rd の現オッズを timeline に 1 行 append する (Step 1 hook 用)。

    stage: "score" | "bet"。直前行と同一オッズなら skip (False)。
    例外は全て呑んで False (解析を止めない)。
    """
    try:
        payload = build_payload(rd.other_bets, rd.trifecta)
        return append_line(
            race_id, payload, stage,
            close_at=getattr(rd.race, "close_at", 0) or 0,
            start_at=getattr(rd.race, "start_at", 0) or 0,
            odds_updated_at=getattr(rd.race, "odds_updated_at", 0) or 0,
            n_horses=len([h2 for h2 in rd.race.horses if not h2.absent]),
        )
    except Exception:  # noqa: BLE001
        return False
