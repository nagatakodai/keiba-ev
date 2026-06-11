import Link from "next/link";
import type { ReactNode } from "react";
import {
  Activity,
  ChartSpline,
  History,
  ServerOff,
  Sparkles,
  Target,
} from "lucide-react";
import {
  api,
  type CalibrationReport,
  type ClaudeBundleAggregate,
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
import {
  BundleRoiBarsChart,
  NetTrendChart,
  RoiTrendChart,
  type BundleRoiBarDatum,
  type BundleTrendPoint,
} from "@/components/dashboard/DashboardCharts";

export const dynamic = "force-dynamic";

type RaceHit = CalibrationReport["races"][number];

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}

function fmtSignedYen(n: number): string {
  return `${n < 0 ? "-" : "+"}¥${Math.abs(n).toLocaleString()}`;
}

// ============================================================
// 累積系列の計算 (server 側)。チャート描画は client ラッパに委譲。
// EV束系列は 2026-06-10 (修正版 EV束 = 実弾既定束) に復活 — ev_measured のみ累積。
// ============================================================

type DistroCounts = { hits: number; misses: number; skips: number };

function buildDashboardData(races: RaceHit[]): {
  points: BundleTrendPoint[];
  evDistro: DistroCounts;
  tDistro: DistroCounts;
} {
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
  // ev* = EV束 (実弾既定, ev_measured のみ) / t* = 3連単束 (trifecta_measured のみ)。
  let evStakeAcc = 0;
  let evPayoutAcc = 0;
  let tStakeAcc = 0;
  let tPayoutAcc = 0;
  const points: BundleTrendPoint[] = sorted.map((r, i) => {
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
      x: r.saved_at ? r.saved_at.slice(5, 10) : `#${i + 1}`,
      evNet: evPayoutAcc - evStakeAcc,
      tNet: tPayoutAcc - tStakeAcc,
      evRoi: evStakeAcc > 0 ? (evPayoutAcc / evStakeAcc) * 100 : 0,
      tRoi: tStakeAcc > 0 ? (tPayoutAcc / tStakeAcc) * 100 : 0,
    };
  });

  // 結果分布: 的中 / 不的中 / 見送り レース数 (各系列の計測対象のみ)
  const evMeasured = measured.filter((r) => r.ev_measured);
  const evDistro: DistroCounts = {
    hits: evMeasured.filter((r) => r.bundle_hit).length,
    misses: evMeasured.filter((r) => r.bundle_participated && !r.bundle_hit).length,
    skips: evMeasured.filter((r) => r.bundle_participated === false).length,
  };
  const tMeasured = measured.filter((r) => r.trifecta_measured !== false);
  const tDistro: DistroCounts = {
    hits: tMeasured.filter((r) => r.trifecta_bundle_hit).length,
    misses: tMeasured.filter(
      (r) => r.trifecta_bundle_participated && !r.trifecta_bundle_hit,
    ).length,
    skips: tMeasured.filter((r) => r.trifecta_bundle_participated === false).length,
  };

  return { points, evDistro, tDistro };
}

// ============================================================
// 表示部品 (server-rendered)
// ============================================================

// セクション見出し: 小さい uppercase eyebrow + フェードする罫線。
// クラスはフル文字列で渡す (Tailwind JIT がソース走査で拾えるように)。
function SectionHeading({
  icon,
  eyebrow,
  title,
  desc,
  eyebrowClass = "text-(--color-muted)",
  lineClass = "from-(--color-line)",
}: {
  icon?: ReactNode;
  eyebrow: string;
  title: string;
  desc?: string;
  eyebrowClass?: string;
  lineClass?: string;
}) {
  return (
    <div className="px-1 space-y-1">
      <div className="flex items-center gap-3">
        <span
          className={`inline-flex items-center gap-1.5 text-[10px] font-bold tracking-[0.22em] uppercase ${eyebrowClass}`}
        >
          {icon}
          {eyebrow}
        </span>
        <span
          className={`h-px flex-1 bg-gradient-to-r ${lineClass} to-transparent`}
        />
      </div>
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-base font-bold tracking-tight">{title}</h2>
        {desc && <span className="text-xs text-(--color-muted)">{desc}</span>}
      </div>
    </div>
  );
}

// hero 内 KPI セル
function KpiCell({
  label,
  value,
  sub,
  valueClass = "",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  valueClass?: string;
}) {
  return (
    <div>
      <div className="text-[10px] font-bold tracking-widest uppercase text-(--color-muted)">
        {label}
      </div>
      <div className={`text-2xl font-black tnum tracking-tight mt-1 ${valueClass}`}>
        {value}
      </div>
      {sub && <div className="text-[11px] text-(--color-muted) tnum mt-0.5">{sub}</div>}
    </div>
  );
}

// hero 用の装飾スパークライン (実弾束の累積収支)。
// 軸なしの純装飾なので server-render の極小 SVG で済ませる
// (本チャートは下の recharts TrendChart が担う)。
function Sparkline({ values, positive }: { values: number[]; positive: boolean }) {
  if (values.length < 2) return null;
  const w = 240;
  const h = 44;
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const range = max - min || 1;
  const yAt = (v: number) => h - 3 - ((v - min) / range) * (h - 6);
  const pts = values
    .map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${yAt(v).toFixed(1)}`)
    .join(" ");
  const stroke = positive ? "var(--good)" : "var(--bad)";
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="w-full max-w-[260px] h-10 mt-2"
      preserveAspectRatio="none"
      aria-hidden
    >
      <line
        x1={0}
        x2={w}
        y1={yAt(0)}
        y2={yAt(0)}
        stroke="rgba(148, 163, 184, 0.3)"
        strokeWidth={1}
        strokeDasharray="3 3"
      />
      <polyline
        points={pts}
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
        strokeOpacity={0.9}
      />
    </svg>
  );
}

// 結果分布の積み上げバー (的中 / 不的中 / 見送り)
function DistroBar({
  label,
  hits,
  misses,
  skips,
}: {
  label: string;
  hits: number;
  misses: number;
  skips: number;
}) {
  const total = hits + misses + skips;
  if (total === 0) return null;
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-xs mb-1.5">
        <span className="font-bold">{label}</span>
        <span className="text-(--color-muted) tnum">
          的中 {hits} ／ 不的中 {misses} ／ 見送り {skips}
        </span>
      </div>
      <div className="flex h-2.5 rounded-full overflow-hidden bg-(--color-surface-2) border border-(--color-line-soft)">
        <div
          className="bg-emerald-500/80"
          style={{ width: `${(hits / total) * 100}%` }}
          title={`的中 ${hits}`}
        />
        <div
          className="bg-rose-500/70"
          style={{ width: `${(misses / total) * 100}%` }}
          title={`不的中 ${misses}`}
        />
        <div
          className="bg-slate-500/35"
          style={{ width: `${(skips / total) * 100}%` }}
          title={`見送り ${skips}`}
        />
      </div>
    </div>
  );
}

// 直近レース表の系列別 結果セル
function SeriesResult({
  measured,
  participated,
  hit,
  net,
  backfilled,
}: {
  measured: boolean;
  participated: boolean;
  hit: boolean;
  net: number | null;
  backfilled?: boolean;
}) {
  if (!measured) return <span className="text-(--color-muted)/60">対象外</span>;
  if (!participated) return <Badge tone="muted">見送り</Badge>;
  return (
    <span className="inline-flex items-center gap-1.5 flex-wrap">
      <Badge tone={hit ? "good" : "bad"}>{hit ? "的中" : "不的中"}</Badge>
      {net != null && (
        <span
          className={`tnum text-[11px] font-semibold ${
            net >= 0 ? "text-emerald-300" : "text-rose-300"
          }`}
        >
          {fmtSignedYen(net)}
        </span>
      )}
      {backfilled && <Badge tone="muted">backfill</Badge>}
    </span>
  );
}

// API 未接続時のエラーカード (silent null の代わりに明示表示)
function ApiDownCard() {
  return (
    <Card tone="alert">
      <div className="flex items-start gap-4">
        <div className="shrink-0 w-12 h-12 rounded-xl bg-amber-500/15 border border-amber-500/40 flex items-center justify-center">
          <ServerOff className="w-6 h-6 text-amber-300" />
        </div>
        <div className="space-y-1.5 min-w-0">
          <div className="text-sm font-bold">API 未接続 — 計測データを取得できません</div>
          <p className="text-xs text-(--color-muted) leading-relaxed">
            バックエンド API (port 9788) に到達できません。リポジトリ直下で
            <code className="mx-1 px-1.5 py-0.5 rounded bg-(--color-surface-2) border border-(--color-line) mono text-emerald-300">
              make api
            </code>
            を実行して API を起動してください。このページは 15 秒おきに自動で再接続を試みます。
          </p>
        </div>
      </div>
    </Card>
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
  // EV束の全期間参考集計 (β=0 事故時代込み)。実弾系列の判断には ev_bundle を使う。
  const claudeBundle = cal?.claude_bundle;
  // 実弾既定束: watch-auto の現在設定 (bet_bundle) に追従。bet_bundle キーが無い旧 config で
  // 稼働中なら旧挙動 = 3連単束、それ以外 (未稼働/新 config) は新既定の EV束。
  const activeBundleKind: "ev" | "trifecta" =
    watch?.config?.bet_bundle ?? (watch?.running ? "trifecta" : "ev");
  const activeBundle = activeBundleKind === "ev" ? evBundle : trifectaBundle;
  const activeBundleLabel = activeBundleKind === "ev" ? "EV束" : "3連単束";

  // RSC はリクエスト毎に 1 回だけ描画されるので、リクエスト時刻の取得は安全。
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();
  // confidence / 集計対象 は実弾既定束の計測窓 (ev_cutoff / trifecta_cutoff 以降) 基準。
  const confidence = activeBundle
    ? calibrationConfidence(activeBundle.races)
    : null;
  const lastUpdated = cal ? fmtRelativeFromNow(cal.last_updated_at, nowMs) : "—";
  // "2026-06-05T00:00:00" → "2026-06-05" (注記表示用)
  const trifectaCutoffDate = cal?.trifecta_cutoff?.slice(0, 10);
  const evCutoffDate = cal?.ev_cutoff?.slice(0, 10);
  const activeCutoffDate = activeBundleKind === "ev" ? evCutoffDate : trifectaCutoffDate;

  // API 未接続: silent null ではなく明示のエラーカードを出す。
  if (!cal) {
    return (
      <Page>
        <AutoRefresh seconds={15} />
        <PageHeader
          eyebrow="Keiba EV Terminal"
          title="ダッシュボード"
          subtitle="競馬オーケストレーション AI の実弾運用と AI 比較の俯瞰。15 秒おきに自動更新。"
        />
        <ApiDownCard />
      </Page>
    );
  }

  // チャート / 直近レース用の累積系列 (server 側で計算して client チャートに渡す)
  const { points, evDistro, tDistro } = buildDashboardData(cal.races);
  const heroSpark =
    activeBundleKind === "ev"
      ? points.map((p) => p.evNet)
      : points.map((p) => p.tNet);

  // hero KPI (実弾投票束 = activeBundle 基準)
  const heroPay = activeBundle
    ? activeBundle.payout_final ?? activeBundle.payout
    : null;
  const heroPl = activeBundle && heroPay != null ? heroPay - activeBundle.stake : null;
  const heroPositive = (heroPl ?? 0) >= 0;
  const heroParticipated = (activeBundle?.participated_races ?? 0) > 0;
  const heroRoi = activeBundle ? activeBundle.roi_final ?? activeBundle.roi : null;

  // 系列別 回収率バー (最終オッズ基準)。参加 0 の系列は出さない。
  const roiBars: BundleRoiBarDatum[] = (
    [
      ["EV束 (実弾既定)", evBundle],
      ["3連単束", trifectaBundle],
      ["EV束 全期間参考", claudeBundle],
    ] as Array<[string, ClaudeBundleAggregate | undefined]>
  ).flatMap(([label, b]) =>
    b && b.participated_races > 0
      ? [{ label, value: (b.roi_final ?? b.roi) * 100 }]
      : [],
  );

  // 直近レース: 計測対象 (どちらかの系列) の和集合を saved_at 降順で 12 件
  const recentRaces = cal.races
    .filter((r) => r.trifecta_measured !== false || r.ev_measured === true)
    .sort((a, b) => (b.saved_at ?? "").localeCompare(a.saved_at ?? ""))
    .slice(0, 12);

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        eyebrow="Keiba EV Terminal"
        title="ダッシュボード"
        subtitle="競馬オーケストレーション AI の実弾運用と AI 比較の俯瞰。15 秒おきに自動更新。"
      />

      {/* watch-auto stat はダッシュボードから削除 (2026-05-29 ユーザ指示)。
          状態は header の WatchPill で確認可能。 */}

      {/* ====== HERO: 実弾投票束 (watch-auto の bet_bundle 設定に追従。既定 EV束 2026-06-10〜) ====== */}
      <section className="relative overflow-hidden rounded-2xl border border-(--color-line) bg-(--color-card) shadow-[0_2px_24px_rgba(0,0,0,0.45)]">
        {/* 収支の正負に応じた淡い発光 (emerald=利益 / rose=損失) + sky の奥行き */}
        <div
          aria-hidden
          className={`pointer-events-none absolute -top-28 -right-20 w-96 h-96 rounded-full blur-3xl ${
            heroPositive ? "bg-emerald-500/10" : "bg-rose-500/10"
          }`}
        />
        <div
          aria-hidden
          className="pointer-events-none absolute -bottom-36 -left-24 w-96 h-96 rounded-full blur-3xl bg-sky-500/5"
        />
        <div className="relative p-5 space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Activity className="w-4 h-4 text-(--color-accent)" />
            <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
              Live P/L — 実弾投票束
            </span>
            <Badge tone={activeBundleKind === "ev" ? "info" : "magenta"}>
              {activeBundleLabel}
            </Badge>
            {watch?.running && <Badge tone="good">watch-auto 稼働中</Badge>}
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-[1.6fr_1fr_1fr_1fr_1fr] gap-x-6 gap-y-5 items-start">
            {/* 収支は **最終オッズ基準** (実払戻に近い)。最終オッズが無い旧 result は
                backend が予想オッズに fallback 済 (payout_final = payout) (2026-05-30 ユーザ指示)。 */}
            <div className="col-span-2 lg:col-span-1">
              <div className="text-[10px] font-bold tracking-widest uppercase text-(--color-muted)">
                収支 (最終オッズ基準)
              </div>
              <div
                className={`text-4xl font-black tnum tracking-tight mt-1 ${
                  heroPl == null
                    ? ""
                    : heroPl < 0
                    ? "text-rose-300"
                    : "text-emerald-300"
                }`}
              >
                {heroPl == null ? "—" : fmtSignedYen(heroPl)}
              </div>
              <div className="text-[11px] text-(--color-muted) tnum mt-0.5">
                {activeBundle && heroPay != null
                  ? `賭金 ${fmtYen(activeBundle.stake)} → 払戻(最終) ${fmtYen(heroPay)}`
                  : "—"}
              </div>
              <Sparkline values={heroSpark} positive={heroPositive} />
            </div>
            <KpiCell
              label="回収率"
              value={heroParticipated && heroRoi != null ? fmtRoiPct(heroRoi) : "—"}
              valueClass={
                !heroParticipated || heroRoi == null
                  ? ""
                  : heroRoi >= 1
                  ? "text-emerald-300"
                  : "text-rose-300"
              }
              sub={
                activeBundle?.roi_final_ci_low != null &&
                activeBundle?.roi_final_ci_high != null
                  ? `95%CI ${Math.round(activeBundle.roi_final_ci_low * 100)}–${Math.round(
                      activeBundle.roi_final_ci_high * 100,
                    )}%`
                  : "—"
              }
            />
            <KpiCell
              label="的中率"
              value={
                activeBundle && heroParticipated
                  ? fmtPct(activeBundle.hit_rate, 1)
                  : "—"
              }
              sub={
                activeBundle
                  ? `${activeBundle.hits} 的中 / ${activeBundle.participated_races} 参加${
                      activeBundle.hit_rate_ci_low != null &&
                      activeBundle.hit_rate_ci_high != null
                        ? ` ・ CI ${fmtPct(activeBundle.hit_rate_ci_low, 0)}–${fmtPct(
                            activeBundle.hit_rate_ci_high,
                            0,
                          )}`
                        : ""
                    }`
                  : "賭けたレースなし"
              }
            />
            <KpiCell
              label="参加レース数"
              value={activeBundle?.participated_races ?? 0}
              sub={activeBundle ? `集計対象 ${activeBundle.races} レース` : "—"}
            />
            <KpiCell
              label="見送りレース数"
              value={activeBundle?.skipped_races ?? 0}
              sub={
                activeBundle && activeBundle.races > 0
                  ? `見送り率 ${Math.round(
                      (activeBundle.skipped_races / activeBundle.races) * 100,
                    )}%`
                  : "—"
              }
            />
          </div>

          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-3 border-t border-(--color-line-soft)">
            <span>
              ※ 全て {activeBundleLabel} (実弾投票束) 基準 ／ 集計対象{" "}
              {activeBundle?.races ?? 0} レース
              {activeCutoffDate ? ` (${activeCutoffDate}〜)` : ""}
            </span>
            {confidence && <Badge tone={confidence.tone}>{confidence.label}</Badge>}
            <span className="inline-flex items-center gap-1">
              <History className="w-3 h-3" />
              最終更新 {lastUpdated}
            </span>
          </div>
        </div>
      </section>

      {/* ====== EV束 セクション — **実弾既定束 (2026-06-10〜, KEIBA_BET_BUNDLE=ev)**。
          全脚がシェード込み P×O≥1.02 + px_o≤2.0 + ½Kelly + トリガミ防止を通過した時のみ
          legs が立つ = 大半のレースは見送り (正常)。系列色=スカイブルー。 ====== */}
      <section className="space-y-3">
        <SectionHeading
          icon={<Target className="w-3.5 h-3.5" />}
          eyebrow="EV Bundle"
          title="EV束"
          desc={`joint ½Kelly・シェード込み P×O ゲート / 実弾既定束 (${evCutoffDate ?? "2026-06-10"}〜)`}
          eyebrowClass="text-sky-300"
          lineClass="from-sky-500/40"
        />
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
        {/* 全期間参考 (claude_bundle): β=0 事故時代込み。実弾系列の判断には上の EV束を使う */}
        {claudeBundle && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-(--color-line-soft) bg-(--color-surface-2) px-3 py-2 text-[11px] text-(--color-muted)">
            <span className="font-bold text-(--color-foreground)/80">
              EV束 全期間参考
            </span>
            <span>
              回収率{" "}
              <span className="tnum font-semibold text-(--color-foreground)">
                {claudeBundle.participated_races > 0
                  ? fmtRoiPct(claudeBundle.roi_final ?? claudeBundle.roi)
                  : "—"}
              </span>
            </span>
            <span>
              的中{" "}
              <span className="tnum">
                {claudeBundle.hits}/{claudeBundle.participated_races}
              </span>
            </span>
            <span>
              収支{" "}
              <span
                className={`tnum font-semibold ${
                  (claudeBundle.payout_final ?? claudeBundle.payout) -
                    claudeBundle.stake <
                  0
                    ? "text-rose-300"
                    : "text-emerald-300"
                }`}
              >
                {fmtSignedYen(
                  (claudeBundle.payout_final ?? claudeBundle.payout) -
                    claudeBundle.stake,
                )}
              </span>
            </span>
            <span className="text-(--color-muted)/70">
              ※ β=0 事故時代込みの全期間集計 (参考値)
            </span>
          </div>
        )}
        <div className="text-[10px] text-(--color-muted) text-right px-1">
          ※ EV束 (実弾既定束)。計測対象は {evCutoffDate ?? "2026-06-10"}〜 のレースのみ
          (それ以前の旧 EV束は β=0 事故込みの別物のため除外)。見送り多数は正常動作
        </div>
      </section>

      {/* ====== 3連単的中モード セクション — KEIBA_BET_BUNDLE=trifecta 選択時の実弾束。
          市場無視・Claude 指数フォーメーション。系列色=フクシア。 ====== */}
      <section className="space-y-3">
        <SectionHeading
          icon={<Sparkles className="w-3.5 h-3.5" />}
          eyebrow="Trifecta Bundle"
          title="3連単束"
          desc="市場無視・Claude 指数フォーメーション / KEIBA_BET_BUNDLE=trifecta 選択時の実弾束"
          eyebrowClass="text-fuchsia-300"
          lineClass="from-fuchsia-500/40"
        />
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

      {/* ====== チャート: 累積収支 / 回収率推移 / 系列別回収率 / 結果分布 (recharts) ====== */}
      {points.length > 0 ? (
        <section className="space-y-3">
          <SectionHeading
            icon={<ChartSpline className="w-3.5 h-3.5" />}
            eyebrow="Performance"
            title="チャート"
            desc="累積収支 / 回収率推移 / 結果分布"
            eyebrowClass="text-emerald-300"
            lineClass="from-emerald-500/40"
          />
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <Card title="累積収支">
              <NetTrendChart data={points} />
            </Card>
            <Card title="累積回収率 推移">
              <RoiTrendChart data={points} />
            </Card>
            {roiBars.length > 0 && (
              <Card title="系列別 回収率 (最終オッズ基準)">
                <BundleRoiBarsChart data={roiBars} />
              </Card>
            )}
            <Card title="結果分布">
              <div className="space-y-4">
                <DistroBar
                  label={`EV束 (実弾既定, ${evCutoffDate ?? "2026-06-10"}〜)`}
                  hits={evDistro.hits}
                  misses={evDistro.misses}
                  skips={evDistro.skips}
                />
                <DistroBar
                  label="3連単束"
                  hits={tDistro.hits}
                  misses={tDistro.misses}
                  skips={tDistro.skips}
                />
              </div>
            </Card>
          </div>
        </section>
      ) : (
        <Card>
          <div className="text-xs text-(--color-muted)">
            まだ計測対象レースがありません。watch-auto を稼働させると実弾系列の計測がここに蓄積されます。
          </div>
        </Card>
      )}

      {/* ====== 直近レース: 計測対象の最新 12 件。backfill (paper 後付け) はグレーアウト ====== */}
      {recentRaces.length > 0 && (
        <section className="space-y-3">
          <SectionHeading
            icon={<History className="w-3.5 h-3.5" />}
            eyebrow="Recent Races"
            title="直近レース"
            desc="計測対象の最新 12 件 (詳細は各レースへ)"
          />
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-xs table-zebra">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                    <th className="px-2 py-2 font-bold">レース</th>
                    <th className="px-2 py-2 font-bold">日時</th>
                    <th className="px-2 py-2 font-bold">着順</th>
                    <th className="px-2 py-2 font-bold">EV束</th>
                    <th className="px-2 py-2 font-bold">3連単束</th>
                  </tr>
                </thead>
                <tbody>
                  {recentRaces.map((r) => {
                    const backfilled = r.bundle_backfilled === true;
                    const evNet = r.bundle_participated
                      ? (r.bundle_payout_final ?? r.bundle_payout ?? 0) -
                        (r.bundle_stake ?? 0)
                      : null;
                    const tNet = r.trifecta_bundle_participated
                      ? (r.trifecta_bundle_payout_final ??
                          r.trifecta_bundle_payout ??
                          0) - (r.trifecta_bundle_stake ?? 0)
                      : null;
                    return (
                      <tr
                        key={r.race_id}
                        className={`border-b border-(--color-line-soft) ${
                          backfilled ? "opacity-50" : ""
                        }`}
                      >
                        <td className="px-2 py-2 whitespace-nowrap">
                          <Link
                            href={`/predictions/${r.race_id}`}
                            className="font-bold hover:text-(--color-accent) transition-colors"
                          >
                            {r.venue || r.race_id}
                          </Link>
                          <span className="ml-1.5 text-[10px] text-(--color-muted) mono">
                            {r.race_id}
                          </span>
                        </td>
                        <td className="px-2 py-2 text-(--color-muted) tnum whitespace-nowrap">
                          {r.saved_at
                            ? r.saved_at.slice(5, 16).replace("T", " ")
                            : "—"}
                        </td>
                        <td className="px-2 py-2 tnum whitespace-nowrap">
                          {r.finish && r.finish.length > 0
                            ? r.finish.join("-")
                            : "—"}
                        </td>
                        <td className="px-2 py-2">
                          <SeriesResult
                            measured={r.ev_measured === true}
                            participated={r.bundle_participated === true}
                            hit={r.bundle_hit === true}
                            net={evNet}
                            backfilled={backfilled}
                          />
                        </td>
                        <td className="px-2 py-2">
                          <SeriesResult
                            measured={r.trifecta_measured !== false}
                            participated={r.trifecta_bundle_participated === true}
                            hit={r.trifecta_bundle_hit === true}
                            net={tNet}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        </section>
      )}
    </Page>
  );
}
