import type { Metadata } from "next";
import Link from "next/link";
import { ChevronRight, History } from "lucide-react";
import {
  api,
  type CalibrationReport,
  type PredictionSummary,
  type WatchAutoHistoryItem,
} from "@/lib/api";

export const metadata: Metadata = { title: "予測分析履歴" };
import { Page, PageHeader, fmtTime, savedAtDate, todayJST } from "@/components/ui";
import { HitsToggle, PredictionsList } from "@/components/PredictionsList";
import { AutoRefresh } from "@/components/AutoRefresh";

export const dynamic = "force-dynamic";

type RaceHit = CalibrationReport["races"][number];

export default async function PredictionsPage({
  searchParams,
}: {
  searchParams: Promise<{ hits?: string }>;
}) {
  const sp = await searchParams;
  const showHits = sp.hits !== "off"; // デフォルト表示

  const [data, cal, watchHist, resultsAuto] = await Promise.all([
    api
      .listPredictions(500)
      .catch(() => ({ items: [] as PredictionSummary[] })),
    api.calibrate().catch(() => null as CalibrationReport | null),
    api
      .watchHistory(500)
      .catch(() => ({ items: [] as WatchAutoHistoryItem[] })),
    api.getResultsAuto().catch(() => null),
  ]);

  // RSC はリクエスト毎に 1 回だけ描画されるので、リクエスト時刻の取得は安全。
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();
  const today = todayJST(nowMs);
  const todays = data.items.filter((p) => savedAtDate(p.saved_at) === today);
  const pastCount = data.items.length - todays.length;

  const raceHitMap = new Map<string, RaceHit>();
  for (const r of cal?.races ?? []) raceHitMap.set(r.race_id, r);
  // snapshot に close_at/start_at が乗らない fresh fetch (claude 評価中) 用 fallback。
  const closeAtMap = new Map<string, number>();
  const startAtMap = new Map<string, number>();
  for (const h of watchHist.items) {
    if (h.race_id && h.close_at) closeAtMap.set(h.race_id, h.close_at);
    if (h.race_id && h.start_at != null) startAtMap.set(h.race_id, h.start_at);
  }

  return (
    <Page>
      <AutoRefresh seconds={60} />
      <PageHeader
        eyebrow="Predictions"
        title="予測分析履歴"
        subtitle={
          <>
            本日 {today} 分のみ表示 (会場ごと / R 番号順)。
            {resultsAuto && (
              <span className="ml-1.5 text-(--color-muted)">
                · 結果自動取得{" "}
                {resultsAuto.loop_running ? (
                  <span className="text-(--color-accent)">
                    {Math.round(resultsAuto.interval_sec / 60)}分毎
                  </span>
                ) : (
                  <span className="text-(--color-bad)">停止 (make api 未起動?)</span>
                )}
                {resultsAuto.last_run_at
                  ? ` · 前回 ${fmtTime(resultsAuto.last_run_at)}`
                  : " · 未実行"}
              </span>
            )}
          </>
        }
        right={
          <div className="flex flex-wrap items-center justify-end gap-3">
            <HitsToggle basePath="/predictions" showHits={showHits} />
            {pastCount > 0 && (
              <Link
                href="/predictions/archive"
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-(--color-line) bg-(--color-surface-2) text-xs font-bold text-(--color-foreground) hover:border-(--color-accent)/60 hover:text-(--color-accent) transition-colors"
              >
                <History className="size-3.5" />
                過去の予測分析履歴
                <span className="tnum text-(--color-muted)">({pastCount})</span>
                <ChevronRight className="size-3.5" />
              </Link>
            )}
          </div>
        }
      />
      <PredictionsList
        items={todays}
        nowMs={nowMs}
        raceHitMap={raceHitMap}
        closeAtMap={closeAtMap}
        startAtMap={startAtMap}
        showHits={showHits}
        emptyMessage={
          pastCount > 0
            ? `本日の予測はまだありません。過去分 ${pastCount} 件は「過去の予測分析履歴」へ。`
            : "まだ予測はありません。"
        }
      />
    </Page>
  );
}
