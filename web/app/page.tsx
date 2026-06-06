import Link from "next/link";
import {
  api,
  type CalibrationReport,
  type PredictionSummary,
  type WatchAutoHistoryItem,
} from "@/lib/api";
import {
  Badge,
  Card,
  Page,
  PageHeader,
  Stat,
  calibrationConfidence,
  fmtPct,
  fmtRelativeFromNow,
  fmtServerDateTime,
  fmtYen,
  parsePlanLabel,
  planAccentClass,
  planBarClass,
  raceTimingRowBg,
  raceTimingStatus,
  savedAtDate,
  todayJST,
  type PlanLetter,
} from "@/components/ui";
import { AutoRefresh } from "@/components/AutoRefresh";
import { PredictionsList } from "@/components/PredictionsList";

export const dynamic = "force-dynamic";

type RaceHit = CalibrationReport["races"][number];

function PlanHitTag({ plan, hit }: { plan: PlanLetter; hit: boolean }) {
  return hit ? (
    <span className={`font-bold ${planAccentClass(plan)}`}>{plan} ✓</span>
  ) : (
    <span className="font-bold text-(--color-muted)">{plan} ×</span>
  );
}

function roiTone(roi: number): "good" | "warn" | "bad" {
  if (roi >= 1) return "good";
  if (roi >= 0.85) return "warn";
  return "bad";
}

type PlanWithCi = {
  hits: number;
  participated_races: number;
  hit_rate: number;
  roi: number;
  roi_ci_low?: number;
  roi_ci_high?: number;
};

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}

// 累積収支推移 (3連単的中モード = 実弾投票束) + 結果分布の簡易 SVG チャート群。
// recharts 等の重い依存を増やさず Tailwind + inline SVG で軽量描画する。
// EV束 (モデル参考) の系列はダッシュボードから削除 (2026-06-06 ユーザ指示: EV束は不要)。
function DashboardCharts({ races }: { races: RaceHit[] }) {
  // 3連単的中モードの計測対象 (trifecta_measured, 計測開始日以降) のみ。
  // field 欠落 (旧 API) は従来通り含める。
  const measured = races.filter((r) => r.trifecta_measured !== false);
  // saved_at 昇順 (古い順) で並べて累積 stake / payout を計算
  const sorted = [...measured].sort((a, b) =>
    (a.saved_at ?? "").localeCompare(b.saved_at ?? ""),
  );

  // 累積収支: 参加レースのみ (見送りは stake/payout 0 で実質スキップ)。
  // 3連単的中モード (実弾投票束, 2026-06-06〜固定) のみ。
  let tStakeAcc = 0;
  let tPayoutAcc = 0;
  const series = sorted.map((r) => {
    if (r.trifecta_bundle_participated) {
      tStakeAcc += r.trifecta_bundle_stake ?? 0;
      // 最終オッズ基準 (実払戻に近い)。最終が無い旧 result は予想 payout に fallback。
      tPayoutAcc += r.trifecta_bundle_payout_final ?? r.trifecta_bundle_payout ?? 0;
    }
    return {
      tNet: tPayoutAcc - tStakeAcc,
      tRoi: tStakeAcc > 0 ? tPayoutAcc / tStakeAcc : 0,
    };
  });
  const tNetSeries = series.map((s) => s.tNet);
  const tRoiSeries = series.map((s) => s.tRoi);

  // 3連単的中モードの結果分布: 的中 / 不的中 / 見送り レース数 (計測対象のみ)
  const tHits = measured.filter((r) => r.trifecta_bundle_hit).length;
  const tMisses = measured.filter(
    (r) => r.trifecta_bundle_participated && !r.trifecta_bundle_hit,
  ).length;
  const tSkips = measured.filter((r) => r.trifecta_bundle_participated === false).length;

  return (
    <section className="space-y-3">
      <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
        <span className="inline-block w-1 h-4 bg-(--color-highlight) translate-y-0.5" />
        <span className="text-base">チャート</span>
        <span className="text-xs font-normal text-(--color-muted)">
          累積収支 / 結果分布
        </span>
      </h2>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Card title="累積収支">
          <LineChart
            seriesA={tNetSeries}
            labelA="3連単的中 (実弾)"
            colorA="#d946ef"
            yFmt={(v) => `${v >= 0 ? "+" : ""}${(v / 1000).toFixed(1)}k`}
          />
        </Card>
        <Card title="累積回収率 推移">
          <LineChart
            seriesA={tRoiSeries.map((v) => v * 100)}
            labelA="3連単的中 (実弾)"
            colorA="#d946ef"
            yFmt={(v) => `${v.toFixed(0)}%`}
            referenceY={100}
          />
        </Card>
        <Card title="結果分布">
          <DistroBar
            label="3連単的中モード (実弾)"
            hits={tHits}
            misses={tMisses}
            skips={tSkips}
          />
        </Card>
      </div>
    </section>
  );
}

function LineChart({
  seriesA, labelA, colorA, yFmt, referenceY,
}: {
  seriesA: number[];
  labelA: string;
  colorA: string;
  yFmt: (v: number) => string;
  referenceY?: number;
}) {
  const w = 600;
  const h = 180;
  const pad = { l: 48, r: 12, t: 8, b: 18 };
  const allValues = [...seriesA, ...(referenceY !== undefined ? [referenceY] : [])];
  const minY = Math.min(0, ...allValues);
  const maxY = Math.max(0, ...allValues);
  const rangeY = maxY - minY || 1;
  const n = seriesA.length;
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const xAt = (i: number) =>
    pad.l + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const yAt = (v: number) =>
    pad.t + innerH - ((v - minY) / rangeY) * innerH;
  const lineFor = (s: number[]) =>
    s.map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
  const ticks = [minY, minY + rangeY * 0.5, maxY];

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-auto" preserveAspectRatio="none">
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={w - pad.r} y1={yAt(t)} y2={yAt(t)}
                  stroke="#e5e7eb" strokeWidth={1} />
            <text x={pad.l - 6} y={yAt(t) + 3} fontSize={10}
                  fill="#6b7280" textAnchor="end">{yFmt(t)}</text>
          </g>
        ))}
        {referenceY !== undefined && (
          <line x1={pad.l} x2={w - pad.r} y1={yAt(referenceY)} y2={yAt(referenceY)}
                stroke="#94a3b8" strokeWidth={1} strokeDasharray="4 2" />
        )}
        {seriesA.length > 0 && (
          <path d={lineFor(seriesA)} fill="none" stroke={colorA} strokeWidth={2} />
        )}
        {/* legend */}
        <g transform={`translate(${pad.l + 4}, ${pad.t + 12})`}>
          <rect width={9} height={9} fill={colorA} />
          <text x={14} y={9} fontSize={10} fill="#374151">{labelA}</text>
        </g>
      </svg>
    </div>
  );
}

function DistroBar({
  label, hits, misses, skips,
}: {
  label: string;
  hits: number;
  misses: number;
  skips: number;
}) {
  const total = hits + misses + skips;
  if (total === 0) return null;
  const hPct = (hits / total) * 100;
  const mPct = (misses / total) * 100;
  const sPct = (skips / total) * 100;
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs mb-1">
        <span className="font-bold">{label}</span>
        <span className="text-(--color-muted) tabnum">
          的中 {hits} ／ 不的中 {misses} ／ 見送り {skips}
        </span>
      </div>
      <div className="flex h-5 overflow-hidden border border-(--color-line)">
        <div className="bg-emerald-500" style={{ width: `${hPct}%` }} title={`的中 ${hits}`} />
        <div className="bg-red-400" style={{ width: `${mPct}%` }} title={`不的中 ${misses}`} />
        <div className="bg-slate-300" style={{ width: `${sPct}%` }} title={`見送り ${skips}`} />
      </div>
    </div>
  );
}

// 表示方針: CI 範囲を常に [low, high] で出す。
// ±X 形式は CI が対称な前提だが、ROI の bootstrap CI は小サンプルだと
// 強く skewed (下限 0 ・上限大) になるので「±1849%」のように本来の
// 情報量より広く見える誤った印象を与える。
function planRoiHint(p: PlanWithCi): string {
  const base = `hit ${p.hits}/${p.participated_races} (${fmtPct(p.hit_rate, 1)})`;
  if (p.roi_ci_low === undefined || p.roi_ci_high === undefined) return base;
  const tail =
    p.participated_races < 30 ? " · 参考値 (n<30)" : "";
  return `${base} · CI [${fmtRoiPct(p.roi_ci_low)}, ${fmtRoiPct(p.roi_ci_high)}]${tail}`;
}

// 小サンプル時は tone を warn に落として、ROI 値だけで緑表示しない。
function planStatTone(
  p: PlanWithCi | undefined,
): "default" | "good" | "warn" | "bad" {
  if (!p || p.participated_races === 0) return "default";
  if (p.participated_races < 30) return "warn"; // n<30 は判断材料未満
  return roiTone(p.roi);
}

// 最新 / 最新の的中 で共用する 1 行レンダラ。
function PredictionRowItem({
  p,
  hit,
  nowMs,
  closeAtMap,
  startAtMap,
}: {
  p: PredictionSummary;
  hit: RaceHit | undefined;
  nowMs: number;
  closeAtMap?: Map<string, number>;
  startAtMap?: Map<string, number>;
}) {
  const closeAt = p.close_at ?? closeAtMap?.get(p.race_id) ?? null;
  const startAt = p.start_at ?? startAtMap?.get(p.race_id) ?? null;
  const timing = raceTimingStatus(closeAt, startAt, p.has_result, nowMs);
  // 「的中」ラベルは **3連単的中モード (実弾投票束) のみ**で判定 (2026-06-06 特化)。
  // 3連単束が無い旧 snapshot は旧実弾だった EV束 (bundle_hit) に fallback して表示を保つ。
  // **skipped は anyHit より優先**: 賭けていない race は理論値が立っていても「的中」ではない。
  const useTrifecta = !!(hit && hit.trifecta_bundle_participated);
  const bundleSkipped = !!(hit && !useTrifecta && hit.bundle_participated === false);
  const anyHit = !bundleSkipped && !!(hit && (useTrifecta ? hit.trifecta_bundle_hit : hit.bundle_hit));
  const rowBg = hit
    ? raceTimingRowBg(anyHit ? "good" : bundleSkipped ? "muted" : "bad")
    : raceTimingRowBg(timing.tone);

  return (
    <li
      className={`py-2.5 flex items-center gap-3 -mx-4 px-4 ${rowBg}`}
    >
      <Link
        href={`/predictions/${p.race_id}`}
        className="flex-1 group min-w-0"
      >
        <div className="flex items-center gap-2 text-sm flex-wrap">
          <span className="font-medium truncate">
            {p.venue_name} {p.race_number}R
          </span>
          <Badge tone="muted">{p.race_class}</Badge>
          {hit ? (
            anyHit ? (
              <Badge tone="good">的中</Badge>
            ) : bundleSkipped ? (
              <Badge tone="muted">見送り</Badge>
            ) : (
              <Badge tone="bad">不的中</Badge>
            )
          ) : (
            <Badge tone={timing.tone}>{timing.label}</Badge>
          )}
          {p.has_evidence ? (
            <Badge tone="magenta">補強済</Badge>
          ) : !p.has_result ? (
            <Badge tone="muted">評価待ち</Badge>
          ) : null}
        </div>
        {hit ? (
          <div className="text-xs tabnum mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <span>
              着順{" "}
              <span className="font-bold mono">{hit.finish.join("-")}</span>
            </span>
            <span className="text-(--color-muted)">·</span>
            {hit.trifecta_bundle_hit && <Badge tone="good">3連単束 的中</Badge>}
            {hit.bundle_hit && <Badge tone="muted">EV束(参考) 的中</Badge>}
            {hit.payout > 0 && (
              <>
                <span className="text-(--color-muted)">·</span>
                <span className="font-bold text-(--color-good)">
                  ¥{hit.payout.toLocaleString()}
                </span>
              </>
            )}
          </div>
        ) : (
          <div className="text-xs text-(--color-muted) mt-0.5 tabnum flex flex-wrap gap-x-1.5">
            <span>{fmtServerDateTime(p.saved_at)}</span>
            <span>·</span>
            <span>候補 {p.row_count}</span>
          </div>
        )}
      </Link>
      <Link
        href={`/predictions/${p.race_id}`}
        className="text-xs text-(--color-accent) hover:underline shrink-0"
      >
        詳細
      </Link>
    </li>
  );
}

export default async function DashboardPage() {
  const [preds, cal, watch, watchHist] = await Promise.all([
    api.listPredictions(200).catch(() => ({
      items: [] as PredictionSummary[],
    })),
    api.calibrate().catch(() => null),
    api.watchStatus().catch(() => null),
    api
      .watchHistory(500)
      .catch(() => ({ items: [] as WatchAutoHistoryItem[] })),
  ]);

  const trifectaBundle = cal?.trifecta_bundle;

  const raceHitMap = new Map<string, RaceHit>();
  for (const r of cal?.races ?? []) raceHitMap.set(r.race_id, r);
  const closeAtMap = new Map<string, number>();
  const startAtMap = new Map<string, number>();
  for (const h of watchHist.items) {
    if (h.race_id && h.close_at) closeAtMap.set(h.race_id, h.close_at);
    if (h.race_id && h.start_at != null) startAtMap.set(h.race_id, h.start_at);
  }

  const nowMs = Date.now();
  // 予測履歴セクションを削除したので related な集計は不要。
  // confidence / 集計対象 は 3連単的中モードの計測窓 (trifecta_cutoff 以降) 基準。
  const confidence = trifectaBundle
    ? calibrationConfidence(trifectaBundle.races)
    : null;
  const lastUpdated = cal ? fmtRelativeFromNow(cal.last_updated_at, nowMs) : "—";
  // "2026-06-05T00:00:00" → "2026-06-05" (注記表示用)
  const trifectaCutoffDate = cal?.trifecta_cutoff?.slice(0, 10);

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        title="ダッシュボード"
        subtitle="競馬オーケストレーション AI の実弾運用と AI 比較の俯瞰。15 秒おきに自動更新。"
      />

      {/* watch-auto stat はダッシュボードから削除 (2026-05-29 ユーザ指示)。
          状態は header の WatchPill で確認可能。 */}

      {/* 一番上の段: 参加レース数 / 見送りレース数 / 的中レース数 / 収支
          (全て 3連単的中モード = 実弾投票束 基準, 2026-06-06〜固定) */}
      <div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat
            label="参加レース数"
            value={trifectaBundle?.participated_races ?? 0}
            hint={
              trifectaBundle
                ? `総合 ${trifectaBundle.races} / 見送り ${trifectaBundle.skipped_races}`
                : "—"
            }
            accentTone="muted"
          />
          <Stat
            label="見送りレース数"
            value={trifectaBundle?.skipped_races ?? 0}
            hint={
              trifectaBundle && trifectaBundle.races > 0
                ? `見送り率 ${Math.round((trifectaBundle.skipped_races / trifectaBundle.races) * 100)}%`
                : "—"
            }
            accentTone="muted"
          />
          <Stat
            label="的中レース数"
            value={trifectaBundle?.hits ?? 0}
            hint={
              trifectaBundle
                ? `参加 ${trifectaBundle.participated_races} / 集計 ${trifectaBundle.races}`
                : "—"
            }
            // 値は黒文字 (default)。左 border のみ緑で AI 種別を示す (2026-05-29 ユーザ指示)
            tone="default"
            accentTone="good"
          />
          <Stat
            label="収支"
            // 収支は **最終オッズ基準** (実払戻に近い)。最終オッズが無い旧 result は
            // backend が予想オッズに fallback 済 (payout_final = payout) (2026-05-30 ユーザ指示)。
            value={
              !trifectaBundle
                ? "—"
                : (() => {
                    const pay = trifectaBundle.payout_final ?? trifectaBundle.payout;
                    const pl = pay - trifectaBundle.stake;
                    return `${pl >= 0 ? "+" : ""}${fmtYen(pl)}`;
                  })()
            }
            hint={
              trifectaBundle
                ? `賭金 ${fmtYen(trifectaBundle.stake)} → 払戻(最終) ${fmtYen(trifectaBundle.payout_final ?? trifectaBundle.payout)}`
                : "—"
            }
            tone={
              // 収支: マイナスなら赤、プラスは黒 (default) (2026-05-29 ユーザ指示)
              !trifectaBundle
                ? "default"
                : (trifectaBundle.payout_final ?? trifectaBundle.payout) - trifectaBundle.stake < 0
                ? "bad"
                : "default"
            }
            accentTone={
              !trifectaBundle ||
              (trifectaBundle.payout_final ?? trifectaBundle.payout) - trifectaBundle.stake >= 0
                ? "magenta"
                : "bad"
            }
          />
        </div>
        <div className="text-[10px] text-(--color-muted) text-right mt-1 px-1">
          ※ 全て 3連単的中モード (実弾投票束) 基準 ／ 集計対象 {trifectaBundle?.races ?? 0} レース
          {trifectaCutoffDate ? ` (${trifectaCutoffDate}〜)` : ""} ／
          {confidence && (
            <span className="ml-1">
              <Badge tone={confidence.tone}>{confidence.label}</Badge>
            </span>
          )}
          <span className="ml-1">最終更新 {lastUpdated}</span>
        </div>
      </div>

      {/* 3連単的中モード セクション — **実弾投票束 (2026-06-06〜固定)**。
          市場無視・Claude 指数フォーメーション。的中率系=フクシア。 */}
      <section className="space-y-2">
        <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
          <span className="inline-block w-1 h-4 bg-fuchsia-500 translate-y-0.5" />
          <span className="text-base">3連単的中モード</span>
          <span className="text-xs font-normal text-(--color-muted)">
            市場無視・Claude 指数フォーメーション / 実弾投票束 (固定)
          </span>
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat
            label="的中率"
            value={
              !trifectaBundle || trifectaBundle.participated_races === 0
                ? "—"
                : fmtPct(trifectaBundle.hit_rate, 1)
            }
            hint={
              trifectaBundle && trifectaBundle.participated_races > 0
                ? `${trifectaBundle.hits} 的中 / ${trifectaBundle.participated_races} 参加`
                : "賭けたレースなし"
            }
            tone="default"
            accentTone="magenta"
          />
          <Stat
            label="回収率"
            value={
              !trifectaBundle || trifectaBundle.participated_races === 0
                ? "—"
                : fmtRoiPct(trifectaBundle.roi_final ?? trifectaBundle.roi)
            }
            hint={
              trifectaBundle && trifectaBundle.participated_races > 0
                ? `賭金 ${fmtYen(trifectaBundle.stake)} → 払戻(最終) ${fmtYen(trifectaBundle.payout_final ?? trifectaBundle.payout)}`
                : "—"
            }
            tone={
              !trifectaBundle || trifectaBundle.participated_races === 0
                ? "default"
                : (trifectaBundle.roi_final ?? trifectaBundle.roi) > 1
                ? "default"
                : "bad"
            }
            accentTone="magenta"
          />
          <Stat
            label="収支"
            value={
              !trifectaBundle || trifectaBundle.participated_races === 0
                ? "—"
                : (() => {
                    const pay = trifectaBundle.payout_final ?? trifectaBundle.payout;
                    const pl = pay - trifectaBundle.stake;
                    return `${pl >= 0 ? "+" : ""}${fmtYen(pl)}`;
                  })()
            }
            hint={
              trifectaBundle
                ? `参加 ${trifectaBundle.participated_races} / 集計 ${trifectaBundle.races}`
                : "—"
            }
            tone={
              !trifectaBundle || trifectaBundle.participated_races === 0
                ? "default"
                : (trifectaBundle.payout_final ?? trifectaBundle.payout) - trifectaBundle.stake < 0
                ? "bad"
                : "default"
            }
            accentTone="magenta"
          />
          <Stat
            label="見送りレース数"
            value={trifectaBundle?.skipped_races ?? 0}
            hint={
              trifectaBundle && trifectaBundle.races > 0
                ? `見送り率 ${Math.round((trifectaBundle.skipped_races / trifectaBundle.races) * 100)}%`
                : "—"
            }
            accentTone="muted"
          />
        </div>
        <div className="text-[10px] text-(--color-muted) text-right px-1">
          ※ 実弾投票束 (3連単的中モード固定)。計測対象は{" "}
          {trifectaCutoffDate ?? "計測開始日"}〜 のレースのみ。Claude 指数なしのレースは自動見送り
        </div>
      </section>

      {/* EV束 (モデル参考) セクションはダッシュボードから削除 (2026-06-06 ユーザ指示)。
          EV束の集計は /calibrate (確率較正) と履歴詳細ページで引き続き参照可能。 */}

      {/* チャート: 累積収支 + 結果分布 (簡易 SVG 描画) — 3連単的中 (実弾) のみ */}
      {cal && cal.races.length > 0 && (
        <DashboardCharts races={cal.races} />
      )}
    </Page>
  );
}
