"""bet 予約の**精密時刻発火**デーモン (watch-auto の poll とは独立した別プロセス)。

watch-auto は score 帯で各馬指数をキャッシュしつつ `data/cache/auto_watch_bet_schedule/<rid>.json`
に「締切 bet_lead_sec 秒前に投票」を予約する (src/auto_watch.py:_write_bet_schedule)。本デーモンは
その予約を読み、**各レースの発火時刻 (close_at - bet_lead_sec) ちょうどまで精密に sleep して撃つ**。

なぜ別プロセスか:
  - watch-auto 本体は毎 tick (既定60s) フレッシュ subprocess で起動するので、tick 間で精密タイマを
    保持できない。発火を tick に相乗りさせると最大 1 tick (60s) ずれる。
  - そこで発火だけを常駐の本デーモンに分離する。**発火精度は poll/tick に縛られず秒単位**。

ループ:
  1. 期限の来た予約を `auto_watch._fire_due_bets` で発火 (最新オッズ→束→enqueue、二重防止は
     analyzed_bet + 予約ファイル削除)。
  2. 残予約の中で**最も早い発火時刻**まで sleep。ただし `--rescan` 秒を上限にして、score 帯で
     後から書かれた新規予約を取りこぼさない (新規予約は発火の数分前に書かれるので、この上限は
     発火精度に影響しない = 発火は常に時刻ちょうど)。

使い方:
  python -m src.bet_scheduler [--bet-lead-sec=60] [--bet-oddspark] [--bet-ipat]
        [--market-blend=X] [--aptitude-top=N] [--llm-blend=X] [--no-llm] [--rescan=15]

watch-auto / 投票 daemon と一緒に常駐させる (make bet / Web UI が自動起動)。Ctrl-C で終了。
"""
from __future__ import annotations

import sys
import time

from . import auto_watch as aw

# 残予約が無い / 最早発火が遠いときの再スキャン上限 (秒)。新規予約 (score 帯で発火の数分前に
# 書かれる) を拾うためのもので、発火精度には影響しない。短いほど新規予約の取り込みが速い。
RESCAN_SEC_DEFAULT = 15


def run_scheduler(
    *, bet_lead_sec: int = aw.BET_LEAD_SEC_DEFAULT, rescan_sec: int = RESCAN_SEC_DEFAULT,
    market_blend=None, aptitude_top=None, no_llm: bool = False, llm_blend=None,
    bet_oddspark: bool = False, bet_ipat: bool = False,
) -> None:
    print(f"[bet_scheduler] 起動: 締切 {bet_lead_sec}s 前に精密発火 "
          f"(再スキャン上限 {rescan_sec}s, bet_oddspark={bet_oddspark} bet_ipat={bet_ipat})",
          flush=True)
    while True:
        now = time.time()
        try:
            aw._fire_due_bets(
                int(now), bet_lead_sec=bet_lead_sec, market_blend=market_blend,
                aptitude_top=aptitude_top, no_llm=no_llm, llm_blend=llm_blend,
                bet_oddspark=bet_oddspark, bet_ipat=bet_ipat, dry_run=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[bet_scheduler] fire error: {e}", flush=True)

        # 残予約の中で最も早い「未来の発火時刻」まで精密 sleep (上限 rescan_sec)。
        now = time.time()
        fire_times = [
            int(s.get("close_at") or 0) - bet_lead_sec
            for s in aw._read_bet_schedule()
            if s.get("close_at")
        ]
        future = [t for t in fire_times if t > now]
        if future:
            nxt = min(future)
            sleep_for = max(1.0, min(float(rescan_sec), nxt - now))
        else:
            sleep_for = float(rescan_sec)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            print("[bet_scheduler] 終了", flush=True)
            return


def _main() -> None:
    argv = sys.argv[1:]
    bet_lead_sec = aw.BET_LEAD_SEC_DEFAULT
    rescan_sec = RESCAN_SEC_DEFAULT
    market_blend = None
    aptitude_top = None
    llm_blend = None
    for a in argv:
        if a.startswith("--bet-lead-sec="):
            try:
                bet_lead_sec = max(0, int(a.split("=", 1)[1]))
            except ValueError:
                pass
        elif a.startswith("--rescan="):
            try:
                rescan_sec = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                pass
        elif a.startswith("--market-blend="):
            try:
                market_blend = float(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a.startswith("--aptitude-top="):
            try:
                aptitude_top = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a.startswith("--llm-blend="):
            try:
                llm_blend = float(a.split("=", 1)[1])
            except ValueError:
                pass
    try:
        run_scheduler(
            bet_lead_sec=bet_lead_sec, rescan_sec=rescan_sec,
            market_blend=market_blend, aptitude_top=aptitude_top,
            no_llm="--no-llm" in argv, llm_blend=llm_blend,
            bet_oddspark="--bet-oddspark" in argv, bet_ipat="--bet-ipat" in argv,
        )
    except KeyboardInterrupt:
        print("[bet_scheduler] 終了", flush=True)


if __name__ == "__main__":
    _main()
