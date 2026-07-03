import Link from "next/link";
import type { ReactNode } from "react";
import { Coins, History, Layers, Scale, ServerOff } from "lucide-react";
import {
  api,
  type MarketAgreementResponse,
  type ShobuPnl,
  type ShobuPnlRace,
  type StrategiesPnl,
} from "@/lib/api";
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
import { VersionHeading } from "@/components/VersionHeading";

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

// shobu 評価レース全体 (recommended に限らない) の仮想収支カード (ダッシュボードの主役)。
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
            Live P/L — shobu 評価レース全体 (推奨+非推奨・上位N頭3連単BOX)
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
            ※ 推奨+非推奨を含む shobu 評価レース全体の paper P/L (当日スキャン母集団のみ・
            betting pipeline の過去スコアは含めない) ／ BOXルール: 8頭以上=5頭(60点) / 7頭=4頭(24点) / 少頭数は最低3頭を場外に残す
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

// Claude 指数 単純戦略くらべ (単勝#1 / 複勝#2,3 / 馬連#1-2 / 単複) の仮想収支カード (比較テーブル)。
// data = shobu 評価レース全体 (過去分全て)。各戦略を横並びで ROI / 的中率 / 収支 比較する。
function StrategiesPnlCard({
  data,
  nowMs,
}: {
  data: StrategiesPnl;
  nowMs: number;
}) {
  const lastUpdated = data.last_updated_at
    ? fmtRelativeFromNow(data.last_updated_at, nowMs)
    : "—";
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Coins className="w-4 h-4 text-amber-300" />
          <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
            Live P/L — Claude 指数 単純戦略くらべ (shobu 評価レース全体)
          </span>
          {data.sample_warning && <Badge tone="muted">サンプル少</Badge>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs table-zebra">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                <th className="px-2 py-2 font-bold">戦略</th>
                <th className="px-2 py-2 font-bold text-right">回収率</th>
                <th className="px-2 py-2 font-bold text-right">的中率</th>
                <th className="px-2 py-2 font-bold text-right">賭金 → 払戻</th>
                <th className="px-2 py-2 font-bold text-right">収支</th>
                <th className="px-2 py-2 font-bold text-right">対象</th>
              </tr>
            </thead>
            <tbody>
              {data.strategies.map((s) => {
                const hasBets = s.bets > 0;
                return (
                  <tr key={s.key} className="border-b border-(--color-line-soft)">
                    <td className="px-2 py-2 font-bold whitespace-nowrap">{s.label}</td>
                    <td
                      className={`px-2 py-2 text-right tnum font-bold ${
                        !hasBets ? "" : s.roi >= 1 ? "text-emerald-300" : "text-rose-300"
                      }`}
                    >
                      {hasBets ? fmtRoiPct(s.roi) : "—"}
                      {hasBets && s.roi_ci_low != null && s.roi_ci_high != null && (
                        <span className="block text-[10px] font-normal text-(--color-muted)">
                          CI {Math.round(s.roi_ci_low * 100)}–{Math.round(s.roi_ci_high * 100)}%
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-right tnum text-(--color-muted) whitespace-nowrap">
                      {hasBets ? fmtPct(s.hit_rate, 1) : "—"}
                      {/* 母数はレース数 (races_hit / races)。脚数 (bets) は stake 列に反映。 */}
                      <span className="block text-[10px]">
                        {s.races_hit}/{s.races}
                      </span>
                    </td>
                    <td className="px-2 py-2 text-right tnum text-(--color-muted) whitespace-nowrap">
                      {hasBets ? `${fmtYen(s.stake)} → ${fmtYen(s.payout)}` : "—"}
                    </td>
                    <td
                      className={`px-2 py-2 text-right tnum font-bold whitespace-nowrap ${
                        !hasBets ? "" : s.net < 0 ? "text-rose-300" : "text-emerald-300"
                      }`}
                    >
                      {hasBets ? fmtSignedYen(s.net) : "—"}
                    </td>
                    <td className="px-2 py-2 text-right tnum text-(--color-muted)">
                      {s.races}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-2 border-t border-(--color-line-soft)">
          <span>
            ※ 各脚 ¥{data.point_cost}: 単勝=指数1位 / 複勝=指数1位・2位・3位 (頭数ルール: 8頭以上=3着・5-7頭=2着・4頭以下=無)
            / 馬連=指数1-2位 (上位2着) / ワイド=指数1-2位・指数1-3位・ワイドBOX=指数1-2-3 (3点) (両馬が上位3着)
            / 馬単=指数1→2位 (着順一致) / 3連単=指数1→2→3 / 3連複=指数1-2-3 (順不同) / 3連複BOX=指数1-2-3-4 (4点)。
            <strong>全券種ともスナップショット時点オッズ ≤1.1 のとき買い見送り</strong> (複勝/ワイドはレンジ下限で判定=保守的)。
            的中率の母数はレース数 (的中R/対象R) ・ 対象={data.races}R 中の賭けたレース数
          </span>
          {data.skipped_no_odds > 0 && (
            <span>・払戻オッズ欠落 {data.skipped_no_odds} 件は分母外</span>
          )}
          <span className="inline-flex items-center gap-1">
            <History className="w-3 h-3" />
            最終更新 {lastUpdated}
          </span>
        </div>
      </div>
    </Card>
  );
}

// shobu 評価レース全体の per-race 明細 (Claude 指数上位N頭3連単BOX)。version 分割で各セクションに置く。
function BoxDetailTable({ rows }: { rows: ShobuPnlRace[] }) {
  if (rows.length === 0) {
    return (
      <Card>
        <div className="text-xs text-(--color-muted)">
          このバージョンの結果確定レースはまだありません。
        </div>
      </Card>
    );
  }
  const detailRows = [...rows].sort((a, b) =>
    (b.saved_at ?? b.date ?? "").localeCompare(a.saved_at ?? a.date ?? ""),
  );
  return (
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
                <td className="px-2 py-2 mono whitespace-nowrap">{r.top_horses.join(",")}</td>
                <td className="px-2 py-2 mono tnum whitespace-nowrap">
                  {r.finish.length > 0 ? r.finish.join("-") : "—"}
                </td>
                <td className="px-2 py-2 mono tnum text-right whitespace-nowrap">
                  {r.hit ? fmtYen(r.payout) : ""}
                </td>
                <td className="px-2 py-2">
                  <Badge tone={r.hit ? "good" : "muted"}>{r.hit ? "的中" : "不的中"}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// 市場一致シグナル (Claude#1==市場1番人気 で券種ROIを分割) の蓄積カード。確証まで自動更新。
function MarketAgreementCard({ data }: { data: MarketAgreementResponse }) {
  const c = data.current;
  const has = c.races > 0;
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Scale className="w-4 h-4 text-teal-300" />
          <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
            研究中シグナル — 市場一致 (Claude#1 == 市場1番人気) で券種 ROI を分割・確証まで自動蓄積
          </span>
          {c.sample_warning && <Badge tone="muted">サンプル少</Badge>}
        </div>
        {has ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs table-zebra">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                  <th className="px-2 py-2 font-bold">券種 / スタイル</th>
                  <th className="px-2 py-2 font-bold text-right">一致 ROI</th>
                  <th className="px-2 py-2 font-bold text-right">不一致 ROI</th>
                  <th className="px-2 py-2 font-bold text-right">Δ(一致−不一致)</th>
                  <th className="px-2 py-2 font-bold text-right">95%CI(Δ)</th>
                  <th className="px-2 py-2 font-bold">状態</th>
                </tr>
              </thead>
              <tbody>
                {c.metrics.map((m) => (
                  <tr key={m.key} className="border-b border-(--color-line-soft)">
                    <td className="px-2 py-2 font-bold whitespace-nowrap">{m.label}</td>
                    <td className="px-2 py-2 text-right tnum">{fmtRoiPct(m.agree_roi)}</td>
                    <td className="px-2 py-2 text-right tnum">{fmtRoiPct(m.disagree_roi)}</td>
                    <td
                      className={`px-2 py-2 text-right tnum font-bold ${
                        m.delta < 0 ? "text-rose-300" : "text-emerald-300"
                      }`}
                    >
                      {m.delta >= 0 ? "+" : ""}
                      {Math.round(m.delta * 100)}pt
                    </td>
                    <td className="px-2 py-2 text-right tnum text-(--color-muted) whitespace-nowrap">
                      {Math.round(m.delta_ci_low * 100)}〜{Math.round(m.delta_ci_high * 100)}
                    </td>
                    <td className="px-2 py-2">
                      <Badge tone={m.significant ? "good" : "muted"}>
                        {m.significant ? "★確証" : "蓄積中"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-xs text-(--color-muted)">
            市場指数付きの結果確定レースがまだありません。
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-2 border-t border-(--color-line-soft)">
          <span>
            対象 {c.races}R (一致 {c.agree_n} / 不一致 {c.disagree_n}) ・ 自動蓄積 {data.appends} 回 ・
            結果取得 (make api) ごとに更新
          </span>
          <span>
            ※ Δの95%CIが0を跨がなくなれば確証(★)。仮説: 馬連/組合せ系は一致時(consensus)に伸び、
            3連複BOXは不一致(Claude contrarian)時に伸びる。{data.history.length}件 蓄積。
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

// 1 バージョン分の計測セクション (BOX 収支 + 戦略くらべ + per-race 明細)。
function VersionMeasurementSection({
  version,
  box,
  strategies,
  nowMs,
}: {
  version: "v1" | "v2" | "v3" | "β";
  box: ShobuPnl | null;
  strategies: StrategiesPnl | null;
  nowMs: number;
}) {
  // β など対象レースが 0 のバージョンは「計測なし」の薄い表示に畳む。
  if (box && box.races === 0 && (!strategies || strategies.races === 0)) {
    return (
      <>
        <VersionHeading version={version} />
        <Card>
          <div className="text-xs text-(--color-muted)">
            このバージョンの結果確定レースはまだありません。
          </div>
        </Card>
      </>
    );
  }
  return (
    <>
      <VersionHeading version={version} />
      {box ? (
        <IndexedPnlCard data={box} nowMs={nowMs} />
      ) : (
        <Card>
          <div className="text-xs text-(--color-muted)">BOX 収支を取得できませんでした。</div>
        </Card>
      )}
      {strategies && <StrategiesPnlCard data={strategies} nowMs={nowMs} />}
      {box && <BoxDetailTable rows={box.races_detail} />}
    </>
  );
}

export default async function DashboardPage() {
  // 計測を Claude 指数バージョン毎に分離 (ユーザ指示 2026-06-30: 新しい/現行が上)。
  // β (市場由来・〜2026-06-21) は対象が少ないため表示しない (ユーザ指示で撤去)。
  const [boxV3, boxV2, boxV1, stratV3, stratV2, stratV1, marketAgree] = await Promise.all([
    api.indexedPnl(100, "v3").catch(() => null),
    api.indexedPnl(100, "v2").catch(() => null),
    api.indexedPnl(100, "v1").catch(() => null),
    api.indexedStrategiesPnl(100, "v3").catch(() => null),
    api.indexedStrategiesPnl(100, "v2").catch(() => null),
    api.indexedStrategiesPnl(100, "v1").catch(() => null),
    api.marketAgreement().catch(() => null),
  ]);

  // API 未接続: どのバージョンも取れなければ明示のエラーカードを出す。
  if (!boxV3 && !boxV2 && !boxV1) {
    return (
      <Page>
        <AutoRefresh seconds={15} />
        <PageHeader
          eyebrow="Keiba EV Terminal"
          title="ダッシュボード"
          subtitle="shobu 評価レースの仮想収支を Claude 指数バージョン毎に俯瞰。15 秒おきに自動更新。"
        />
        <ApiDownCard />
      </Page>
    );
  }

  // RSC はリクエスト毎に 1 回だけ描画されるので、リクエスト時刻の取得は安全。
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        eyebrow="Keiba EV Terminal"
        title="ダッシュボード"
        subtitle="shobu 評価レースの仮想収支 (上位N頭3連単BOX + 単純戦略くらべ) を Claude 指数バージョン毎に表示 (v3=仮指数アンカー・現行 / v2=補強根拠無制限 / v1=3件上限)。競馬場別の内訳は上部メニューの「競馬場別」へ。15 秒おきに自動更新。"
      />

      {/* ====== 研究中シグナル: 市場一致 (自動蓄積・確証まで) ====== */}
      {marketAgree && <MarketAgreementCard data={marketAgree} />}

      {/* ====== v3 (現行・仮指数アンカー) を上、v2 / v1 (旧) を下 ====== */}
      <VersionMeasurementSection version="v3" box={boxV3} strategies={stratV3} nowMs={nowMs} />
      <VersionMeasurementSection version="v2" box={boxV2} strategies={stratV2} nowMs={nowMs} />
      <VersionMeasurementSection version="v1" box={boxV1} strategies={stratV1} nowMs={nowMs} />
    </Page>
  );
}
