import {
  api,
  type CalibrationReport,
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
  fmtYen,
} from "@/components/ui";
import { AutoRefresh } from "@/components/AutoRefresh";

export const dynamic = "force-dynamic";

type RaceHit = CalibrationReport["races"][number];

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}

// 累積収支推移 (EV束 = 実弾既定束 + 3連単束) + 結果分布の簡易 SVG チャート群。
// recharts 等の重い依存を増やさず Tailwind + inline SVG で軽量描画する。
// EV束系列は 2026-06-10 (修正版 EV束 = 実弾既定束) に復活 — ev_measured のみ累積。
function DashboardCharts({ races }: { races: RaceHit[] }) {
  // どちらかの系列の計測対象になっている race のみ (両 cutoff の和集合)。
  // field 欠落 (旧 API) は従来通り含める。
  const measured = races.filter(
    (r) => r.trifecta_measured !== false || r.ev_measured === true,
  );
  // saved_at 昇順 (古い順) で並べて累積 stake / payout を計算
  const sorted = [...measured].sort((a, b) =>
    (a.saved_at ?? "").localeCompare(b.saved_at ?? ""),
  );

  // 累積収支: 参加レースのみ (見送りは stake/payout 0 で実質スキップ)。
  // A = EV束 (実弾既定, ev_measured のみ) / B = 3連単束 (trifecta_measured のみ)。
  let evStakeAcc = 0;
  let evPayoutAcc = 0;
  let tStakeAcc = 0;
  let tPayoutAcc = 0;
  const series = sorted.map((r) => {
    if (r.ev_measured && r.bundle_participated) {
      evStakeAcc += r.bundle_stake ?? 0;
      // 最終オッズ基準 (実払戻に近い)。最終が無い旧 result は予想 payout に fallback。
      evPayoutAcc += r.bundle_payout_final ?? r.bundle_payout ?? 0;
    }
    if (r.trifecta_measured !== false && r.trifecta_bundle_participated) {
      tStakeAcc += r.trifecta_bundle_stake ?? 0;
      tPayoutAcc += r.trifecta_bundle_payout_final ?? r.trifecta_bundle_payout ?? 0;
    }
    return {
      evNet: evPayoutAcc - evStakeAcc,
      evRoi: evStakeAcc > 0 ? evPayoutAcc / evStakeAcc : 0,
      tNet: tPayoutAcc - tStakeAcc,
      tRoi: tStakeAcc > 0 ? tPayoutAcc / tStakeAcc : 0,
    };
  });
  const evNetSeries = series.map((s) => s.evNet);
  const evRoiSeries = series.map((s) => s.evRoi);
  const tNetSeries = series.map((s) => s.tNet);
  const tRoiSeries = series.map((s) => s.tRoi);

  // 結果分布: 的中 / 不的中 / 見送り レース数 (各系列の計測対象のみ)
  const evMeasured = measured.filter((r) => r.ev_measured);
  const evHits = evMeasured.filter((r) => r.bundle_hit).length;
  const evMisses = evMeasured.filter(
    (r) => r.bundle_participated && !r.bundle_hit,
  ).length;
  const evSkips = evMeasured.filter((r) => r.bundle_participated === false).length;
  const tMeasured = measured.filter((r) => r.trifecta_measured !== false);
  const tHits = tMeasured.filter((r) => r.trifecta_bundle_hit).length;
  const tMisses = tMeasured.filter(
    (r) => r.trifecta_bundle_participated && !r.trifecta_bundle_hit,
  ).length;
  const tSkips = tMeasured.filter((r) => r.trifecta_bundle_participated === false).length;

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
            seriesA={evNetSeries}
            labelA="EV束 (実弾既定)"
            colorA="#0ea5e9"
            seriesB={tNetSeries}
            labelB="3連単束"
            colorB="#d946ef"
            yFmt={(v) => `${v >= 0 ? "+" : ""}${(v / 1000).toFixed(1)}k`}
          />
        </Card>
        <Card title="累積回収率 推移">
          <LineChart
            seriesA={evRoiSeries.map((v) => v * 100)}
            labelA="EV束 (実弾既定)"
            colorA="#0ea5e9"
            seriesB={tRoiSeries.map((v) => v * 100)}
            labelB="3連単束"
            colorB="#d946ef"
            yFmt={(v) => `${v.toFixed(0)}%`}
            referenceY={100}
          />
        </Card>
        <Card title="結果分布">
          <div className="space-y-3">
            <DistroBar
              label="EV束 (実弾既定, 2026-06-10〜)"
              hits={evHits}
              misses={evMisses}
              skips={evSkips}
            />
            <DistroBar
              label="3連単束"
              hits={tHits}
              misses={tMisses}
              skips={tSkips}
            />
          </div>
        </Card>
      </div>
    </section>
  );
}

function LineChart({
  seriesA, seriesB, labelA, labelB, colorA, colorB, yFmt, referenceY,
}: {
  seriesA: number[];
  // seriesB は任意 (省略時は系列 A のみ描画)。A=EV束 実弾既定 / B=3連単束 が既定の使い方。
  seriesB?: number[];
  labelA: string;
  labelB?: string;
  colorA: string;
  colorB?: string;
  yFmt: (v: number) => string;
  referenceY?: number;
}) {
  const w = 600;
  const h = 180;
  const pad = { l: 48, r: 12, t: 8, b: 18 };
  const allValues = [
    ...seriesA,
    ...(seriesB ?? []),
    ...(referenceY !== undefined ? [referenceY] : []),
  ];
  const minY = Math.min(0, ...allValues);
  const maxY = Math.max(0, ...allValues);
  const rangeY = maxY - minY || 1;
  const n = Math.max(seriesA.length, seriesB?.length ?? 0);
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
        {seriesB && seriesB.length > 0 && colorB && (
          <path d={lineFor(seriesB)} fill="none" stroke={colorB} strokeWidth={2}
                strokeOpacity={0.85} />
        )}
        {seriesA.length > 0 && (
          <path d={lineFor(seriesA)} fill="none" stroke={colorA} strokeWidth={2} />
        )}
        {/* legend */}
        <g transform={`translate(${pad.l + 4}, ${pad.t + 12})`}>
          <rect width={9} height={9} fill={colorA} />
          <text x={14} y={9} fontSize={10} fill="#374151">{labelA}</text>
          {labelB && colorB && (
            <>
              <rect x={110} width={9} height={9} fill={colorB} />
              <text x={124} y={9} fontSize={10} fill="#374151">{labelB}</text>
            </>
          )}
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

// (旧 PredictionRowItem は dead code だったため削除 2026-06-10 — per-race 行の描画は
// /predictions の PredictionsList と /calibrate に存在し、そちらが ev_measured 対応済み)

export default async function DashboardPage() {
  const [cal, watch] = await Promise.all([
    api.calibrate().catch(() => null),
    api.watchStatus().catch(() => null),
  ]);

  const trifectaBundle = cal?.trifecta_bundle;
  const evBundle = cal?.ev_bundle;
  // 実弾既定束: watch-auto の現在設定 (bet_bundle) に追従。bet_bundle キーが無い旧 config で
  // 稼働中なら旧挙動 = 3連単束、それ以外 (未稼働/新 config) は新既定の EV束。
  const activeBundleKind: "ev" | "trifecta" =
    watch?.config?.bet_bundle ?? (watch?.running ? "trifecta" : "ev");
  const activeBundle = activeBundleKind === "ev" ? evBundle : trifectaBundle;
  const activeBundleLabel = activeBundleKind === "ev" ? "EV束" : "3連単束";

  const nowMs = Date.now();
  // 予測履歴セクションを削除したので related な集計は不要。
  // confidence / 集計対象 は実弾既定束の計測窓 (ev_cutoff / trifecta_cutoff 以降) 基準。
  const confidence = activeBundle
    ? calibrationConfidence(activeBundle.races)
    : null;
  const lastUpdated = cal ? fmtRelativeFromNow(cal.last_updated_at, nowMs) : "—";
  // "2026-06-05T00:00:00" → "2026-06-05" (注記表示用)
  const trifectaCutoffDate = cal?.trifecta_cutoff?.slice(0, 10);
  const evCutoffDate = cal?.ev_cutoff?.slice(0, 10);
  const activeCutoffDate = activeBundleKind === "ev" ? evCutoffDate : trifectaCutoffDate;

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
          (実弾投票束 = watch-auto の bet_bundle 設定に追従。既定 EV束 2026-06-10〜) */}
      <div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat
            label="参加レース数"
            value={activeBundle?.participated_races ?? 0}
            hint={
              activeBundle
                ? `総合 ${activeBundle.races} / 見送り ${activeBundle.skipped_races}`
                : "—"
            }
            accentTone="muted"
          />
          <Stat
            label="見送りレース数"
            value={activeBundle?.skipped_races ?? 0}
            hint={
              activeBundle && activeBundle.races > 0
                ? `見送り率 ${Math.round((activeBundle.skipped_races / activeBundle.races) * 100)}%`
                : "—"
            }
            accentTone="muted"
          />
          <Stat
            label="的中レース数"
            value={activeBundle?.hits ?? 0}
            hint={
              activeBundle
                ? `参加 ${activeBundle.participated_races} / 集計 ${activeBundle.races}`
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
              !activeBundle
                ? "—"
                : (() => {
                    const pay = activeBundle.payout_final ?? activeBundle.payout;
                    const pl = pay - activeBundle.stake;
                    return `${pl >= 0 ? "+" : ""}${fmtYen(pl)}`;
                  })()
            }
            hint={
              activeBundle
                ? `賭金 ${fmtYen(activeBundle.stake)} → 払戻(最終) ${fmtYen(activeBundle.payout_final ?? activeBundle.payout)}`
                : "—"
            }
            tone={
              // 収支: マイナスなら赤、プラスは黒 (default) (2026-05-29 ユーザ指示)
              !activeBundle
                ? "default"
                : (activeBundle.payout_final ?? activeBundle.payout) - activeBundle.stake < 0
                ? "bad"
                : "default"
            }
            accentTone={
              !activeBundle ||
              (activeBundle.payout_final ?? activeBundle.payout) - activeBundle.stake >= 0
                ? "magenta"
                : "bad"
            }
          />
        </div>
        <div className="text-[10px] text-(--color-muted) text-right mt-1 px-1">
          ※ 全て {activeBundleLabel} (実弾投票束) 基準 ／ 集計対象 {activeBundle?.races ?? 0} レース
          {activeCutoffDate ? ` (${activeCutoffDate}〜)` : ""} ／
          {confidence && (
            <span className="ml-1">
              <Badge tone={confidence.tone}>{confidence.label}</Badge>
            </span>
          )}
          <span className="ml-1">最終更新 {lastUpdated}</span>
        </div>
      </div>

      {/* EV束 セクション — **実弾既定束 (2026-06-10〜, KEIBA_BET_BUNDLE=ev)**。
          全脚がシェード込み P×O≥1.02 + px_o≤2.0 + ½Kelly + トリガミ防止を通過した時のみ
          legs が立つ = 大半のレースは見送り (正常)。系列色=スカイブルー。 */}
      <section className="space-y-2">
        <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
          <span className="inline-block w-1 h-4 bg-sky-500 translate-y-0.5" />
          <span className="text-base">EV束</span>
          <span className="text-xs font-normal text-(--color-muted)">
            joint ½Kelly・シェード込み P×O ゲート / 実弾既定束 ({evCutoffDate ?? "2026-06-10"}〜)
          </span>
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat
            label="的中率"
            value={
              !evBundle || evBundle.participated_races === 0
                ? "—"
                : fmtPct(evBundle.hit_rate, 1)
            }
            hint={
              evBundle && evBundle.participated_races > 0
                ? `${evBundle.hits} 的中 / ${evBundle.participated_races} 参加`
                : "賭けたレースなし"
            }
            tone="default"
            accentTone="info"
          />
          <Stat
            label="回収率"
            value={
              !evBundle || evBundle.participated_races === 0
                ? "—"
                : fmtRoiPct(evBundle.roi_final ?? evBundle.roi)
            }
            hint={
              evBundle && evBundle.participated_races > 0
                ? `賭金 ${fmtYen(evBundle.stake)} → 払戻(最終) ${fmtYen(evBundle.payout_final ?? evBundle.payout)}`
                : "—"
            }
            tone={
              !evBundle || evBundle.participated_races === 0
                ? "default"
                : (evBundle.roi_final ?? evBundle.roi) > 1
                ? "default"
                : "bad"
            }
            accentTone="info"
          />
          <Stat
            label="収支"
            value={
              !evBundle || evBundle.participated_races === 0
                ? "—"
                : (() => {
                    const pay = evBundle.payout_final ?? evBundle.payout;
                    const pl = pay - evBundle.stake;
                    return `${pl >= 0 ? "+" : ""}${fmtYen(pl)}`;
                  })()
            }
            hint={
              evBundle
                ? `参加 ${evBundle.participated_races} / 集計 ${evBundle.races}`
                : "—"
            }
            tone={
              !evBundle || evBundle.participated_races === 0
                ? "default"
                : (evBundle.payout_final ?? evBundle.payout) - evBundle.stake < 0
                ? "bad"
                : "default"
            }
            accentTone="info"
          />
          <Stat
            label="見送りレース数"
            value={evBundle?.skipped_races ?? 0}
            hint={
              evBundle && evBundle.races > 0
                ? `見送り率 ${Math.round((evBundle.skipped_races / evBundle.races) * 100)}%`
                : "—"
            }
            accentTone="muted"
          />
        </div>
        <div className="text-[10px] text-(--color-muted) text-right px-1">
          ※ EV束 (実弾既定束)。計測対象は {evCutoffDate ?? "2026-06-10"}〜 のレースのみ
          (それ以前の旧 EV束は β=0 事故込みの別物のため除外)。見送り多数は正常動作
        </div>
      </section>

      {/* 3連単的中モード セクション — **実弾投票束 (2026-06-06〜固定)**。
          市場無視・Claude 指数フォーメーション。的中率系=フクシア。 */}
      <section className="space-y-2">
        <h2 className="flex items-baseline gap-2 text-sm font-bold tracking-tight px-1">
          <span className="inline-block w-1 h-4 bg-fuchsia-500 translate-y-0.5" />
          <span className="text-base">3連単束</span>
          <span className="text-xs font-normal text-(--color-muted)">
            市場無視・Claude 指数フォーメーション / KEIBA_BET_BUNDLE=trifecta 選択時の実弾束
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
          ※ 計測対象は {trifectaCutoffDate ?? "計測開始日"}〜 のレースのみ。
          Claude 指数なしのレースは自動見送り。2026-06-10〜の実弾既定は EV束 (上段)
        </div>
      </section>

      {/* チャート: 累積収支 + 結果分布 (簡易 SVG 描画) — EV束 (実弾既定) + 3連単束 */}
      {cal && cal.races.length > 0 && (
        <DashboardCharts races={cal.races} />
      )}
    </Page>
  );
}
