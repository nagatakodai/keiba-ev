import type { Metadata } from "next";
import type { ReactNode } from "react";

// `analyze/page.tsx` は client component なので metadata export 不可。
// この layout が title 提供を担う (template は root layout 側で適用される)。
export const metadata: Metadata = { title: "レース予測分析" };

export default function AnalyzeLayout({ children }: { children: ReactNode }) {
  return children;
}
