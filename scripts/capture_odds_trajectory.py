"""CLV 用: NAR レースの単勝/複勝オッズを朝〜締切で複数回 capture して時系列保存。

CLV (closing line value) = 「朝の高いオッズで買い、賢い金が入って縮む前に先回りする」戦略の
基礎データ。我々は締切時刻のオッズしか持たないので、本スクリプトを racing hours 中に繰り返し
(例: 15-20分毎) 走らせ、各 NAR race の単勝オッズの時系列 (trajectory) を貯める。

出力 data/cache/odds_trajectory/<netkeiba_rid>.jsonl (1行=1回の capture):
  {ts, iso, sec_to_post, win:{馬番:オッズ}, place_min:{馬番:複勝下限}}

後で結果と突き合わせ:
  - CLV ドリフト計測 (締切前N分→確定でオッズがどう動くか、馬番別)
  - 「縮む馬」予測モデル (朝のオッズ + 特徴量 → オッズ変化方向)
を作る。これらは前向き (ライブ蓄積) でのみ検証可能。

使い方 (cron/loop で繰り返す):
  python scripts/capture_odds_trajectory.py            # 今日
  python scripts/capture_odds_trajectory.py 20260601   # 日付指定
  while true; do python scripts/capture_odds_trajectory.py; sleep 1200; done  # 20分毎
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scrape_oddspark import (  # noqa: E402
    fetch_oddspark_tanfuku,
    fetch_race_list_oddspark,
    find_oddspark_race,
)

OUT = ROOT / "data" / "cache" / "odds_trajectory"
MAX_LEAD_SEC = 6 * 3600   # 発走 6h より先の race は capture しない (無駄打ち回避)
MIN_LEAD_SEC = -120       # 発走後 2分まで (締切=発走2分前) は拾う


def capture_once(date: str | None = None) -> int:
    try:
        races = fetch_race_list_oddspark(date)
    except Exception as ex:  # noqa: BLE001
        print(f"  race list 取得失敗: {ex}", flush=True)
        return 0
    now = int(time.time())
    n_cap = 0
    OUT.mkdir(parents=True, exist_ok=True)
    for r in races:
        rid = r.get("netkeiba_race_id") or r.get("race_id")
        start_at = r.get("start_at") or 0
        if not rid or not start_at:
            continue
        sec = start_at - now
        if sec > MAX_LEAD_SEC or sec < MIN_LEAD_SEC:
            continue
        try:
            loc = find_oddspark_race(rid)
            if not loc:
                continue
            horses = fetch_oddspark_tanfuku(loc)
        except Exception:
            continue
        win = {h.number: h.win_odds for h in horses if getattr(h, "win_odds", 0) > 0}
        if not win:
            continue
        rec = {
            "ts": now,
            "iso": datetime.now().isoformat(timespec="seconds"),
            "sec_to_post": sec,
            "win": win,
            "place_min": {h.number: getattr(h, "place_min", 0)
                          for h in horses if getattr(h, "place_min", 0) > 0},
        }
        with open(OUT / f"{rid}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_cap += 1
        time.sleep(1.0)   # oddspark へ優しく
    return n_cap


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else None
    n = capture_once(date)
    print(f"captured {n} races at {datetime.now():%Y-%m-%d %H:%M:%S} "
          f"-> {OUT.relative_to(ROOT)}/", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
