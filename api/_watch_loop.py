"""watch-auto の永続ループを Python だけで回すラッパー。

bash + while ループを使うと、bash が SIGKILL されたとき孫の python (auto_watch)
が孤児として残る。bash は SIGKILL を trap できないため、子に signal を伝搬できない。

代わりにこの Python ラッパが:
1. 自身に PR_SET_PDEATHSIG=SIGKILL を設定 (uvicorn が死ねば自分も死ぬ)
2. 子プロセス (auto_watch) にも PR_SET_PDEATHSIG=SIGKILL を設定 (ラッパが死ねば子も死ぬ)
3. ループの間隔 (sleep) は SIGINT/SIGTERM で即中断
"""
from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time


def _set_pdeathsig(sig: int = signal.SIGKILL) -> None:
    if sys.platform != "linux":
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, sig)
    except Exception:
        pass


def _child_preexec() -> None:
    os.setsid()
    _set_pdeathsig(signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.argv
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("[_watch_loop] no command given", file=sys.stderr, flush=True)
        return 2

    _set_pdeathsig(signal.SIGKILL)
    stop = False

    def _handle_sig(signum: int, frame) -> None:  # noqa: ARG001
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    while not stop:
        try:
            proc = subprocess.Popen(
                cmd,
                preexec_fn=_child_preexec,
            )
        except Exception as e:
            print(f"[_watch_loop] spawn failed: {e}", flush=True)
            _interruptible_sleep(args.interval, lambda: stop)
            continue

        try:
            rc = proc.wait()
            if rc != 0:
                print(f"[_watch_loop] child rc={rc}", flush=True)
        except KeyboardInterrupt:
            stop = True
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

        if stop:
            break
        print(f"[next poll in {args.interval}s]", flush=True)
        _interruptible_sleep(args.interval, lambda: stop)

    return 130 if stop else 0


def _interruptible_sleep(seconds: int, should_stop) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if should_stop():
            return
        # max(0, ...) で負値 (race: time advances between check & sleep) を防ぐ
        remaining = end - time.time()
        time.sleep(max(0.0, min(0.5, remaining)))


if __name__ == "__main__":
    sys.exit(main())
