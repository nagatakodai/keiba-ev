"use client";

import { useEffect, useRef } from "react";
import { useWatchStatus } from "./WatchStatusContext";

// 全ページ共通の背景演出 = atom 動画 (右奥) + 純 CSS aurora グラデーション。
// 動画は黒背景素材を mix-blend-screen で背景に溶かし、radial mask で縁をフェード。
// watch-auto 稼働中のみ再生し、停止中は静止フレームのまま減光する (2026-06-12 ユーザ指示)。
// 素材は public/atom-animation.{webm,mp4} (720p 再エンコード、~0.7/0.9MB)。
export function BackgroundVideo() {
  const { status } = useWatchStatus();
  const running = !!status?.running;
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (running) {
      // muted + playsInline なので autoplay policy には通常通る。
      // 低電力モード等で reject されても背景演出なので無視。
      v.play().catch(() => {});
    } else {
      v.pause();
    }
  }, [running]);

  return (
    <div
      aria-hidden
      className="fixed inset-0 overflow-hidden pointer-events-none z-0 select-none"
    >
      {/* atom 動画: 右奥 (右端に一部はみ出す配置で「後ろに居る」感を出す) */}
      <video
        ref={videoRef}
        muted
        loop
        playsInline
        preload="auto"
        className="absolute mix-blend-screen transition-opacity duration-700"
        style={{
          top: "50%",
          right: "-18vmin",
          transform: "translateY(-50%)",
          width: "78vmin",
          height: "78vmin",
          objectFit: "cover",
          opacity: running ? 0.5 : 0.18,
          maskImage: "radial-gradient(closest-side, black 55%, transparent 100%)",
          WebkitMaskImage: "radial-gradient(closest-side, black 55%, transparent 100%)",
        }}
      >
        <source src="/atom-animation.webm" type="video/webm" />
        <source src="/atom-animation.mp4" type="video/mp4" />
      </video>

      <div
        className="transition-opacity duration-700"
        style={{ opacity: running ? 0.5 : 0.22 }}
      >
        {/* emerald: 右上 (利益/+EV のブランド色) */}
        <div
          className="aurora-blob"
          style={{
            top: "-18%",
            right: "-12%",
            width: "55vw",
            height: "55vh",
            background: "radial-gradient(closest-side, rgba(52,211,153,0.16), transparent)",
          }}
        />
        {/* blue: 左中央 (slate レイヤの blue tint と同系) */}
        <div
          className="aurora-blob"
          style={{
            top: "25%",
            left: "-15%",
            width: "50vw",
            height: "60vh",
            background: "radial-gradient(closest-side, rgba(59,130,246,0.13), transparent)",
            animationDelay: "-8s",
            animationDuration: "30s",
          }}
        />
        {/* violet: 右下 (Claude/LLM 色) */}
        <div
          className="aurora-blob"
          style={{
            bottom: "-22%",
            right: "10%",
            width: "45vw",
            height: "50vh",
            background: "radial-gradient(closest-side, rgba(167,139,250,0.11), transparent)",
            animationDelay: "-16s",
            animationDuration: "36s",
          }}
        />
      </div>
    </div>
  );
}
