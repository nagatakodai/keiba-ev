"use client";

import { useEffect, useRef, useState } from "react";
import { fmtTime } from "@/components/ui";

type LogEntry = { seq: number; ts: number; stream: string; text: string };

// 親側で `key={url}` を渡してマウントし直す前提。
// このコンポーネント自体は url を 1 つだけ subscribe する。
// 一時的な切断後は exponential backoff で auto-reconnect し、受信済 seq の
// 続きから (?since=N) を渡す。これで watch-auto のような long-running stream で
// ネットワーク瞬断後にユーザーが reload しなくてもログが復帰する。
export function LogStream({
  url,
  emptyHint = "(まだログがありません)",
  height = "h-[60vh]",
}: {
  url: string | null;
  emptyHint?: string;
  height?: string;
}) {
  const [lines, setLines] = useState<LogEntry[]>([]);
  const [status, setStatus] = useState<"idle" | "open" | "ended" | "error">(
    url ? "open" : "idle",
  );
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!url) return;
    const lastSeqRef = { current: -1 };
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    let es: EventSource | null = null;

    const connect = () => {
      if (closed) return;
      // 受信済 seq の続きから再開 (server 側 runner.py が ?since= を理解する)。
      const sep = url.includes("?") ? "&" : "?";
      const fullUrl =
        lastSeqRef.current >= 0
          ? `${url}${sep}since=${lastSeqRef.current + 1}`
          : url;
      es = new EventSource(fullUrl);
      es.addEventListener("log", (ev) => {
        try {
          const data = JSON.parse((ev as MessageEvent).data) as LogEntry;
          lastSeqRef.current = Math.max(lastSeqRef.current, data.seq);
          retry = 0; // 受信成功で backoff リセット
          setStatus("open");
          setLines((prev) => [...prev, data]);
        } catch {
          // ignore
        }
      });
      es.addEventListener("end", () => {
        closed = true;
        setStatus("ended");
        es?.close();
      });
      es.onerror = () => {
        es?.close();
        if (closed) return;
        // exponential backoff: 1s, 2s, 4s, 8s, 最大 30s
        retry = Math.min(retry + 1, 5);
        const delay = Math.min(1000 * 2 ** (retry - 1), 30000);
        setStatus("error");
        timer = setTimeout(connect, delay);
      };
    };
    connect();

    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      es?.close();
    };
  }, [url]);

  useEffect(() => {
    if (boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [lines]);

  return (
    <div
      ref={boxRef}
      className={`mono text-[12px] leading-5 bg-[#1a1d24] text-[#e6e8ee] border border-(--color-line) p-3 overflow-auto ${height}`}
    >
      {lines.length === 0 && (
        <div className="text-[#8b93a7]">{emptyHint}</div>
      )}
      {lines.map((l) => (
        <div key={l.seq} className="whitespace-pre-wrap break-words">
          <span className="text-[#8b93a7]">{fmtTime(l.ts)}</span>{" "}
          <span className={l.stream === "system" ? "text-[#e0a0d8]" : ""}>{l.text}</span>
        </div>
      ))}
      <div className="text-[#8b93a7] mt-2">
        {status === "open" && "[stream: open]"}
        {status === "ended" && "[stream: ended]"}
        {status === "error" && "[stream: error / disconnected]"}
      </div>
    </div>
  );
}

