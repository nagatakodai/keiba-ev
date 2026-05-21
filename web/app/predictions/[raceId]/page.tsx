import { notFound } from "next/navigation";
import Link from "next/link";
import { api, type PredictionDetail, type PredictionRow } from "@/lib/api";
import {
  Badge,
  type BadgeTone,
  Card,
  Page,
  PageHeader,
  Stat,
  fmtKey,
  fmtPct,
  fmtServerDateTime,
  fmtTime,
  fmtTs,
  planAccentClass,
  planTone,
  type PlanLetter,
  pxoTone,
  tierLabel,
  tierTone,
} from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function PredictionDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ raceId: string }>;
  searchParams: Promise<{ url?: string }>;
}) {
  const { raceId } = await params;
  const sp = await searchParams;
  const winticketUrl = sp.url;
  let d: PredictionDetail;
  try {
    d = await api.getPrediction(raceId);
  } catch {
    notFound();
  }

  const topByPxo = [...d.rows].sort((a, b) => b.px_o - a.px_o).slice(0, 30);
  const topByProb = [...d.rows]
    .sort((a, b) => b.prob - a.prob)
    .slice(0, 20);

  const finish = d.result?.finish_order;

  return (
    <Page>
      <PageHeader
        title={
          <span className="flex items-center gap-3">
            {d.venue_name} {d.race_number}R
            <Badge tone="muted">{d.race_class}</Badge>
            {d.evidence && <Badge tone="magenta">補強済</Badge>}
            {d.result && <Badge tone="good">結果あり</Badge>}
          </span>
        }
        subtitle={
          <span className="tabnum">
            race_id <span className="mono">{d.race_id}</span>
            {(d.close_at != null || d.start_at != null) && (
              <>
                {" · 締切 "}<span className="mono">{fmtTime(d.close_at)}</span>
                {" → 発走 "}<span className="mono">{fmtTime(d.start_at)}</span>
              </>
            )}
            {" · 保存 "}{fmtServerDateTime(d.saved_at)}
            {" · オッズ更新 "}{fmtTs(d.odds_updated_at)}
          </span>
        }
        right={
          <div className="flex flex-col items-end gap-1">
            {winticketUrl && (
              <a
                href={winticketUrl}
                target="_blank"
                rel="noreferrer"
                className="text-xs font-bold text-(--color-highlight) hover:underline"
              >
                netkeiba で開く ↗
              </a>
            )}
            <Link
              href="/predictions"
              className="text-xs text-(--color-accent) hover:underline"
            >
              ← 一覧
            </Link>
          </div>
        }
      />

      {d.result && (() => {
        const hits: Array<{ plan: PlanLetter; keys: number[][] | undefined }> = [
          { plan: "F", keys: d.plan_f_keys },
          { plan: "A", keys: d.plan_a_keys },
          { plan: "B", keys: d.plan_b_keys },
          { plan: "C", keys: d.plan_c_keys },
          { plan: "H1", keys: d.plan_h1_keys },
          { plan: "H2", keys: d.plan_h2_keys },
        ];
        const planHits = hits.map(({ plan, keys }) => ({
          plan,
          hit: isFinishInKeys(keys, finish),
          available: !!keys && keys.length > 0,
        }));
        const anyHit = planHits.some((h) => h.hit);
        return (
          <Card
            tone={anyHit ? "active" : "default"}
            title={
              <span className="flex items-center gap-2">
                <span>結果</span>
                {anyHit ? (
                  <Badge tone="good">的中</Badge>
                ) : (
                  <Badge tone="bad">不的中</Badge>
                )}
              </span>
            }
          >
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
              <Stat
                label="3連単 着順"
                value={<span className="mono">{finish?.join("-")}</span>}
              />
              <Stat
                label="払戻"
                value={d.result.trifecta_payout ? `¥${d.result.trifecta_payout.toLocaleString()}` : "—"}
              />
              <Stat label="ヒット tier" value={hitTier(d, finish) ?? "—"} />
            </div>
            <div className="mt-4 flex items-center gap-2 flex-wrap">
              <span className="text-xs text-(--color-muted) font-bold tracking-wider uppercase">
                Plan 別
              </span>
              {planHits.map(({ plan, hit, available }) =>
                available ? (
                  <Badge key={plan} tone={hit ? planTone(plan) : "muted"}>
                    {plan} {hit ? "✓ 的中" : "× 不的中"}
                  </Badge>
                ) : (
                  <Badge key={plan} tone="muted">
                    {plan} —
                  </Badge>
                ),
              )}
            </div>
          </Card>
        );
      })()}

      {d.evidence && d.evidence_rows && (() => {
        const topEvRows = [...d.evidence_rows]
          .sort((a, b) => b.px_o - a.px_o)
          .slice(0, 30);
        const evTotalProb = topEvRows.reduce((s, r) => s + r.prob, 0);
        const evAvgOdds = topEvRows.length
          ? topEvRows.reduce((s, r) => s + r.odds, 0) / topEvRows.length
          : 0;
        const evAvgPxo = topEvRows.length
          ? topEvRows.reduce((s, r) => s + r.px_o, 0) / topEvRows.length
          : 0;
        return (
          <Card
            tone="alert"
            title={
              <span className="flex items-center gap-2">
                <span className="text-(--color-highlight) font-black">Claude オススメ</span>
                <span className="text-xs text-(--color-muted) font-normal">検索補強を反映した最終調整</span>
              </span>
            }
          >
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
              <Stat label="点数" value={`${topEvRows.length}点`} />
              <Stat label="合計推定的中率" value={fmtPct(evTotalProb, 2)} />
              <Stat label="平均オッズ" value={evAvgOdds.toFixed(1)} />
              <Stat label="EV (平均 P×O)" value={evAvgPxo.toFixed(2)} />
            </div>
            <div className="text-xs text-(--color-muted) mb-2">
              cuts: {(d.evidence.cuts ?? []).join(", ") || "(なし)"} · evidence keys:{" "}
              {Object.keys(d.evidence.evidence_by_key ?? {}).length} 件
            </div>
            <RowsTable rows={topEvRows} finish={finish} />
          </Card>
        );
      })()}

      {(() => {
        const fKeys = d.evidence_plan_f_keys ?? d.plan_f_keys;
        if (!fKeys || fKeys.length === 0) return null;
        const useEvidence = !!d.evidence_plan_f_keys && !!d.evidence_rows;
        const fRows = useEvidence ? d.evidence_rows! : d.rows;
        const subtitle = useEvidence
          ? "最終買い目 / A〜H2 union 重複除去 (補強反映) · ¥10,000 均等振り"
          : "最終買い目 / A〜H2 union 重複除去 · ¥10,000 均等振り";
        return (
          <PlansCard plan="F" subtitle={subtitle} keys={fKeys} rows={fRows} finish={finish} />
        );
      })()}

      <PlansCard plan="A" subtitle="EV 枠 / 5点バランス" keys={d.plan_a_keys} rows={d.rows} finish={finish} />
      <PlansCard plan="B" subtitle="EV 枠 / 最高 EV 集中" keys={d.plan_b_keys} rows={d.rows} finish={finish} />
      <PlansCard plan="C" subtitle="EV 枠 / 広め 保険" keys={d.plan_c_keys} rows={d.rows} finish={finish} />
      {d.plan_h1_keys && d.plan_h1_keys.length > 0 && (
        <PlansCard plan="H1" subtitle="当て枠 / 確率最優先" keys={d.plan_h1_keys} rows={d.rows} finish={finish} />
      )}
      {d.plan_h2_keys && d.plan_h2_keys.length > 0 && (
        <PlansCard plan="H2" subtitle="当て枠 / 確率優先 + P×O ≥ 1.0" keys={d.plan_h2_keys} rows={d.rows} finish={finish} />
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="P×O ランキング 上位 30">
          <RowsTable rows={topByPxo} finish={finish} />
        </Card>
        <Card title="推定当選率ランキング 上位 20">
          <ProbRankingTable rows={topByProb} finish={finish} />
        </Card>
      </div>
    </Page>
  );
}

// 与えられた buy-key リストに finish_order が含まれているか。
function isFinishInKeys(keys: number[][] | undefined, finish?: number[]): boolean {
  if (!keys || !finish || finish.length !== 3) return false;
  const target = finish.join("-");
  return keys.some((k) => k.join("-") === target);
}

function hitTier(d: PredictionDetail, finish?: number[]): string | null {
  if (!finish || finish.length !== 3) return null;
  const target = finish.join("-");
  const r = d.rows.find((r) => fmtKey(r.key) === target);
  return r ? tierLabel(r.tier) : null;
}

function PlansCard({
  plan,
  subtitle,
  keys,
  rows,
  finish,
}: {
  plan: PlanLetter;
  subtitle: string;
  keys: number[][];
  rows: PredictionRow[];
  finish?: number[];
}) {
  const map = new Map(rows.map((r) => [fmtKey(r.key), r] as const));
  const picks = keys
    .map((k) => map.get(fmtKey(k)))
    .filter(Boolean) as PredictionRow[];
  const totalProb = picks.reduce((s, r) => s + r.prob, 0);
  const avgPxo = picks.length ? picks.reduce((s, r) => s + r.px_o, 0) / picks.length : 0;
  const finishKey = finish ? finish.join("-") : null;

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span className={`text-base font-black ${planAccentClass(plan)}`}>Plan {plan}</span>
          <span className="text-xs text-(--color-muted) font-normal">{subtitle}</span>
          <span className="text-xs text-(--color-muted) tabnum">
            · {picks.length} 点 · 的中率 {fmtPct(totalProb, 2)} · 平均 P×O {avgPxo.toFixed(2)}
          </span>
        </span>
      }
    >
      {picks.length === 0 ? (
        <p className="text-sm text-(--color-muted)">対象なし。スキップ推奨。</p>
      ) : (
        <RowsTable rows={picks} finish={finish} highlight={finishKey} />
      )}
    </Card>
  );
}

// 推定 P × 市場率 を並べてバイアスを観察するためのランキング専用テーブル。
// EV (P×O) フィルタは外し、-EV の人気目もそのまま並ぶ前提。「評価」列で
// P×O > 1 ⇒ 過小 (買い候補) / ≒ 1 ⇒ 適正 / < 1 ⇒ 過大 を明示する。
function pxoEvaluation(pxo: number): { label: string; tone: BadgeTone } {
  if (pxo >= 1.05) return { label: `過小 ×${pxo.toFixed(2)}`, tone: "good" };
  if (pxo >= 0.95) return { label: "適正", tone: "default" };
  if (pxo > 0) return { label: `過大 ×${(1 / pxo).toFixed(2)}`, tone: "bad" };
  return { label: "—", tone: "muted" };
}

function ProbRankingTable({
  rows,
  finish,
}: {
  rows: PredictionRow[];
  finish?: number[];
}) {
  const finishKey = finish ? finish.join("-") : null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabnum table-zebra">
        <thead className="text-left text-(--color-muted) text-xs">
          <tr className="border-b border-(--color-line)">
            <th className="py-2 pr-3 text-right">#</th>
            <th className="py-2 pr-3">買い目</th>
            <th className="py-2 pr-3 text-right">推定 P</th>
            <th className="py-2 pr-3 text-right">市場率</th>
            <th className="py-2 pr-3 text-right">オッズ</th>
            <th className="py-2 pr-3 text-right">人気</th>
            <th className="py-2 pr-3 text-right">P×O</th>
            <th className="py-2 pr-3">評価</th>
            <th className="py-2 pr-3">帯</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const k = fmtKey(r.key);
            const hit = finishKey === k;
            const marketRate = r.odds > 0 ? 100 / r.odds : 0;
            const evaluation = pxoEvaluation(r.px_o);
            return (
              <tr
                key={k}
                className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}
              >
                <td className="py-1.5 pr-3 text-right text-(--color-muted)">{i + 1}</td>
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {hit && <span className="ml-2 text-(--color-good)">●</span>}
                </td>
                <td className="py-1.5 pr-3 text-right">{fmtPct(r.prob, 2)}</td>
                <td className="py-1.5 pr-3 text-right text-(--color-muted)">
                  {marketRate.toFixed(2)}%
                </td>
                <td className="py-1.5 pr-3 text-right">{r.odds.toFixed(1)}</td>
                <td className="py-1.5 pr-3 text-right">{r.popularity}</td>
                <td className="py-1.5 pr-3 text-right">
                  <Badge tone={pxoTone(r.px_o)}>{r.px_o.toFixed(2)}</Badge>
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={evaluation.tone}>{evaluation.label}</Badge>
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={tierTone(r.tier)}>{tierLabel(r.tier)}</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RowsTable({
  rows,
  finish,
  highlight,
}: {
  rows: PredictionRow[];
  finish?: number[];
  highlight?: string | null;
}) {
  const finishKey = highlight ?? (finish ? finish.join("-") : null);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabnum table-zebra">
        <thead className="text-left text-(--color-muted) text-xs">
          <tr className="border-b border-(--color-line)">
            <th className="py-2 pr-3">買い目</th>
            <th className="py-2 pr-3 text-right">人気</th>
            <th className="py-2 pr-3 text-right">オッズ</th>
            <th className="py-2 pr-3 text-right">推定 P</th>
            <th className="py-2 pr-3 text-right">P×O</th>
            <th className="py-2 pr-3">帯</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const k = fmtKey(r.key);
            const hit = finishKey === k;
            return (
              <tr
                key={k}
                className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}
              >
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {hit && <span className="ml-2 text-(--color-good)">●</span>}
                </td>
                <td className="py-1.5 pr-3 text-right">{r.popularity}</td>
                <td className="py-1.5 pr-3 text-right">{r.odds.toFixed(1)}</td>
                <td className="py-1.5 pr-3 text-right">{fmtPct(r.prob, 2)}</td>
                <td className="py-1.5 pr-3 text-right">
                  <Badge tone={pxoTone(r.px_o)}>{r.px_o.toFixed(2)}</Badge>
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={tierTone(r.tier)}>{tierLabel(r.tier)}</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
