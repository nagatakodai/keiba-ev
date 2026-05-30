import type { Metadata } from "next";
import Link from "next/link";
import {
  api,
  type CalibrationReport,
  type PredictionSummary,
  type WatchAutoHistoryItem,
} from "@/lib/api";

export const metadata: Metadata = { title: "予測分析履歴" };
import { Page, PageHeader, savedAtDate, todayJST } from "@/components/ui";
import { PredictionsList } from "@/components/PredictionsList";
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

  const [data, cal, watchHist] = await Promise.all([
    api
      .listPredictions(500)
      .catch(() => ({ items: [] as PredictionSummary[] })),
    api.calibrate().catch(() => null as CalibrationReport | null),
    api
      .watchHistory(500)
      .catch(() => ({ items: [] as WatchAutoHistoryItem[] })),
  ]);

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
        title="予測分析履歴"
        subtitle={`本日 ${today} 分のみ表示 (会場ごと / R 番号順)。`}
        right={
          <div className="flex items-center gap-3 text-xs">
            <Link
              href={`/predictions${showHits ? "?hits=off" : ""}`}
              className={`px-2 py-1 border ${
                showHits
                  ? "bg-(--color-accent) text-white border-(--color-accent)"
                  : "bg-white border-(--color-line) text-(--color-foreground) hover:border-(--color-accent)"
              } font-bold`}
            >
              的中 {showHits ? "ON" : "OFF"}
            </Link>
            {pastCount > 0 && (
              <Link
                href="/predictions/archive"
                className="text-(--color-accent) hover:underline"
              >
                過去の予測分析履歴 ({pastCount}) →
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
