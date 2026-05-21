"use client";

import { useEffect, useRef, useState } from "react";
import { fmtTime } from "@/components/ui";

type LogEntry = { seq: number; ts: number; stream: string; text: string };

// 親側で `key={url}` を渡してマウントし直す前提。
// このコンポーネント自体は url を 1 つだけ subscribe する。
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
    const es = new EventSource(url);
    es.addEventListener("log", (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data) as LogEntry;
        setLines((prev) => [...prev, data]);
      } catch {
        // ignore
      }
    });
    es.addEventListener("end", () => {
      setStatus("ended");
      es.close();
    });
    es.onerror = () => {
      setStatus("error");
      es.close();
    };
    return () => es.close();
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

