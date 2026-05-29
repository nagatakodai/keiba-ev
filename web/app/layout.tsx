import type { Metadata } from "next";
import { Geist_Mono, Noto_Sans_JP } from "next/font/google";
import Link from "next/link";
import "./globals.css";
import { WatchPill } from "@/components/WatchPill";
import { BackgroundVideo } from "@/components/BackgroundVideo";
import { WatchStatusProvider } from "@/components/WatchStatusContext";

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
  description: "中央 (JRA) + 地方 (NAR) 競馬の全7券種を確率モデル + Claude AI で解析し、回収/的中の 2 軸で総合オススメを提示する競馬予想オーケストレーション AI「競愛」。",
};

const NAV = [
  { href: "/", label: "ダッシュボード" },
  { href: "/analyze", label: "レース予測分析" },
  { href: "/predictions", label: "予測分析履歴" },
  { href: "/watch-auto", label: "自動予測分析・投票" },
  { href: "/calibrate", label: "確率較正" },
];

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja" className={`${notoSansJP.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col bg-(--color-panel-2)">
        <WatchStatusProvider>
          <BackgroundVideo />
          <header className="bg-white border-b border-(--color-line) sticky top-0 z-20">
            <div className="max-w-7xl mx-auto px-4 h-12 flex items-center gap-4">
              <Link href="/" className="flex items-baseline gap-1.5 font-bold tracking-tight">
                <span className="text-lg sm:text-xl">
                  <span className="text-(--color-accent)">競</span>
                  <span className="text-(--color-bad)">愛</span>
                </span>
                <span className="text-(--color-muted) text-xs font-normal hidden sm:inline">
                  ｜ 競馬予想オーケストレーションAI
                </span>
              </Link>
              <div className="ml-auto">
                <WatchPill />
              </div>
            </div>
          </header>

          <nav className="bg-(--color-nav) text-(--color-nav-text) sticky top-12 z-10 shadow-sm">
            <div className="max-w-7xl mx-auto px-2 h-10 flex items-center text-sm font-medium">
              {NAV.map((n) => (
                <Link
                  key={n.href}
                  href={n.href}
                  className="px-4 h-10 flex items-center hover:bg-black/10 transition-colors"
                >
                  {n.label}
                </Link>
              ))}
            </div>
          </nav>

          <main className="flex-1 relative z-[1]">{children}</main>

          <footer className="bg-white border-t border-(--color-line) text-(--color-muted) text-xs px-4 py-3 text-center relative z-[1]">
            長期 +EV 運用のための補助ツール。単発の勝敗で係数を変えない。
          </footer>
        </WatchStatusProvider>
      </body>
    </html>
  );
}
