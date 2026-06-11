import type { Metadata } from "next";
import { Geist_Mono, Noto_Sans_JP } from "next/font/google";
import Link from "next/link";
import "./globals.css";
import { WatchPill } from "@/components/WatchPill";
import { BackgroundVideo } from "@/components/BackgroundVideo";
import { WatchStatusProvider } from "@/components/WatchStatusContext";
import { NavLinks } from "@/components/NavLinks";

const notoSansJP = Noto_Sans_JP({
  variable: "--font-noto-jp",
  subsets: ["latin"],
  weight: ["400", "500", "700", "900"],
  display: "swap",
});
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

// **タイトル方針** (2026-05-29 ユーザ指示): 全ページ `<ページ名> ｜ 競馬予想オーケストレーションAIの競愛`。
// 各ページが `export const metadata = { title: "<ページ名>" }` を返すと template が適用される。
// ルート (/) はクライアントコンポーネントで metadata を export できないため、default で
// 「ダッシュボード ｜ ...」を出す。
const BRAND_SUFFIX = "競馬予想オーケストレーションAIの競愛";

export const metadata: Metadata = {
  title: {
    template: `%s ｜ ${BRAND_SUFFIX}`,
    default: `ダッシュボード ｜ ${BRAND_SUFFIX}`,
  },
  description: "中央 (JRA) + 地方 (NAR) 競馬を確率モデル + Claude AI で解析し、3連単的中モードに特化した買い目を提示する競馬予想オーケストレーション AI「競愛」。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja" className={`${notoSansJP.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col bg-(--color-bg)">
        <WatchStatusProvider>
          <BackgroundVideo />
          {/* sticky glass ヘッダ: 1段目 = ブランド + WatchPill, 2段目 = ナビ (mobile は横スクロール) */}
          <header className="sticky top-0 z-20 glass bg-(--color-nav) border-b border-(--color-line)">
            <div className="max-w-7xl mx-auto px-4 h-13 flex items-center gap-4">
              <Link href="/" className="flex items-baseline gap-2.5 tracking-tight">
                <span className="mono text-base sm:text-lg font-bold tracking-tight">
                  KEIBA<span className="text-(--color-accent)">·EV</span>
                </span>
                <span className="text-sm font-bold">
                  <span className="text-(--color-accent)">競</span>
                  <span className="text-(--color-bad)">愛</span>
                </span>
                <span className="text-(--color-muted) text-xs font-normal hidden md:inline">
                  競馬予想オーケストレーションAI
                </span>
              </Link>
              <div className="ml-auto">
                <WatchPill />
              </div>
            </div>
            <div className="max-w-7xl mx-auto px-2 border-t border-(--color-line-soft)">
              <NavLinks />
            </div>
          </header>

          <main className="flex-1 relative z-[1]">{children}</main>

          <footer className="border-t border-(--color-line) text-(--color-muted) text-xs px-4 py-3 text-center relative z-[1]">
            長期 +EV 運用のための補助ツール。単発の勝敗で係数を変えない。
          </footer>
        </WatchStatusProvider>
      </body>
    </html>
  );
}
