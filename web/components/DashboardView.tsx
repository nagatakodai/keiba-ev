// ダッシュボード本体 (server component)。venue プロップで 地方(NAR)/中央(JRA) を分離
// (ユーザ指示 2026-07-05「JRAはいったん別ページに避難して。ダッシュボード（地方）と
// ダッシュボード（中央）で分ける」)。app/page.tsx (地方) と app/jra/page.tsx (中央) の
// 薄いラッパから venue を受けて描画する。旧 app/page.tsx の内容を移動 (書き換え最小限)。
import Link from "next/link";
import type { ReactNode } from "react";
import { Coins, FlaskConical, History, Layers, Scale, ServerOff } from "lucide-react";
import {
  api,
  type MarketAgreementResponse,
  type MatrixCell,
  type ShobuPnl,
  type ShobuPnlRace,
  type SignalRule,
  type SignalRulesResponse,
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

export type DashboardVenue = "nar" | "jra";

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
// 券種ラベルの短縮 ("馬連 (指数1-2位)" → "馬連")。マトリクスの列ヘッダ用。
function shortTargetLabel(label: string): string {
  return label.split(" (")[0];
}

// マトリクス 1 セル (状況 × 券種) の ROI 描画。最良=緑背景・確証=★・サンプル不足=淡色。
// marketRoi (市場人気だけで券種を買った基準線) を渡すと、それを上回るセルに ▲ を付けて
// 「Claude 指数戦略が単に人気を買うのに勝てているか」を一目で比較できるようにする。
function MatrixCellTd({
  cell,
  isBest,
  floor,
  marketRoi,
}: {
  cell: MatrixCell;
  isBest: boolean;
  floor: number;
  marketRoi?: number;
}) {
  const enough = cell.legs >= floor;
  const beatsMarket =
    enough && marketRoi !== undefined && cell.legs > 0 && cell.roi > marketRoi;
  return (
    <td
      className={`px-2 py-1.5 text-right tnum align-middle ${
        isBest ? "bg-emerald-500/15" : ""
      }`}
    >
      <div className="flex items-center justify-end gap-1">
        {beatsMarket && (
          <span className="text-sky-300 text-[10px]" title="市場人気だけで買った ROI を上回る">
            ▲
          </span>
        )}
        <span
          className={
            !enough
              ? "text-(--color-muted)"
              : isBest
                ? "font-bold text-emerald-200"
                : ""
          }
        >
          {cell.legs > 0 ? fmtRoiPct(cell.roi) : "—"}
        </span>
        {cell.confirmed && <span className="text-emerald-300 text-[10px]">★</span>}
      </div>
      <span className="block text-[9px] text-(--color-muted) leading-tight">{cell.legs}R</span>
    </td>
  );
}

// 買い方マトリクス: 発走前条件 (一致/型/場) の組合せ × 券種 の ROI を1枚の表で。
// 状況毎の最良の買い方 (best_key=緑ハイライト) と確証 (★=CI下限>100%) を読み取る。
function MarketAgreementCard({ data }: { data: MarketAgreementResponse }) {
  const c = data.current;
  const mx = c.matrix;
  const has = c.races > 0 && !!mx;
  const targets = mx?.targets ?? [];
  const floor = mx?.sample_floor ?? 8;
  // 市場人気だけで券種を買った基準線 (key→ROI)。各セルの比較 (▲) に使う。
  const marketRoiByKey: Record<string, number> = {};
  for (const cell of mx?.market_baseline.cells ?? []) marketRoiByKey[cell.key] = cell.roi;
  // 参考行 (全体 / 市場人気) を本体行と同じ列構成で描くヘルパ。
  // compareMarket=true の行 (全体) は各セルに ▲ (市場人気超) を付ける。市場人気行自身には付けない。
  const refRow = (
    label: string,
    n: number,
    cells: MatrixCell[],
    bestKey: string | null,
    tone: string,
    compareMarket: boolean,
  ) => (
    <tr className={`border-t border-(--color-line) ${tone}`}>
      <td className="px-2 py-1.5">
        <span className="font-bold whitespace-nowrap">{label}</span>
        <span className="block text-[9px] text-(--color-muted) leading-tight">{n}R</span>
      </td>
      {targets.map((t) => {
        const cell = cells.find((x) => x.key === t.key);
        return cell ? (
          <MatrixCellTd
            key={t.key}
            cell={cell}
            isBest={bestKey === t.key}
            floor={floor}
            marketRoi={compareMarket ? marketRoiByKey[t.key] : undefined}
          />
        ) : (
          <td key={t.key} className="px-2 py-1.5 text-right text-(--color-muted)">
            —
          </td>
        );
      })}
    </tr>
  );
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Scale className="w-4 h-4 text-teal-300" />
          <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
            研究中シグナル — 買い方マトリクス (市場一致 × 拮抗/本命 × JRA/NAR → 状況毎の最良の買い方)
          </span>
          {c.sample_warning && <Badge tone="muted">サンプル少</Badge>}
        </div>
        {has ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs table-zebra">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                  <th className="px-2 py-2 font-bold">状況 (一致 / 型 / 場)</th>
                  {targets.map((t) => (
                    <th key={t.key} className="px-2 py-2 font-bold text-right whitespace-nowrap">
                      {shortTargetLabel(t.label)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {mx.rows
                  .filter((r) => r.n > 0)
                  .map((r) => (
                    <tr
                      key={r.signature.join("-")}
                      className="border-b border-(--color-line-soft)"
                    >
                      <td className="px-2 py-1.5">
                        <div className="flex flex-wrap items-center gap-1">
                          {r.labels.map((lab, i) => (
                            <span
                              key={i}
                              className="px-1.5 py-0.5 rounded bg-(--color-surface-2) border border-(--color-line) text-[10px] whitespace-nowrap"
                            >
                              {lab}
                            </span>
                          ))}
                        </div>
                        <span className="block text-[9px] text-(--color-muted) leading-tight pt-0.5">
                          {r.n}R{r.best_key === null && r.n < floor ? " ・サンプル不足" : ""}
                        </span>
                      </td>
                      {targets.map((t) => {
                        const cell = r.cells.find((x) => x.key === t.key);
                        return cell ? (
                          <MatrixCellTd
                            key={t.key}
                            cell={cell}
                            isBest={r.best_key === t.key}
                            floor={floor}
                            marketRoi={marketRoiByKey[t.key]}
                          />
                        ) : (
                          <td key={t.key} className="px-2 py-1.5 text-right text-(--color-muted)">
                            —
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                {refRow(
                  "全体 (条件なし)",
                  mx.overall.n,
                  mx.overall.cells,
                  mx.overall.best_key,
                  "text-(--color-muted)",
                  true,
                )}
                {refRow(
                  "市場人気だけで購入 (基準線)",
                  mx.market_baseline.n,
                  mx.market_baseline.cells,
                  null,
                  "text-sky-200/80",
                  false,
                )}
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
            対象 {c.races}R ・ 自動蓄積 {data.appends} 回 ・ 結果取得 (make api) ごとに更新 ・
            {data.history.length}件 蓄積
          </span>
          <span>
            ※ 各セル = その状況で券種を Claude 指数上位馬で買った ROI (下段=レース数)。
            <span className="text-emerald-200">緑=その状況の最良</span>・
            ★=ROIの95%CI下限が100%超 (確定的に+EV)・
            <span className="text-sky-300">▲=「市場人気だけで購入 (基準線)」の同券種 ROI を上回る</span>。
            {floor}R 未満のセルは信頼できないため淡色・推奨から除外。
            <span className="text-sky-200/80">「市場人気だけで購入 (基準線)」</span>= 各券種を
            市場指数順の上位馬で機械的に買った ROI (Claude 戦略が単に人気を買うのに勝てているかの比較対象)。
          </span>
        </div>
      </div>
    </Card>
  );
}

// プレレジ済ルール 1 行 (登録後データのみで確証判定)。券種 (strategy) でグループ化した表の
// 1 行 = 1 条件定義。`strategySpan` があればグループ先頭行として券種セルを rowSpan で描く
// (ユーザ指示 2026-07-05: 券種 × [発見時参考・登録後・市場人気基準・状態] × 各条件の定義)。
function SignalRuleRow({
  rule,
  minConfirm,
  strategySpan,
}: {
  rule: SignalRule;
  minConfirm: number;
  strategySpan?: number;
}) {
  const p = rule.prospective;
  const i = rule.insample;
  // ROI ≥ 100% の緑ハイライト (ユーザ指示 2026-07-05): 発見時/登録後の ROI と、登録後が
  // 緑のルールは条件の定義も緑にする。broken (rose) は status 装飾を優先。
  const insampleGreen = i.races > 0 && i.roi >= 1.0;
  const prospectiveGreen = p.races > 0 && p.roi >= 1.0;
  const statusClass: Record<SignalRule["status"], string> = {
    confirmed: "bg-emerald-400/20 text-emerald-300",
    promising: "bg-sky-400/20 text-sky-300",
    accumulating: "bg-(--color-surface-2) text-(--color-muted)",
    broken: "bg-rose-500/20 text-rose-300",
  };
  return (
    <tr
      className={
        strategySpan
          ? "border-t border-(--color-line)" // 券種グループの先頭は強い区切り線
          : "border-t border-(--color-line-soft)"
      }
    >
      {strategySpan != null && (
        <td
          rowSpan={strategySpan}
          className="px-2 py-1.5 align-top font-bold whitespace-nowrap border-r border-(--color-line-soft)"
        >
          {rule.strategy_label}
        </td>
      )}
      <td
        className={`px-2 py-1.5 text-[11px] leading-tight ${
          prospectiveGreen && rule.status !== "broken" ? "text-emerald-300" : ""
        }`}
      >
        {rule.condition_label}
        <span className="block text-[9px] text-(--color-muted) leading-tight">
          プレレジ {rule.registered_at}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right tnum text-(--color-muted)">
        {i.races > 0 ? (
          <>
            <span className={insampleGreen ? "text-emerald-300" : ""}>{fmtRoiPct(i.roi)}</span>
            <span className="block text-[9px] leading-tight">
              {i.races}R ・ 的中 {fmtPct(i.hits / i.races, 0)} ・ 単発抜き{" "}
              {fmtRoiPct(i.drop_best_roi ?? 0)}
            </span>
          </>
        ) : (
          "—"
        )}
      </td>
      <td className="px-2 py-1.5 text-right tnum">
        {p.races > 0 ? (
          <>
            <span
              className={
                rule.status === "confirmed"
                  ? "font-black text-emerald-300"
                  : rule.status === "broken"
                    ? "font-bold text-rose-300"
                    : prospectiveGreen
                      ? "font-bold text-emerald-300"
                      : "font-bold"
              }
            >
              {fmtRoiPct(p.roi)}
            </span>
            <span className="block text-[9px] text-(--color-muted) leading-tight">
              {p.races}/{minConfirm}R ・ 的中 {fmtPct(p.hits / p.races, 0)} ・ CI[
              {fmtRoiPct(p.roi_ci_low)},{fmtRoiPct(p.roi_ci_high)}]
            </span>
          </>
        ) : (
          <span className="text-(--color-muted)">
            0/{minConfirm}R
            <span className="block text-[9px] leading-tight">登録後の結果待ち</span>
          </span>
        )}
      </td>
      <td className="px-2 py-1.5 text-right tnum text-sky-200/80">
        {rule.market_baseline.races > 0 ? (
          <>
            {fmtRoiPct(rule.market_baseline.roi)}
            <span className="block text-[9px] leading-tight">
              的中 {fmtPct(rule.market_baseline.hits / rule.market_baseline.races, 0)}
            </span>
          </>
        ) : (
          "—"
        )}
      </td>
      <td className="px-2 py-1.5 text-right">
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-black whitespace-nowrap ${statusClass[rule.status]}`}
        >
          {rule.status_label}
        </span>
      </td>
    </tr>
  );
}

// プレレジ済シグナルルールの検証カード: 発見 (in-sample) と検証 (登録後) を分離し、
// 登録後データの ROI CI だけで 確証★/破綻 を自動判定する。walk-forward ガードレール付き。
function SignalRulesCard({ data }: { data: SignalRulesResponse }) {
  const c = data.current;
  const wfBest = c.walkforward.find((w) => w.key === "matrix_best");
  const deadQuinella = c.dead_cell.targets.find((t) => t.key === "quinella12");
  // 券種 (strategy) でグループ化 (初出順を保持)。同じ券種の条件定義を1つの券種セルにまとめる。
  const groups: SignalRule[][] = [];
  {
    const byStrategy = new Map<string, SignalRule[]>();
    for (const r of c.rules) {
      const g = byStrategy.get(r.strategy);
      if (g) {
        g.push(r);
      } else {
        const arr = [r];
        byStrategy.set(r.strategy, arr);
        groups.push(arr);
      }
    }
  }
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <FlaskConical className="w-4 h-4 text-emerald-300" />
          <span className="text-[10px] font-bold tracking-[0.22em] uppercase text-(--color-muted)">
            研究中シグナル — プレレジ検証 (定義凍結 → 登録後データのみで確証判定)
          </span>
          {c.sample_warning && <Badge tone="muted">サンプル少</Badge>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                <th className="px-2 py-2 font-bold">券種</th>
                <th className="px-2 py-2 font-bold">条件の定義</th>
                <th className="px-2 py-2 font-bold text-right whitespace-nowrap">発見時 (参考)</th>
                <th className="px-2 py-2 font-bold text-right whitespace-nowrap">登録後 (確証判定)</th>
                <th className="px-2 py-2 font-bold text-right whitespace-nowrap">市場人気基準</th>
                <th className="px-2 py-2 font-bold text-right">状態</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) =>
                g.map((r, i) => (
                  <SignalRuleRow
                    key={r.key}
                    rule={r}
                    minConfirm={c.min_confirm}
                    strategySpan={i === 0 ? g.length : undefined}
                  />
                )),
              )}
            </tbody>
          </table>
        </div>
        <div className="rounded-lg border border-amber-400/30 bg-amber-400/5 px-3 py-2 text-[11px] text-(--color-muted)">
          <span className="font-bold text-amber-200">walk-forward ガードレール: </span>
          上の買い方マトリクスの「その時点の best セル」を look-ahead なしで追従した場合の実測は
          {wfBest ? (
            <span className="tnum">
              {" "}
              ROI {fmtRoiPct(wfBest.roi)} ({wfBest.races}R・単発抜き{" "}
              {fmtRoiPct(wfBest.drop_best_roi ?? 0)})
            </span>
          ) : (
            " —"
          )}
          。マトリクスのセル追従は現状 <span className="font-bold">機能しない</span>{" "}
          (in-sample の最良セルはノイズ)。行動に移すのは上の表で「確証★」になったルールのみ。
          {deadQuinella && deadQuinella.dead_races > 0 && (
            <span>
              {" "}
              見送り規律: {c.dead_cell.label} は馬連 ROI {fmtRoiPct(deadQuinella.dead_roi)} (
              {deadQuinella.dead_races}R) vs それ以外 {fmtRoiPct(deadQuinella.alive_roi)} (
              {deadQuinella.alive_races}R) — このゾーンは賭けない。
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--color-muted) pt-2 border-t border-(--color-line-soft)">
          <span>
            対象 {c.races}R ・ 自動蓄積 {data.appends} 回 ・ {data.history.length}件 蓄積
          </span>
          <span>
            ※ 「発見時」= ルールを見つけたデータ込みの全期間 ROI (楽観・参考値)。「登録後」=
            プレレジ日以降の発走レースのみ (真の out-of-sample)。確証★ = 登録後 {c.min_confirm}R
            以上 かつ ROI 95%CI 下限 &gt; 100% ・ 破綻 = 登録後 {c.min_broken}R 以上 かつ CI 上限
            &lt; 100% (ルール棄却)。「市場人気基準」= 同条件で市場人気順に同じ買い方をした全期間
            ROI (Claude 指数の付加価値の基準線)。
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

export async function DashboardView({ venue }: { venue: DashboardVenue }) {
  const isNar = venue === "nar";
  const title = isNar ? "ダッシュボード（地方）" : "ダッシュボード（中央）";
  const venueLabel = isNar ? "地方 (NAR・ばんえい含む)" : "中央 (JRA)";
  // 計測を Claude 指数バージョン毎に分離 (ユーザ指示 2026-06-30: 新しい/現行が上)。
  // β (市場由来・〜2026-06-21) は対象が少ないため表示しない (ユーザ指示で撤去)。
  // 計測系は venue で 地方/中央 を分離 (2026-07-05)。研究系カード (プレレジ台帳・市場一致
  // マトリクス) は venue を条件/軸として内包するグローバル研究台帳なので **地方ページにのみ**
  // フィルタ無しで表示する (中央ページには出さない・重複もしない)。
  const [boxV3, boxV2, boxV1, stratV3, stratV2, stratV1, marketAgree, signalRules] =
    await Promise.all([
      api.indexedPnl(100, "v3", venue).catch(() => null),
      api.indexedPnl(100, "v2", venue).catch(() => null),
      api.indexedPnl(100, "v1", venue).catch(() => null),
      api.indexedStrategiesPnl(100, "v3", venue).catch(() => null),
      api.indexedStrategiesPnl(100, "v2", venue).catch(() => null),
      api.indexedStrategiesPnl(100, "v1", venue).catch(() => null),
      isNar ? api.marketAgreement().catch(() => null) : Promise.resolve(null),
      isNar ? api.signalRules().catch(() => null) : Promise.resolve(null),
    ]);

  // API 未接続: どのバージョンも取れなければ明示のエラーカードを出す。
  if (!boxV3 && !boxV2 && !boxV1) {
    return (
      <Page>
        <AutoRefresh seconds={15} />
        <PageHeader
          eyebrow="Keiba EV Terminal"
          title={title}
          subtitle={`${venueLabel} の shobu 評価レースの仮想収支を Claude 指数バージョン毎に俯瞰。15 秒おきに自動更新。`}
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
        title={title}
        subtitle={`${venueLabel} の shobu 評価レースの仮想収支 (上位N頭3連単BOX + 単純戦略くらべ) を Claude 指数バージョン毎に表示 (v3=仮指数アンカー・現行 / v2=補強根拠無制限 / v1=3件上限)。競馬場別の内訳は上部メニューの「競馬場別」へ。15 秒おきに自動更新。`}
      />

      {/* ====== 研究中シグナル: プレレジ検証 + 市場一致マトリクス (地方ページのみ・全 venue の
           グローバル研究台帳なのでフィルタしない) ====== */}
      {signalRules && <SignalRulesCard data={signalRules} />}
      {marketAgree && <MarketAgreementCard data={marketAgree} />}

      {/* ====== v3 (現行・仮指数アンカー) を上、v2 / v1 (旧) を下 ====== */}
      <VersionMeasurementSection version="v3" box={boxV3} strategies={stratV3} nowMs={nowMs} />
      <VersionMeasurementSection version="v2" box={boxV2} strategies={stratV2} nowMs={nowMs} />
      <VersionMeasurementSection version="v1" box={boxV1} strategies={stratV1} nowMs={nowMs} />
    </Page>
  );
}
