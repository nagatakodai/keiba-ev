import Link from "next/link";
import {
  api,
  type CalibrationReport,
  type PredictionSummary,
  type WatchAutoHistoryItem,
} from "@/lib/api";
import { Page, PageHeader, savedAtDate, todayJST } from "@/components/ui";
import { PredictionsList } from "@/components/PredictionsList";

export const dynamic = "force-dynamic";

type RaceHit = CalibrationReport["races"][number];

export default async function PredictionsArchivePage({
  searchParams,
}: {
  searchParams: Promise<{ hits?: string }>;
}) {
  const sp = await searchParams;
  const showHits = sp.hits !== "off";

  const [data, cal, watchHist] = await Promise.all([
    api
      .listPredictions(1000)
      .catch(() => ({ items: [] as PredictionSummary[] })),
    api.calibrate().catch(() => null as CalibrationReport | null),
    api
      .watchHistory(1000)
      .catch(() => ({ items: [] as WatchAutoHistoryItem[] })),
  ]);

  const nowMs = Date.now();
  const today = todayJST(nowMs);
  const past = data.items.filter((p) => {
    const d = savedAtDate(p.saved_at);
    return d !== "" && d !== today;
  });

  const raceHitMap = new Map<string, RaceHit>();
  for (const r of cal?.races ?? []) raceHitMap.set(r.race_id, r);
  const closeAtMap = new Map<string, number>();
  const startAtMap = new Map<string, number>();
  for (const h of watchHist.items) {
    if (h.race_id && h.close_at) closeAtMap.set(h.race_id, h.close_at);
    if (h.race_id && h.start_at != null) startAtMap.set(h.race_id, h.start_at);
  }

  return (
    <Page>
      <PageHeader
        title="過去の予測履歴"
        subtitle={`本日 (${today}) より前の予測 ${past.length} 件。`}
        right={
          <div className="flex items-center gap-3 text-xs">
            <Link
              href={`/predictions/archive${showHits ? "?hits=off" : ""}`}
              className={`px-2 py-1 border ${
                showHits
                  ? "bg-(--color-accent) text-white border-(--color-accent)"
                  : "bg-white border-(--color-line) text-(--color-foreground) hover:border-(--color-accent)"
              } font-bold`}
            >
              的中 {showHits ? "ON" : "OFF"}
            </Link>
            <Link
              href="/predictions"
              className="text-(--color-accent) hover:underline"
            >
              ← 本日へ戻る
            </Link>
          </div>
        }
      />
      <PredictionsList
        items={past}
        nowMs={nowMs}
        raceHitMap={raceHitMap}
        closeAtMap={closeAtMap}
        startAtMap={startAtMap}
        showHits={showHits}
        emptyMessage="過去の予測はありません。"
      />
    </Page>
  );
}
