import Link from "next/link";
import type { ReactNode } from "react";
import { Activity, History, Layers, ServerOff } from "lucide-react";
import { api, type ShobuPnl, type ShobuPnlRace } from "@/lib/api";
import {
  Badge,
  Card,
  Page,
  PageHeader,
  fmtPct,
  fmtRelativeFromNow,
  fmtYen,
} from "@/components/ui";
import { AutoRefresh } from "@/components/AutoRefresh";

export const dynamic = "force-dynamic";

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}

function fmtSignedYen(n: number): string {
  return `${n < 0 ? "-" : "+"}¥${Math.abs(n).toLocaleString()}`;
}

// ============================================================
// 表示部品 (server-rendered)
// ============================================================

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

// hero 用の装飾スパークライン (勝負レース仮想収支の累積)。
// 軸なしの純装飾なので server-render の極小 SVG で済ませる。
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

// shobu 評価レース全体 (recommended に限らない) の仮想収支カード (勝負レースとは別カードで併記)。
function IndexedPnlCard({ data, nowMs }: { data: ShobuPnl; nowMs: number }) {
  const pl = data.payout - data.stake;
  const has = data.races > 0;
  const lastUpdated = data.last_updated_at
    ? fmtRelativeFromNow(data.last_updated_at, nowMs)
    : "—";
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Layers className="w-4 h-4 text-sky-300" />
          <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
            参考 — shobu 評価レース全体 (推奨+非推奨・上位N頭3連単BOX)
          </span>
          {data.sample_warning && <Badge tone="muted">サンプル少</Badge>}
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-4">
          <KpiCell
            label="収支"
            value={has ? fmtSignedYen(pl) : "—"}
            valueClass={!has ? "" : pl < 0 ? "text-rose-300" : "text-emerald-300"}
            sub={has ? `賭金 ${fmtYen(data.stake)} → 払戻 ${fmtYen(data.payout)}` : "—"}
          />
          <KpiCell
            label="回収率"
            value={has ? fmtRoiPct(data.roi) : "—"}
            valueClass={!has ? "" : data.roi >= 1 ? "text-emerald-300" : "text-rose-300"}
            sub={
              data.roi_ci_low != null && data.roi_ci_high != null
                ? `95%CI ${Math.round(data.roi_ci_low * 100)}–${Math.round(data.roi_ci_high * 100)}%`
                : "—"
            }
          />
          <KpiCell
            label="的中率"
            value={has ? fmtPct(data.hit_rate, 1) : "—"}
            sub={has ? `${data.hits} 的中 / ${data.races} レース` : "対象なし"}
          />
          <KpiCell
            label="対象レース"
            value={data.races}
            sub={`shobu評価 ${data.recommended_total} / 結果待ち ${data.skipped_no_result}`}
          />
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-2 border-t border-(--color-line-soft)">
          <span>
            ※ 推奨に限らず shobu が評価した全レースの paper P/L (推奨カードの superset・
            ほぼ推奨。betting pipeline の過去スコアは含めず当日スキャン母集団のみ)
          </span>
          <span className="inline-flex items-center gap-1">
            <History className="w-3 h-3" />
            最終更新 {lastUpdated}
          </span>
        </div>
      </div>
    </Card>
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

export default async function DashboardPage() {
  const [shobu, indexed] = await Promise.all([
    api.shobuPnl().catch(() => null),
    api.indexedPnl().catch(() => null),
  ]);

  // API 未接続: silent null ではなく明示のエラーカードを出す。
  if (!shobu) {
    return (
      <Page>
        <AutoRefresh seconds={15} />
        <PageHeader
          eyebrow="Keiba EV Terminal"
          title="ダッシュボード"
          subtitle="勝負レースの仮想収支 (上位N頭3連単BOX) を俯瞰。15 秒おきに自動更新。"
        />
        <ApiDownCard />
      </Page>
    );
  }

  // RSC はリクエスト毎に 1 回だけ描画されるので、リクエスト時刻の取得は安全。
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();
  const lastUpdated = shobu.last_updated_at
    ? fmtRelativeFromNow(shobu.last_updated_at, nowMs)
    : "—";

  const heroPl = shobu.payout - shobu.stake;
  const heroPositive = heroPl >= 0;
  const hasRaces = shobu.races > 0;

  // 日付昇順に累積した net で hero スパークライン (装飾)。
  const sortedDetail = [...shobu.races_detail].sort((a, b) =>
    (a.saved_at ?? a.date ?? "").localeCompare(b.saved_at ?? b.date ?? ""),
  );
  let netAcc = 0;
  const heroSpark = sortedDetail.map((r) => {
    netAcc += (r.hit ? r.payout : 0) - r.stake;
    return netAcc;
  });

  // 明細は最新が上 (saved_at / date 降順)
  const detailRows: ShobuPnlRace[] = [...shobu.races_detail].sort((a, b) =>
    (b.saved_at ?? b.date ?? "").localeCompare(a.saved_at ?? a.date ?? ""),
  );

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        eyebrow="Keiba EV Terminal"
        title="ダッシュボード"
        subtitle="勝負レースの仮想収支 (上位N頭3連単BOX) を俯瞰。15 秒おきに自動更新。"
      />

      {/* ====== HERO: 勝負レース仮想収支 (Claude 指数上位N頭の3連単BOX paper P/L) ====== */}
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
              Live P/L — 勝負レース (上位N頭3連単BOX)
            </span>
            {shobu.sample_warning && <Badge tone="muted">サンプル少</Badge>}
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-[1.6fr_1fr_1fr_1fr] gap-x-6 gap-y-5 items-start">
            <div className="col-span-2 lg:col-span-1">
              <div className="text-[10px] font-bold tracking-widest uppercase text-(--color-muted)">
                収支
              </div>
              <div
                className={`text-4xl font-black tnum tracking-tight mt-1 ${
                  !hasRaces
                    ? ""
                    : heroPl < 0
                    ? "text-rose-300"
                    : "text-emerald-300"
                }`}
              >
                {!hasRaces ? "—" : fmtSignedYen(heroPl)}
              </div>
              <div className="text-[11px] text-(--color-muted) tnum mt-0.5">
                {hasRaces
                  ? `賭金 ${fmtYen(shobu.stake)} → 払戻 ${fmtYen(shobu.payout)}`
                  : "—"}
              </div>
              <Sparkline values={heroSpark} positive={heroPositive} />
            </div>
            <KpiCell
              label="回収率"
              value={hasRaces ? fmtRoiPct(shobu.roi) : "—"}
              valueClass={
                !hasRaces ? "" : shobu.roi >= 1 ? "text-emerald-300" : "text-rose-300"
              }
              sub={
                shobu.roi_ci_low != null && shobu.roi_ci_high != null
                  ? `95%CI ${Math.round(shobu.roi_ci_low * 100)}–${Math.round(
                      shobu.roi_ci_high * 100,
                    )}%`
                  : "—"
              }
            />
            <KpiCell
              label="的中率"
              value={hasRaces ? fmtPct(shobu.hit_rate, 1) : "—"}
              sub={
                hasRaces
                  ? `${shobu.hits} 的中 / ${shobu.races} レース${
                      shobu.hit_rate_ci_low != null && shobu.hit_rate_ci_high != null
                        ? ` ・ CI ${fmtPct(shobu.hit_rate_ci_low, 0)}–${fmtPct(
                            shobu.hit_rate_ci_high,
                            0,
                          )}`
                        : ""
                    }`
                  : "対象レースなし"
              }
            />
            <KpiCell
              label="対象レース"
              value={shobu.races}
              sub={`勝負レース ${shobu.recommended_total} / 結果待ち ${shobu.skipped_no_result} / 指数なし ${shobu.skipped_no_index}`}
            />
          </div>

          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-3 border-t border-(--color-line-soft)">
            <span>
              ※ Claude 指数上位N頭の3連単BOX paper P/L ／ BOXルール: 8頭以上=5頭(60点) / 7頭=4頭(24点) / 少頭数は最低3頭を場外に残す
            </span>
            {shobu.sample_warning && <Badge tone="muted">サンプル少 (n&lt;30)</Badge>}
            <span className="inline-flex items-center gap-1">
              <History className="w-3 h-3" />
              最終更新 {lastUpdated}
            </span>
          </div>
        </div>
      </section>

      {/* ====== 別カード: shobu 評価レース全体の仮想収支 (非破壊・参考) ====== */}
      {indexed && <IndexedPnlCard data={indexed} nowMs={nowMs} />}

      {/* ====== per-race 明細 (勝負レース=推奨) ====== */}
      {hasRaces ? (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-xs table-zebra">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                  <th className="px-2 py-2 font-bold">レース</th>
                  <th className="px-2 py-2 font-bold">日付</th>
                  <th className="px-2 py-2 font-bold">BOX</th>
                  <th className="px-2 py-2 font-bold">上位</th>
                  <th className="px-2 py-2 font-bold">着順</th>
                  <th className="px-2 py-2 font-bold text-right">払戻</th>
                  <th className="px-2 py-2 font-bold">結果</th>
                </tr>
              </thead>
              <tbody>
                {detailRows.map((r) => (
                  <tr
                    key={r.race_id}
                    className={`border-b border-(--color-line-soft) ${
                      r.hit ? "bg-emerald-500/10" : ""
                    }`}
                  >
                    <td className="px-2 py-2 whitespace-nowrap">
                      <Link
                        href={`/predictions/${r.race_id}`}
                        className="font-bold hover:text-(--color-accent) transition-colors"
                      >
                        {r.venue}
                        {r.race_no != null ? `${r.race_no}R` : ""}
                      </Link>
                    </td>
                    <td className="px-2 py-2 text-(--color-muted) tnum whitespace-nowrap">
                      {r.date}
                    </td>
                    <td className="px-2 py-2 text-(--color-muted) whitespace-nowrap">
                      {r.box}頭BOX
                    </td>
                    <td className="px-2 py-2 mono whitespace-nowrap">
                      {r.top_horses.join(",")}
                    </td>
                    <td className="px-2 py-2 mono tnum whitespace-nowrap">
                      {r.finish.length > 0 ? r.finish.join("-") : "—"}
                    </td>
                    <td className="px-2 py-2 mono tnum text-right whitespace-nowrap">
                      {r.hit ? fmtYen(r.payout) : ""}
                    </td>
                    <td className="px-2 py-2">
                      <Badge tone={r.hit ? "good" : "muted"}>
                        {r.hit ? "的中" : "不的中"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : (
        <Card>
          <div className="text-xs text-(--color-muted)">
            勝負レースのデータがありません (今日の勝負レースをスキャンしてください)。
          </div>
        </Card>
      )}
    </Page>
  );
}
