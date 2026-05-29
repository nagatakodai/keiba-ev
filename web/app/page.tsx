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

// 累積収支推移 (回収優先 + 的中優先) + 結果分布の簡易 SVG チャート群。recharts 等の
// 重い依存を増やさず Tailwind + inline SVG で軽量描画する。
function DashboardCharts({ races }: { races: RaceHit[] }) {
  // saved_at 昇順 (古い順) で並べて累積 stake / payout を計算
  const sorted = [...races].sort((a, b) =>
    (a.saved_at ?? "").localeCompare(b.saved_at ?? ""),
  );

  // 累積収支 (回収優先): 参加レースのみ (見送りは stake/payout 0 で実質スキップ)。
  let yieldStakeAcc = 0;
  let yieldPayoutAcc = 0;
  let hitStakeAcc = 0;
  let hitPayoutAcc = 0;
  const series = sorted.map((r) => {
    if (r.bundle_participated) {
      yieldStakeAcc += r.bundle_stake ?? 0;
      yieldPayoutAcc += r.bundle_payout ?? 0;
    }
    if (r.bundle_hit_first_participated) {
      hitStakeAcc += r.bundle_hit_first_stake ?? 0;
      hitPayoutAcc += r.bundle_hit_first_payout ?? 0;
    }
    return {
      yieldNet: yieldPayoutAcc - yieldStakeAcc,
      yieldRoi: yieldStakeAcc > 0 ? yieldPayoutAcc / yieldStakeAcc : 0,
      hitNet: hitPayoutAcc - hitStakeAcc,
      hitRoi: hitStakeAcc > 0 ? hitPayoutAcc / hitStakeAcc : 0,
    };
  });
  const yieldNetSeries = series.map((s) => s.yieldNet);
  const hitNetSeries = series.map((s) => s.hitNet);
  const yieldRoiSeries = series.map((s) => s.yieldRoi);
  const hitRoiSeries = series.map((s) => s.hitRoi);

  // 結果分布: 的中 / 不的中 / 見送り レース数
  const yieldHits = races.filter((r) => r.bundle_hit).length;
  const yieldMisses = races.filter(
    (r) => r.bundle_participated && !r.bundle_hit,
  ).length;
  const yieldSkips = races.filter(
    (r) => r.bundle_participated === false,
  ).length;
  const hitHits = races.filter((r) => r.bundle_hit_first_hit).length;
  const hitMisses = races.filter(
    (r) => r.bundle_hit_first_participated && !r.bundle_hit_first_hit,
  ).length;
  const hitSkips = races.filter(
    (r) => r.bundle_hit_first_participated === false,
  ).length;
  const totalRaces = races.length;

  // bet type 別 hit 内訳 (回収優先)
  const betTypeHits: Record<string, number> = {};
  for (const r of races) {
    for (const bt of r.bundle_hit_bet_types ?? []) {
      betTypeHits[bt] = (betTypeHits[bt] ?? 0) + 1;
    }
  }
  const betLabel: Record<string, string> = {
    win: "単勝", place: "複勝", quinella: "馬連", wide: "ワイド",
    exacta: "馬単", trio: "3連複", trifecta: "3連単",
  };

  return (
    <section className="space-y-3">
      <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
        <span className="inline-block w-1 h-4 bg-(--color-highlight) translate-y-0.5" />
        <span className="text-base">チャート</span>
        <span className="text-xs font-normal text-(--color-muted)">
          累積収支 / 結果分布 / 的中 bet 種別
        </span>
      </h2>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Card title="累積収支 (¥, 回収優先 vs 的中優先)">
          <LineChart
            seriesA={yieldNetSeries}
            seriesB={hitNetSeries}
            labelA="回収優先"
            labelB="的中優先"
            colorA="#10b981"
            colorB="#0ea5e9"
            yFmt={(v) => `${v >= 0 ? "+" : ""}${(v / 1000).toFixed(1)}k`}
          />
        </Card>
        <Card title="累積回収率 推移 (%)">
          <LineChart
            seriesA={yieldRoiSeries.map((v) => v * 100)}
            seriesB={hitRoiSeries.map((v) => v * 100)}
            labelA="回収優先"
            labelB="的中優先"
            colorA="#10b981"
            colorB="#0ea5e9"
            yFmt={(v) => `${v.toFixed(0)}%`}
            referenceY={100}
          />
        </Card>
        <Card title={`結果分布 (n=${totalRaces})`}>
          <div className="space-y-3">
            <DistroBar
              label="回収優先AI"
              hits={yieldHits}
              misses={yieldMisses}
              skips={yieldSkips}
            />
            <DistroBar
              label="的中優先AI"
              hits={hitHits}
              misses={hitMisses}
              skips={hitSkips}
            />
          </div>
        </Card>
        <Card title="回収優先AI 的中 bet 種別">
          {Object.keys(betTypeHits).length === 0 ? (
            <p className="text-xs text-(--color-muted)">的中なし</p>
          ) : (
            <div className="space-y-1.5">
              {Object.entries(betTypeHits)
                .sort((a, b) => b[1] - a[1])
                .map(([bt, n]) => {
                  const pct = totalRaces > 0 ? (n / totalRaces) * 100 : 0;
                  return (
                    <div key={bt} className="flex items-center gap-2 text-xs">
                      <span className="w-12 shrink-0 text-(--color-muted)">
                        {betLabel[bt] ?? bt}
                      </span>
                      <div className="flex-1 bg-(--color-panel-2) h-4 relative">
                        <div
                          className="absolute inset-y-0 left-0 bg-(--color-good)/60"
                          style={{ width: `${Math.min(100, pct * 4)}%` }}
                        />
                      </div>
                      <span className="font-bold tabnum w-12 text-right">
                        {n} 件
                      </span>
                    </div>
                  );
                })}
            </div>
          )}
        </Card>
      </div>
    </section>
  );
}

function LineChart({
  seriesA, seriesB, labelA, labelB, colorA, colorB, yFmt, referenceY,
}: {
  seriesA: number[];
  seriesB: number[];
  labelA: string;
  labelB: string;
  colorA: string;
  colorB: string;
  yFmt: (v: number) => string;
  referenceY?: number;
}) {
  const w = 600;
  const h = 180;
  const pad = { l: 48, r: 12, t: 8, b: 18 };
  const allValues = [...seriesA, ...seriesB, ...(referenceY !== undefined ? [referenceY] : [])];
  const minY = Math.min(0, ...allValues);
  const maxY = Math.max(0, ...allValues);
  const rangeY = maxY - minY || 1;
  const n = Math.max(seriesA.length, seriesB.length);
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
        {seriesB.length > 0 && (
          <path d={lineFor(seriesB)} fill="none" stroke={colorB} strokeWidth={2} strokeDasharray="3 2" />
        )}
        {/* legend */}
        <g transform={`translate(${pad.l + 4}, ${pad.t + 12})`}>
          <rect width={9} height={9} fill={colorA} />
          <text x={14} y={9} fontSize={10} fill="#374151">{labelA}</text>
          <rect width={9} height={9} fill={colorB} x={70} />
          <text x={84} y={9} fontSize={10} fill="#374151">{labelB}</text>
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
          的中 {hits} ／ 不的中 {misses} ／ 見送り {skips} (n={total})
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
  // Claude 総合オススメが「見送り」(束 legs 空) なら「不的中」ではなく「未参加」表示。
  // **bundleSkipped は anyHit より優先**: 賭けていない race は plan_X_hit (理論値) が
  // 立っていても「的中」ではない (見送り = 不参加)。
  const bundleSkipped = !!(hit && hit.bundle_participated === false);
  // 回収優先 bundle hit OR 的中優先 bundle hit のどちらかで的中扱い
  const anyHit =
    !bundleSkipped &&
    !!(hit && (hit.bundle_hit || hit.bundle_hit_first_hit));
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
            {hit.bundle_hit && <Badge tone="good">回収 Claude</Badge>}
            {hit.bundle_hit_first_hit && <Badge tone="info">的中 Claude</Badge>}
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

  const claudeBundle = cal?.claude_bundle;
  const claudeBundleHit = cal?.claude_bundle_hit;

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
  const confidence = cal ? calibrationConfidence(cal.race_count) : null;
  const lastUpdated = cal ? fmtRelativeFromNow(cal.last_updated_at, nowMs) : "—";

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        title="ダッシュボード"
        subtitle="競馬オーケストレーション AI の実弾運用と AI 比較の俯瞰。15 秒おきに自動更新。"
      />

      {/* watch-auto stat はダッシュボードから削除 (2026-05-29 ユーザ指示)。
          状態は header の WatchPill で確認可能。 */}

      {/* 回収優先AI セクション (実弾で買う) */}
      <section className="space-y-2">
        <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
          <span className="inline-block w-1 h-4 bg-(--color-good) translate-y-0.5" />
          <span className="text-base">回収優先AI</span>
          <span className="text-xs font-normal text-(--color-muted)">
            joint Kelly EV 最適 / 実弾で買う対象
          </span>
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <Stat
          label="集計対象レース"
          value={cal?.race_count ?? 0}
          hint={
            confidence ? (
              <span className="flex items-center gap-1">
                <Badge tone={confidence.tone}>{confidence.label}</Badge>
                <span>· 最終更新 {lastUpdated}</span>
              </span>
            ) : (
              "—"
            )
          }
          tone={confidence?.tone === "good" ? "good" : confidence?.tone === "warn" ? "warn" : "bad"}
        />
        <Stat
          label="回収優先AI 回収率"
          value={
            !claudeBundle || claudeBundle.participated_races === 0
              ? "—"
              : fmtRoiPct(claudeBundle.roi)
          }
          hint={
            claudeBundle && claudeBundle.participated_races > 0
              ? `${claudeBundle.participated_races} 参加 / ${claudeBundle.skipped_races} 見送り · 賭金 ${fmtYen(claudeBundle.stake)} → 払戻 ${fmtYen(claudeBundle.payout)}`
              : "賭けたレースなし"
          }
          tone={
            !claudeBundle || claudeBundle.participated_races < 30
              ? "warn"
              : claudeBundle.roi >= 1
              ? "good"
              : claudeBundle.roi >= 0.85
              ? "warn"
              : "bad"
          }
        />
        <Stat
          label="回収優先AI 的中率"
          value={
            !claudeBundle || claudeBundle.participated_races === 0
              ? "—"
              : fmtPct(claudeBundle.hit_rate, 1)
          }
          hint={
            claudeBundle && claudeBundle.participated_races > 0
              ? `${claudeBundle.hits} 的中 / ${claudeBundle.participated_races} 参加 (見送り除く)`
              : ""
          }
          tone={
            !claudeBundle || claudeBundle.participated_races < 30
              ? "warn"
              : claudeBundle.hit_rate >= 0.3
              ? "good"
              : claudeBundle.hit_rate >= 0.15
              ? "warn"
              : "bad"
          }
        />
        </div>
      </section>

      {/* 的中優先AI セクション (おまけ計測 / 買わない) */}
      <section className="space-y-2">
        <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
          <span className="inline-block w-1 h-4 bg-(--color-info) translate-y-0.5" />
          <span className="text-base">的中優先AI</span>
          <span className="text-xs font-normal text-(--color-muted)">
            prob 降順 pool / おまけ計測・買わない
          </span>
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <Stat
          label="集計対象レース"
          value={cal?.race_count ?? 0}
          hint={
            confidence ? (
              <span className="flex items-center gap-1">
                <Badge tone={confidence.tone}>{confidence.label}</Badge>
              </span>
            ) : (
              "—"
            )
          }
          tone={confidence?.tone === "good" ? "good" : confidence?.tone === "warn" ? "warn" : "bad"}
        />
        <Stat
          label="的中優先AI 回収率"
          value={
            !claudeBundleHit || claudeBundleHit.participated_races === 0
              ? "—"
              : fmtRoiPct(claudeBundleHit.roi)
          }
          hint={
            claudeBundleHit && claudeBundleHit.participated_races > 0
              ? `${claudeBundleHit.participated_races} 参加 / 賭金 ${fmtYen(claudeBundleHit.stake)} → 払戻 ${fmtYen(claudeBundleHit.payout)}`
              : "新スキーマ待ち (本日以降の analyze から蓄積)"
          }
          tone={
            !claudeBundleHit || claudeBundleHit.participated_races < 30
              ? "warn"
              : claudeBundleHit.roi >= 1
              ? "good"
              : claudeBundleHit.roi >= 0.85
              ? "warn"
              : "bad"
          }
        />
        <Stat
          label="的中優先AI 的中率"
          value={
            !claudeBundleHit || claudeBundleHit.participated_races === 0
              ? "—"
              : fmtPct(claudeBundleHit.hit_rate, 1)
          }
          hint={
            claudeBundleHit && claudeBundleHit.participated_races > 0
              ? `${claudeBundleHit.hits} 的中 / ${claudeBundleHit.participated_races} 参加`
              : "新スキーマ待ち (本日以降の analyze から蓄積)"
          }
          tone={
            !claudeBundleHit || claudeBundleHit.participated_races < 30
              ? "warn"
              : claudeBundleHit.hit_rate >= 0.3
              ? "good"
              : claudeBundleHit.hit_rate >= 0.15
              ? "warn"
              : "bad"
          }
        />
        </div>
      </section>

      {/* チャート: 累積収支 + 結果分布 (簡易 SVG 描画) */}
      {cal && cal.races.length > 0 && (
        <DashboardCharts races={cal.races} />
      )}
    </Page>
  );
}
