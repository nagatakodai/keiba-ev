"use client";

import Link from "next/link";
import { useWatchStatus } from "./WatchStatusContext";

export function WatchPill() {
  const { status } = useWatchStatus();
  const running = !!status?.running;
  return (
    <Link
      href="/watch-auto"
      className={`inline-flex items-center gap-2 px-3 py-1 border rounded-full text-xs font-semibold transition-colors ${
        running
          ? "bg-(--color-good)/15 border-(--color-good)/60 text-(--color-good)"
          : "bg-(--color-surface-2) border-(--color-line) text-(--color-muted) hover:border-(--color-accent)/60"
      }`}
      title="自動予測分析・投票 の稼働状態"
    >
      <span
        className={`inline-block w-2 h-2 rounded-full ${
          running ? "bg-(--color-good) animate-pulse" : "bg-(--color-muted)/60"
        }`}
      />
      <span>自動予測分析・投票</span>
      <span>{running ? "稼働中" : "停止"}</span>
    </Link>
  );
}
