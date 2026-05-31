"""block 解除を polling し、解除され次第 rids_retry.txt を再取得する一回限りの常駐スクリプト。

bulk_fetch 終盤の rate-limit で取りこぼした 143 race を、netkeiba block 解除後に自動で埋める。
両ドメイン (race./nar.) の race_list を probe し、両方 reachable になったら bulk_fetch を起動。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
RIDS = ROOT / "data" / "cache" / "rids_retry.txt"
POLL_SEC = 1200  # 20 分

sys.path.insert(0, str(ROOT))
from src.scrape import fetch_html, race_list_url  # noqa: E402


def reachable() -> bool:
    """nar/race 両ドメインの race_list が実 HTML を返すか。"""
    for nar in (True, False):
        try:
            html = fetch_html(race_list_url("20260530", nar=nar))
        except Exception:
            return False
        if not html or len(html) < 2000:
            return False
    return True


def main() -> int:
    print(f"[retry-poller] start; probing every {POLL_SEC}s", flush=True)
    while True:
        if reachable():
            print("[retry-poller] netkeiba reachable -> launching bulk_fetch retry", flush=True)
            break
        print("[retry-poller] still blocked; sleeping", flush=True)
        time.sleep(POLL_SEC)

    rc = subprocess.call(
        [PY, "-m", "src.bulk_fetch", "--rids-file", str(RIDS),
         "--workers", "2", "--polite-ms", "2000", "--block-cooldown", "600"],
        cwd=str(ROOT),
    )
    print(f"[retry-poller] bulk_fetch exited rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
