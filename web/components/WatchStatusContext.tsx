"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, type WatchAutoStatus } from "@/lib/api";

// 全ページで watch-auto の running 状態を共有するためのコンテキスト。
// ステータスを 1 箇所だけポーリングし、複数の購読者 (WatchPill / BackgroundVideo /
// watch-auto ページ) に配信することで重複 fetch を避ける。

type Ctx = {
  status: WatchAutoStatus | null;
  refresh: () => Promise<void>;
};

const WatchStatusCtx = createContext<Ctx>({
  status: null,
  refresh: async () => {},
});

export function WatchStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<WatchAutoStatus | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api.watchStatus();
      setStatus(s);
    } catch {
      // ネットワーク / 認証失敗時は null のまま。BG video は静止、Pill は「停止」表示。
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <WatchStatusCtx.Provider value={{ status, refresh }}>
      {children}
    </WatchStatusCtx.Provider>
  );
}

export function useWatchStatus() {
  return useContext(WatchStatusCtx);
}
