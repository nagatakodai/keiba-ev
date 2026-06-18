"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import { Badge, Button } from "@/components/ui";
import { LogStream } from "@/components/LogStream";
import { api, type JobInfo } from "@/lib/api";

// 履歴詳細ページのクライアント島。今すぐ最新オッズで score 段を再評価し、
// 完了したら router.refresh() でサーバーコンポーネントを再描画して最新 snapshot を出す。
export function OddsRefreshButton({
  raceId,
  canRefresh = true,
  closeAt,
}: {
  raceId: string;
  canRefresh?: boolean | null;
  closeAt?: number | null;
}) {
  const router = useRouter();
  const [job, setJob] = useState<JobInfo | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshedRef = useRef(false);

  const jobActive = job != null && (job.status === "running" || job.status === "pending");
  // 締切を過ぎたら fresh odds は存在しないので無効化 (2 分の grace 込み)。
  const pastClose = closeAt != null && closeAt < Date.now() / 1000 - 120;
  const disabled = submitting || jobActive || pastClose || canRefresh === false;

  // job 実行中は getJob を polling し、終了したら一度だけ画面を再取得 (LogStream は完了通知を出さない)。
  useEffect(() => {
    if (!job || !jobActive) return;
    const t = setInterval(async () => {
      try {
        const j = await api.getJob(job.id);
        setJob(j);
        if (j.status !== "running" && j.status !== "pending") {
          clearInterval(t);
          if (!refreshedRef.current) {
            refreshedRef.current = true;
            router.refresh();
          }
        }
      } catch {
        /* 一時的な失敗は無視 (次 tick で再試行) */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [job, jobActive, router]);

  const run = async () => {
    setSubmitting(true);
    setError(null);
    refreshedRef.current = false;
    try {
      const j = await api.refreshOdds(raceId);
      setJob(j);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const title = pastClose
    ? "締切済 — 最新オッズ取得不可"
    : canRefresh === false
      ? "このレースは再取得不可 (race_id から復元不能)"
      : "今すぐ最新オッズで score を再取得 (暫定 snapshot を更新)";

  return (
    <div className="flex flex-col items-end gap-1" title={title}>
      <Button size="sm" variant="ghost" onClick={run} disabled={disabled}>
        {submitting || jobActive ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
        ) : (
          <RefreshCw className="w-3.5 h-3.5" />
        )}
        オッズ更新
      </Button>
      {error && (
        <span className="inline-flex items-center gap-1 text-xs text-rose-300 max-w-64 text-right">
          <AlertTriangle className="w-3 h-3 shrink-0" />
          <span className="break-all">{error}</span>
        </span>
      )}
      {job && (
        <div className="w-72 max-w-[80vw] mt-1">
          <div className="flex items-center gap-1.5 mb-1 text-xs text-(--color-muted) justify-end">
            <Badge tone={statusTone(job.status)}>{job.status}</Badge>
            <span className="mono">{job.id.slice(0, 8)}</span>
          </div>
          <LogStream key={job.id} url={`/api/jobs/${job.id}/stream`} height="h-40" />
        </div>
      )}
    </div>
  );
}

function statusTone(s: JobInfo["status"]): "good" | "warn" | "bad" | "default" | "muted" {
  if (s === "running" || s === "pending") return "warn";
  if (s === "done") return "good";
  if (s === "failed") return "bad";
  if (s === "cancelled") return "muted";
  return "default";
}
