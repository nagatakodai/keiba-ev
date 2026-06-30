"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  FlaskConical,
  ListOrdered,
  MapPin,
  Radio,
  Swords,
  Target,
  type LucideIcon,
} from "lucide-react";

// グローバルナビ。usePathname でアクティブ状態を付けるため client component。
// layout.tsx (server) からはこのコンポーネントを mount するだけ。
const NAV: { href: string; label: string; icon: LucideIcon }[] = [
  { href: "/", label: "ダッシュボード", icon: LayoutDashboard },
  { href: "/venues", label: "競馬場別", icon: MapPin },
  { href: "/shobu", label: "今日の勝負レース", icon: Swords },
  { href: "/analyze", label: "レース予測分析", icon: FlaskConical },
  { href: "/predictions", label: "予測分析履歴", icon: ListOrdered },
  { href: "/watch-auto", label: "自動予測分析・投票", icon: Radio },
  { href: "/calibrate", label: "確率較正", icon: Target },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  // /predictions/[raceId] や /predictions/archive も /predictions をアクティブに
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function NavLinks() {
  const pathname = usePathname() ?? "/";
  return (
    <nav className="flex items-center gap-0.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = isActive(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={`relative flex items-center gap-1.5 px-3 h-11 text-[13px] font-medium whitespace-nowrap transition-colors ${
              active
                ? "text-(--color-accent)"
                : "text-(--color-muted) hover:text-(--color-foreground) hover:bg-white/5"
            }`}
          >
            <Icon size={15} strokeWidth={active ? 2.4 : 2} aria-hidden />
            {label}
            {/* アクティブ下線 (emerald) */}
            {active && (
              <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-(--color-accent)" />
            )}
          </Link>
        );
      })}
    </nav>
  );
}
