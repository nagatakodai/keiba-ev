import type { Metadata } from "next";
import type { ReactNode } from "react";

// `watch-auto/page.tsx` は client component なので metadata export 不可。
// この layout が title 提供を担う (template は root layout 側で適用される)。
export const metadata: Metadata = { title: "自動予測分析・投票" };

export default function WatchAutoLayout({ children }: { children: ReactNode }) {
  return children;
}
