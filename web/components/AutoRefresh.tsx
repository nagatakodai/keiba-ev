"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// RSC ページに置くだけで、指定秒数おきに `router.refresh()` を呼び、
// Server Component を再評価 + データを再フェッチさせる。
// 表示は何も出さない (タブが裏に回ったときは visibilitychange で停止)。
export function AutoRefresh({ seconds = 15 }: { seconds?: number }) {
  const router = useRouter();

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      stop();
      timer = setInterval(() => router.refresh(), seconds * 1000);
    };
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [router, seconds]);

  return null;
}
