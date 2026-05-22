import { notFound } from "next/navigation";
import Link from "next/link";
import {
  api,
  type BetEvRow,
  type HorseAptitude,
  type HorseBestTime,
  type MarketSignal,
  type PredictionDetail,
  type PredictionRow,
} from "@/lib/api";
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
            {d.model_info?.engine === "lgbm" ? (
              <span title={`LightGBM ${d.model_info.n_features ?? "?"} features, trained ${d.model_info.trained_at ?? "?"}`}>
                <Badge tone="info">LGBM</Badge>
              </span>
            ) : d.model_info?.engine === "linear-fallback" ? (
              <span title="LightGBM 学習済モデル未利用 - 線形 softmax fallback">
                <Badge tone="warn">linear</Badge>
              </span>
            ) : null}
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
          { plan: "G", keys: d.plan_g_keys },
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

      {d.horse_aptitude && d.horse_aptitude.length > 0 && (
        <AptitudeCard items={d.horse_aptitude} finish={finish} />
      )}

      {d.market_signals && d.market_signals.length > 0 && (
        <MarketSignalCard items={d.market_signals} finish={finish} />
      )}

      {d.horse_best_times && d.horse_best_times.length > 0 && (
        <BestTimesCard items={d.horse_best_times} finish={finish} />
      )}

      {d.bet_tables && Object.keys(d.bet_tables).length > 0 && (
        <BetTablesCard
          tables={d.bet_tables}
          tablesG={d.bet_tables_g}
          finish={finish}
        />
      )}

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
      {d.plan_g_keys && d.plan_g_keys.length > 0 && (
        <PlansCard
          plan="G"
          subtitle={`適性ゲート (top ${d.aptitude_top_horses?.length ?? "N"} 頭 → P×O≥1.02) / EV は最終フィルタ`}
          keys={d.plan_g_keys}
          rows={d.rows}
          finish={finish}
        />
      )}
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

function totalTone(total: number): BadgeTone {
  if (total >= 75) return "good";
  if (total >= 60) return "warn";
  if (total >= 45) return "default";
  return "muted";
}

// 因子値 (0-100) を 0-5 段階の濃淡セルで表示。0 は dim。
function factorCellClass(v: number): string {
  if (v <= 0) return "text-(--color-muted)";
  if (v >= 85) return "text-(--color-good) font-bold";
  if (v >= 70) return "text-(--color-good)";
  if (v >= 50) return "";
  return "text-(--color-muted)";
}

function AptitudeCard({
  items,
  finish,
}: {
  items: HorseAptitude[];
  finish?: number[];
}) {
  const finishSet = new Set(finish ?? []);
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span>適性指数</span>
          <span className="text-xs text-(--color-muted) font-normal">
            各馬の相対適性 0-100 (同レース内 max=100) · 総合 = 8 因子重み付け平均
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabnum table-zebra">
          <thead className="text-left text-(--color-muted) text-xs">
            <tr className="border-b border-(--color-line)">
              <th className="py-2 pr-3 text-right">馬</th>
              <th className="py-2 pr-3">馬名</th>
              <th className="py-2 pr-3 text-right">総合</th>
              <th className="py-2 pr-2 text-right" title="speed_idx 重み付け">能力</th>
              <th className="py-2 pr-2 text-right" title="距離 × surface 条件付き shrinkage 勝率 + 経験">距離</th>
              <th className="py-2 pr-2 text-right" title="上がり 3F 距離標準化">末脚</th>
              <th className="py-2 pr-2 text-right" title="同 surface + 当該場経験 + show率">コース</th>
              <th className="py-2 pr-2 text-right" title="現馬場状態 (良/稍/重/不) での好走率 / 経験無しなら馬場多様性">馬場</th>
              <th className="py-2 pr-2 text-right" title="間隔 + 馬体重変動">状態</th>
              <th className="py-2 pr-2 text-right" title="騎手継続 / 乗替り">騎手</th>
              <th className="py-2 pr-2 text-right" title="脚質×想定ペース">ペース</th>
              <th className="py-2 pr-2 text-right" title="G1=10 / G2=5 / G3=3 / L=2 / OP=1 × finish 倍率">重賞</th>
              <th className="py-2 pr-3">主要根拠</th>
            </tr>
          </thead>
          <tbody>
            {items.map((a) => {
              const hit = finishSet.has(a.number);
              return (
                <tr
                  key={a.number}
                  className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}
                >
                  <td className="py-1.5 pr-3 text-right font-bold">{a.number}</td>
                  <td className="py-1.5 pr-3">
                    {a.name}
                    {hit && finish && (
                      <span className="ml-2 text-(--color-good) text-xs">
                        ●{finish.indexOf(a.number) + 1}着
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    <Badge tone={totalTone(a.total)}>{a.total.toFixed(1)}</Badge>
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.ability)}`}>
                    {a.ability.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.distance_fit)}`}>
                    {a.distance_fit.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.last3f)}`}>
                    {a.last3f.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.surface_fit)}`}>
                    {a.surface_fit.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.going_fit ?? 0)}`}>
                    {a.going_fit != null ? a.going_fit.toFixed(0) : "—"}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.condition)}`}>
                    {a.condition.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.jockey_fit)}`}>
                    {a.jockey_fit.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.pace_fit)}`}>
                    {a.pace_fit.toFixed(0)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${factorCellClass(a.graded_record)}`}>
                    {a.graded_record > 0 ? a.graded_record.toFixed(0) : "—"}
                  </td>
                  <td className="py-1.5 pr-3">
                    <div className="flex flex-wrap gap-1">
                      {a.reasons.length === 0 ? (
                        <span className="text-(--color-muted) text-xs">—</span>
                      ) : (
                        a.reasons.map((r) => (
                          <Badge key={r} tone="muted">{r}</Badge>
                        ))
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-(--color-muted)">
        注: 検索 MCP の補強根拠は含まない事前指数。LLM 評価後の補正は別。
      </p>
    </Card>
  );
}

const BET_TYPE_JP: Record<string, string> = {
  win: "単勝",
  place: "複勝",
  quinella: "馬連",
  wide: "ワイド",
  exacta: "馬単",
  trio: "3連複",
};

// finish 着順から該当 bet type のキー (馬連/馬単/3連複/単勝) を生成。
// 単勝は 1着馬、馬連/ワイドは top2 (順不同)、馬単は top2 (順序あり)、3連複は top3 (順不同)。
// 複勝・ワイドは any-match なので null を返し、対応する hits 関数を別途使う。
function finishKeyForBetType(bt: string, finish?: number[]): string | null {
  if (!finish || finish.length < 1) return null;
  if (bt === "win") return String(finish[0]);
  if (finish.length < 2) return null;
  if (bt === "exacta") return finish.slice(0, 2).join("-");
  if (bt === "quinella") {
    const [a, b] = [...finish.slice(0, 2)].sort((x, y) => x - y);
    return `${a}-${b}`;
  }
  if (bt === "place" || bt === "wide") {
    // 単一/2馬の「top3 含み」判定は finishKeyForBetType の役割外。
    // BetTable 側で placeHits / wideHits を使って判定する。
    return null;
  }
  if (bt === "trio") {
    if (finish.length < 3) return null;
    const [a, b, c] = [...finish.slice(0, 3)].sort((x, y) => x - y);
    return `${a}-${b}-${c}`;
  }
  return null;
}

// 複勝専用: 「該当 key (1 馬) が finish の top3 に入っているか」を判定。
function placeHits(key: number[], finish?: number[]): boolean {
  if (!finish || finish.length < 3 || key.length !== 1) return false;
  return finish.slice(0, 3).includes(key[0]);
}

// ワイド専用: 「該当 key (2 馬) が finish の top3 のうち 2 頭を含むか」を判定。
function wideHits(key: number[], finish?: number[]): boolean {
  if (!finish || finish.length < 3) return false;
  const top3 = new Set(finish.slice(0, 3));
  return key.every((k) => top3.has(k));
}

function BetTable({
  bt,
  rows,
  rowsG,
  finish,
}: {
  bt: string;
  rows: BetEvRow[];
  rowsG?: BetEvRow[];
  finish?: number[];
}) {
  if (rows.length === 0) return null;
  const finishKey = finishKeyForBetType(bt, finish);
  const top = rows.slice(0, 10);
  const totalProb = top.reduce((s, r) => s + r.prob, 0);
  const avgPxo = top.reduce((s, r) => s + r.px_o, 0) / top.length;
  const gKeySet = new Set((rowsG ?? []).map((r) => r.key.join("-")));
  return (
    <div className="mb-4">
      <div className="text-xs text-(--color-muted) mb-1 tabnum">
        <span className="font-bold text-(--color-foreground) mr-2">
          {BET_TYPE_JP[bt] ?? bt}
        </span>
        top10 合計推定的中率 {fmtPct(totalProb, 2)} · 平均 P×O {avgPxo.toFixed(2)}
        {rowsG && rowsG.length > 0 && (
          <span className={`ml-2 font-bold ${planAccentClass("G")}`}>
            · Plan G {rowsG.length}点
          </span>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabnum table-zebra">
          <thead className="text-left text-(--color-muted) text-xs">
            <tr className="border-b border-(--color-line)">
              <th className="py-2 pr-3 text-right">#</th>
              <th className="py-2 pr-3">買い目</th>
              <th className="py-2 pr-3 text-right">人気</th>
              <th className="py-2 pr-3 text-right">オッズ</th>
              <th className="py-2 pr-3 text-right">推定 P</th>
              <th className="py-2 pr-3 text-right">P×O</th>
              <th className="py-2 pr-3">帯</th>
              <th className="py-2 pr-3">適性G</th>
            </tr>
          </thead>
          <tbody>
            {top.map((r, i) => {
              const k = r.key.join("-");
              const hit =
                bt === "wide"
                  ? wideHits(r.key, finish)
                  : bt === "place"
                    ? placeHits(r.key, finish)
                    : finishKey === k;
              const inG = gKeySet.has(k);
              return (
                <tr
                  key={k}
                  className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}
                >
                  <td className="py-1.5 pr-3 text-right text-(--color-muted)">
                    {i + 1}
                  </td>
                  <td className="py-1.5 pr-3 font-medium mono">
                    {k}
                    {hit && <span className="ml-2 text-(--color-good)">●</span>}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.popularity ? r.popularity : "—"}
                  </td>
                  <td className="py-1.5 pr-3 text-right">{r.odds.toFixed(1)}</td>
                  <td className="py-1.5 pr-3 text-right">{fmtPct(r.prob, 2)}</td>
                  <td className="py-1.5 pr-3 text-right">
                    <Badge tone={pxoTone(r.px_o)}>{r.px_o.toFixed(2)}</Badge>
                  </td>
                  <td className="py-1.5 pr-3">
                    <Badge tone={tierTone(r.tier)}>{tierLabel(r.tier)}</Badge>
                  </td>
                  <td className="py-1.5 pr-3">
                    {inG ? (
                      <Badge tone={planTone("G")}>G</Badge>
                    ) : (
                      <span className="text-(--color-muted) text-xs">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BetTablesCard({
  tables,
  tablesG,
  finish,
}: {
  tables: Record<string, BetEvRow[]>;
  tablesG?: Record<string, BetEvRow[]>;
  finish?: number[];
}) {
  // bet type の表示順: リスク低 → 高 (単勝 → 複勝 → 馬連 → ワイド → 馬単 → 3 連複)
  const order = ["win", "place", "quinella", "wide", "exacta", "trio"];
  const present = order.filter((bt) => (tables[bt]?.length ?? 0) > 0);
  if (present.length === 0) return null;
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span>他の bet type 比較</span>
          <span className="text-xs text-(--color-muted) font-normal">
            3 連単以外 (単勝 / 複勝 / 馬連 / ワイド / 馬単 / 3 連複) の P×O 上位 10 · 同じ確率モデル
          </span>
        </span>
      }
    >
      {present.map((bt) => (
        <BetTable
          key={bt}
          bt={bt}
          rows={tables[bt]}
          rowsG={tablesG?.[bt]}
          finish={finish}
        />
      ))}
      <p className="text-xs text-(--color-muted) mt-2">
        注: 確率モデルは 3 連単と共通 (Plackett-Luce 連鎖)。低リスク bet type は
        控除率が低い (単複 20% / 馬連 22.5% / 3 連単 27.5%) ぶん +EV が残りやすい。
        複勝オッズは下限 (fuku_min) を採用 (実払戻が下限以上で確定する保守値)。
      </p>
    </Card>
  );
}

function MarketSignalCard({
  items,
  finish,
}: {
  items: MarketSignal[];
  finish?: number[];
}) {
  // 1着型 / 3着型 / 極端 のみ表示 (標準・不明はノイズ)
  const interesting = items.filter(
    (s) => s.interpretation === "3着型" || s.interpretation === "1着型" || s.interpretation === "極端",
  );
  if (interesting.length === 0) return null;
  const finishSet = new Set(finish ?? []);
  const order: Record<string, number> = { "3着型": 0, "1着型": 1, "極端": 2 };
  const sorted = [...interesting].sort(
    (a, b) =>
      (order[a.interpretation] ?? 9) - (order[b.interpretation] ?? 9) ||
      b.place_to_win_ratio - a.place_to_win_ratio,
  );
  const toneFor = (interp: string) => {
    if (interp === "3着型") return "magenta" as const;
    if (interp === "1着型") return "info" as const;
    return "bad" as const;
  };
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span>市場乖離 (1着型 / 3着型)</span>
          <span className="text-xs text-(--color-muted) font-normal">
            単勝 vs 複勝 implied prob 比率で構造的ミスプライスを検出
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabnum table-zebra">
          <thead className="text-left text-(--color-muted) text-xs">
            <tr className="border-b border-(--color-line)">
              <th className="py-2 pr-3 text-right">馬</th>
              <th className="py-2 pr-3">馬名</th>
              <th className="py-2 pr-3">解釈</th>
              <th className="py-2 pr-3 text-right">単勝</th>
              <th className="py-2 pr-3 text-right">複(下限)</th>
              <th className="py-2 pr-3 text-right">win%</th>
              <th className="py-2 pr-3 text-right">place%</th>
              <th className="py-2 pr-3 text-right">ratio</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => {
              const hit = finishSet.has(s.number);
              return (
                <tr key={s.number} className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}>
                  <td className="py-1.5 pr-3 text-right font-bold">{s.number}</td>
                  <td className="py-1.5 pr-3">
                    {s.name}
                    {hit && finish && (
                      <span className="ml-2 text-(--color-good) text-xs">
                        ●{finish.indexOf(s.number) + 1}着
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3">
                    <Badge tone={toneFor(s.interpretation)}>{s.interpretation}</Badge>
                  </td>
                  <td className="py-1.5 pr-3 text-right">{s.win_odds.toFixed(1)}</td>
                  <td className="py-1.5 pr-3 text-right">{s.place_odds_min.toFixed(1)}</td>
                  <td className="py-1.5 pr-3 text-right">{(s.win_implied * 100).toFixed(2)}%</td>
                  <td className="py-1.5 pr-3 text-right">{(s.place_implied * 100).toFixed(2)}%</td>
                  <td className="py-1.5 pr-3 text-right">{s.place_to_win_ratio.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-(--color-muted)">
        <span className="font-bold">3着型</span> = 市場が「3 着までは堅いが 1 着は薄い」と見る馬 (Plan G の 2/3 着スロット候補)。
        <span className="font-bold ml-2">1着型</span> = 市場が「1 着取らないと終わり」と見る馬 (Plan G の 1 着スロット候補)。
      </p>
    </Card>
  );
}

function BestTimesCard({
  items,
  finish,
}: {
  items: HorseBestTime[];
  finish?: number[];
}) {
  const finishSet = new Set(finish ?? []);
  // 速い順 (snapshot は既に best_time_sec 昇順) — top 10 まで表示
  const top = items.slice(0, 10);
  if (top.length === 0) return null;
  const fastest = top[0].best_time_sec;
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span>持ち時計</span>
          <span className="text-xs text-(--color-muted) font-normal">
            同 venue × 同距離 ±100m × 同 surface での past best own_time_sec (速い順)
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabnum table-zebra">
          <thead className="text-left text-(--color-muted) text-xs">
            <tr className="border-b border-(--color-line)">
              <th className="py-2 pr-3 text-right">馬</th>
              <th className="py-2 pr-3">馬名</th>
              <th className="py-2 pr-3 text-right">持ち時計</th>
              <th className="py-2 pr-3 text-right">トップ差</th>
              <th className="py-2 pr-3 text-right">経験</th>
            </tr>
          </thead>
          <tbody>
            {top.map((t) => {
              const hit = finishSet.has(t.number);
              const diff = t.best_time_sec - fastest;
              return (
                <tr key={t.number} className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}>
                  <td className="py-1.5 pr-3 text-right font-bold">{t.number}</td>
                  <td className="py-1.5 pr-3">
                    {t.name}
                    {hit && finish && (
                      <span className="ml-2 text-(--color-good) text-xs">
                        ●{finish.indexOf(t.number) + 1}着
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-right font-medium">{t.best_time_sec.toFixed(1)}s</td>
                  <td className="py-1.5 pr-3 text-right text-(--color-muted)">
                    {diff === 0 ? "—" : `+${diff.toFixed(1)}s`}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-(--color-muted)">{t.runs} 走</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-(--color-muted)">
        speed_idx (適性「能力」列) と独立した同条件絶対値。秒数の差が小さいほど能力拮抗。
      </p>
    </Card>
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
