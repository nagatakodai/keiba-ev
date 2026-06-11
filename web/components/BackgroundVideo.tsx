"use client";

import { useWatchStatus } from "./WatchStatusContext";

// 全ページ共通の背景演出。旧実装は ~1.4MB の video (atom-animation.webm/mp4) を
// 読み込んでいたが、純 CSS の aurora グラデーション (globals.css の .aurora-blob)
// に置換してネットワーク/デコードコストをゼロにした。コンポーネント名と mount
// 箇所 (layout.tsx) は互換のため維持。
// watch-auto 稼働中は少し明るく、停止中はほぼ見えない程度に落とす。
export function BackgroundVideo() {
  const { status } = useWatchStatus();
  const running = !!status?.running;

  return (
    <div
      aria-hidden
      className="fixed inset-0 overflow-hidden pointer-events-none z-0 select-none transition-opacity duration-700"
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
  );
}
