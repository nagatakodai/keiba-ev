"use client";

import { useEffect, useRef } from "react";
import { useWatchStatus } from "./WatchStatusContext";

// 全ページ共通の背景アニメーション。
// watch-auto が稼働中はループ再生 + 高めの opacity、停止中は一時停止 + ほぼ非表示。
// 画面右端に大きく配置し、コンテンツの邪魔にならない z-0 / pointer-events-none。
export function BackgroundVideo() {
  const { status } = useWatchStatus();
  const running = !!status?.running;
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (running) {
      v.play().catch(() => {});
    } else {
      v.pause();
      v.currentTime = 0;
    }
  }, [running]);

  return (
    <video
      ref={videoRef}
      muted
      loop
      playsInline
      autoPlay
      preload="auto"
      aria-hidden
      className="fixed top-1/2 -translate-y-1/2 pointer-events-none z-0 select-none transition-opacity duration-700 w-auto max-w-none right-[-30vw] h-[55vh] sm:right-[-22vw] sm:h-[70vh] md:right-[-12vw] md:h-[95vh]"
      // webm を先に: Chrome/Firefox は alpha チャンネル付き webm を使い黒背景が透過される。
      // iOS Safari は webm 未対応のため mp4 にフォールバック。
      // mp4 は alpha なし (黒背景に変換済み) → screen blend で黒を消す。
      // webm 側では transparent な画素は screen に影響しないので無害。
      style={{
        opacity: running ? 0.45 : 0.1,
        mixBlendMode: "screen",
      }}
    >
      <source src="/atom-animation.webm" type="video/webm" />
      <source src="/atom-animation.mp4" type="video/mp4" />
    </video>
  );
}
