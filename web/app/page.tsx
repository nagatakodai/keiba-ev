import Link from "next/link";
import type { ReactNode } from "react";
import { Coins, History, Layers, ServerOff } from "lucide-react";
import {
  api,
  type ShobuPnl,
  type ShobuPnlRace,
  type WinPlacePnl,
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

// Claude 指数 単複戦略 (1位=単勝 / 2,3位=複勝) の仮想収支カード。
// data = shobu 評価レース全体 (過去分全て)、rec = うち勝負レース(推奨) のサブ行 (任意)。
function WinPlacePnlCard({
  data,
  rec,
  nowMs,
}: {
  data: WinPlacePnl;
  rec?: WinPlacePnl | null;
  nowMs: number;
}) {
  const has = data.races > 0;
  const lastUpdated = data.last_updated_at
    ? fmtRelativeFromNow(data.last_updated_at, nowMs)
    : "—";
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Coins className="w-4 h-4 text-amber-300" />
          <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
            参考 — Claude 指数 単複戦略 (1位=単勝 ／ 2・3位=複勝・shobu 評価レース全体)
          </span>
          {data.sample_warning && <Badge tone="muted">サンプル少</Badge>}
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-x-6 gap-y-4">
          <KpiCell
            label="収支"
            value={has ? fmtSignedYen(data.net) : "—"}
            valueClass={!has ? "" : data.net < 0 ? "text-rose-300" : "text-emerald-300"}
            sub={has ? `賭金 ${fmtYen(data.stake)} → 払戻 ${fmtYen(data.payout)}` : "—"}
          />
          <KpiCell
            label="回収率 (単複合計)"
            value={has ? fmtRoiPct(data.roi) : "—"}
            valueClass={!has ? "" : data.roi >= 1 ? "text-emerald-300" : "text-rose-300"}
            sub={
              data.roi_ci_low != null && data.roi_ci_high != null
                ? `95%CI ${Math.round(data.roi_ci_low * 100)}–${Math.round(data.roi_ci_high * 100)}%`
                : "—"
            }
          />
          <KpiCell
            label="単勝 #1"
            value={has ? fmtRoiPct(data.win_roi) : "—"}
            valueClass={!has ? "" : data.win_roi >= 1 ? "text-emerald-300" : "text-rose-300"}
            sub={
              has
                ? `的中 ${fmtPct(data.win_hit_rate, 1)} (${data.win_hits}/${data.win_bets})`
                : "—"
            }
          />
          <KpiCell
            label="複勝 #2,3"
            value={has ? fmtRoiPct(data.place_roi) : "—"}
            valueClass={!has ? "" : data.place_roi >= 1 ? "text-emerald-300" : "text-rose-300"}
            sub={
              has
                ? `的中 ${fmtPct(data.place_hit_rate, 1)} (${data.place_hits}/${data.place_bets})`
                : "—"
            }
          />
          <KpiCell
            label="対象レース"
            value={data.races}
            sub={`shobu評価 ${data.recommended_total} / 結果待ち ${data.skipped_no_result} / 指数なし ${data.skipped_no_index}`}
          />
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-2 border-t border-(--color-line-soft)">
          <span>
            ※ 各レース ¥{data.point_cost} ずつ: Claude 指数1位の単勝 + 2・3位の複勝 (複勝は頭数ルール
            適用: 8頭以上=3着まで / 5-7頭=2着まで / 4頭以下=発売なし)
          </span>
          {rec && rec.races > 0 && (
            <span>
              ・うち勝負レース(推奨){rec.races}R: 回収率 {fmtRoiPct(rec.roi)} (収支{" "}
              {fmtSignedYen(rec.net)})
            </span>
          )}
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
  const [indexed, winplace, winplaceRec] = await Promise.all([
    api.indexedPnl().catch(() => null),
    api.indexedWinplacePnl().catch(() => null),
    api.winplacePnl().catch(() => null),
  ]);

  // API 未接続: silent null ではなく明示のエラーカードを出す。
  if (!indexed) {
    return (
      <Page>
        <AutoRefresh seconds={15} />
        <PageHeader
          eyebrow="Keiba EV Terminal"
          title="ダッシュボード"
          subtitle="shobu 評価レース全体の仮想収支 (上位N頭3連単BOX) を俯瞰。15 秒おきに自動更新。"
        />
        <ApiDownCard />
      </Page>
    );
  }

  // RSC はリクエスト毎に 1 回だけ描画されるので、リクエスト時刻の取得は安全。
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();
  const hasRaces = indexed.races > 0;

  // 明細は最新が上 (saved_at / date 降順) — shobu 評価レース全体の per-race。
  const detailRows: ShobuPnlRace[] = [...indexed.races_detail].sort((a, b) =>
    (b.saved_at ?? b.date ?? "").localeCompare(a.saved_at ?? a.date ?? ""),
  );

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        eyebrow="Keiba EV Terminal"
        title="ダッシュボード"
        subtitle="shobu 評価レース全体の仮想収支 (上位N頭3連単BOX) を俯瞰。15 秒おきに自動更新。"
      />

      {/* ====== 主役: shobu 評価レース全体の仮想収支 (Claude 指数上位N頭3連単BOX) ====== */}
      <IndexedPnlCard data={indexed} nowMs={nowMs} />

      {/* ====== Claude 指数 単複戦略 (1位=単勝 / 2,3位=複勝) ====== */}
      {winplace && (
        <WinPlacePnlCard data={winplace} rec={winplaceRec} nowMs={nowMs} />
      )}

      {/* ====== per-race 明細 (shobu 評価レース全体) ====== */}
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
            shobu 評価レースのデータがありません (今日の勝負レースをスキャンしてください)。
          </div>
        </Card>
      )}
    </Page>
  );
}
