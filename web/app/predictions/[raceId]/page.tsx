import { notFound } from "next/navigation";
import Link from "next/link";
import {
  api,
  type BetEvRow,
  type BundleLeg,
  type HorseAptitude,
  type HorseBestTime,
  type MarketSignal,
  type PredictionDetail,
  type PredictionRow,
  type RecommendedBundle,
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

      <TopRecommendationCard d={d} finish={finish} />

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

      {(() => {
        // Plan F と同じ pattern: evidence_plan_*_keys があればそれを優先、無ければ raw plan_*_keys。
        // Plan F だけ evidence-aware だった旧仕様を A/B/C/G/H1/H2 にも揃える。
        const hasEv = !!d.evidence_rows;
        const rowsToUse = hasEv ? d.evidence_rows! : d.rows;
        const aKeys = d.evidence_plan_a_keys ?? d.plan_a_keys;
        const bKeys = d.evidence_plan_b_keys ?? d.plan_b_keys;
        const cKeys = d.evidence_plan_c_keys ?? d.plan_c_keys;
        const gKeys = d.evidence_plan_g_keys ?? d.plan_g_keys;
        const h1Keys = d.evidence_plan_h1_keys ?? d.plan_h1_keys;
        const h2Keys = d.evidence_plan_h2_keys ?? d.plan_h2_keys;
        const evSuffix = hasEv ? " (LLM 補強反映)" : "";
        return (
          <>
            <PlansCard plan="A" subtitle={`EV 枠 / 5点バランス${evSuffix}`} keys={aKeys} rows={rowsToUse} finish={finish} />
            <PlansCard plan="B" subtitle={`EV 枠 / 最高 EV 集中${evSuffix}`} keys={bKeys} rows={rowsToUse} finish={finish} />
            <PlansCard plan="C" subtitle={`EV 枠 / 広め 保険${evSuffix}`} keys={cKeys} rows={rowsToUse} finish={finish} />
            {gKeys && gKeys.length > 0 && (
              <PlansCard
                plan="G"
                subtitle={`適性ゲート (top ${d.aptitude_top_horses?.length ?? "N"} 頭 → P×O≥1.02) / EV は最終フィルタ${evSuffix}`}
                keys={gKeys}
                rows={rowsToUse}
                finish={finish}
              />
            )}
            {h1Keys && h1Keys.length > 0 && (
              <PlansCard plan="H1" subtitle={`当て枠 / 確率最優先${evSuffix}`} keys={h1Keys} rows={rowsToUse} finish={finish} />
            )}
            {h2Keys && h2Keys.length > 0 && (
              <PlansCard plan="H2" subtitle={`当て枠 / 確率優先 + P×O ≥ 1.0${evSuffix}`} keys={h2Keys} rows={rowsToUse} finish={finish} />
            )}
          </>
        );
      })()}

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

// ===== Claude 総合オススメ (全 bet type 横断 / Kelly 効率順) =====
// CLAUDE.md の方針に沿う「効率」= Kelly 資金成長率 f* = (P×O − 1) / (O − 1)。
// 純 P×O 順だと高オッズの 3 連単 (楽観バイアスの罠) が上位を占めるが、Kelly は
// 低オッズで堅い +EV (単勝/複勝/ワイド) を上位に、宝くじ型を下位に並べる。
const TOP_REC_BUDGET = 10_000; // 予算 ¥10,000 (CLAUDE.md)
const TOP_REC_PXO_FLOOR = 1.02; // Plan 入りフロアと同値

const BET_TYPE_TONE: Record<string, BadgeTone> = {
  win: "good",
  place: "good",
  quinella: "info",
  wide: "info",
  exacta: "warn",
  trio: "warn",
  trifecta: "magenta",
};

function betLabel(bt: string): string {
  return bt === "trifecta" ? "3連単" : (BET_TYPE_JP[bt] ?? bt);
}

type EffCandidate = {
  betType: string;
  key: number[];
  prob: number;
  odds: number;
  pxo: number;
  tier: string;
  kelly: number;
  hit: boolean;
};

// bet type ごとの finish 的中判定 (既存の placeHits/wideHits/finishKeyForBetType を流用)。
function betHits(betType: string, key: number[], finish?: number[]): boolean {
  if (!finish) return false;
  if (betType === "place") return placeHits(key, finish);
  if (betType === "wide") return wideHits(key, finish);
  if (betType === "trifecta") {
    if (finish.length < 3) return false;
    return key.join("-") === finish.slice(0, 3).join("-");
  }
  const fk = finishKeyForBetType(betType, finish);
  return fk != null && fk === key.join("-");
}

// 1/4 Kelly を ¥100 単位に丸めた配分目安。
function quarterKelly(kelly: number): number {
  return Math.round((TOP_REC_BUDGET * kelly) / 4 / 100) * 100;
}

function collectEfficientCandidates(d: PredictionDetail, finish?: number[]): EffCandidate[] {
  const out: EffCandidate[] = [];
  const seen = new Set<string>();
  const push = (
    betType: string,
    key: number[],
    prob: number,
    odds: number,
    pxo: number,
    tier: string,
  ) => {
    if (!(odds > 1) || pxo < TOP_REC_PXO_FLOOR) return;
    const kelly = (pxo - 1) / (odds - 1);
    if (kelly <= 0) return;
    const id = `${betType}:${key.join("-")}`;
    if (seen.has(id)) return;
    seen.add(id);
    out.push({ betType, key, prob, odds, pxo, tier, kelly, hit: betHits(betType, key, finish) });
  };
  // 3 連単: LLM 補強後があれば優先
  const triRows = d.evidence_rows ?? d.rows;
  for (const r of triRows) push("trifecta", r.key, r.prob, r.odds, r.px_o, r.tier);
  // その他 bet type (単勝/複勝/馬連/ワイド/馬単/3 連複)
  for (const [bt, rows] of Object.entries(d.bet_tables ?? {})) {
    for (const r of rows) push(bt, r.key, r.prob, r.odds, r.px_o, r.tier);
  }
  out.sort((a, b) => b.kelly - a.kelly);
  return out;
}

function TopRecTable({ cands }: { cands: EffCandidate[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabnum table-zebra">
        <thead className="text-left text-(--color-muted) text-xs">
          <tr className="border-b border-(--color-line)">
            <th className="py-2 pr-3 text-right">#</th>
            <th className="py-2 pr-3">種別</th>
            <th className="py-2 pr-3">買い目</th>
            <th className="py-2 pr-3 text-right">推定 P</th>
            <th className="py-2 pr-3 text-right">オッズ</th>
            <th className="py-2 pr-3 text-right">P×O</th>
            <th className="py-2 pr-3 text-right">Kelly</th>
            <th className="py-2 pr-3 text-right">¼K 配分</th>
            <th className="py-2 pr-3">帯</th>
          </tr>
        </thead>
        <tbody>
          {cands.map((c, i) => {
            const k = c.key.join("-");
            const qk = quarterKelly(c.kelly);
            return (
              <tr
                key={`${c.betType}:${k}`}
                className={`border-b border-(--color-line)/60 ${c.hit ? "bg-emerald-500/5" : ""}`}
              >
                <td className="py-1.5 pr-3 text-right text-(--color-muted)">{i + 1}</td>
                <td className="py-1.5 pr-3">
                  <Badge tone={BET_TYPE_TONE[c.betType] ?? "muted"}>{betLabel(c.betType)}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {c.hit && <span className="ml-2 text-(--color-good)">●</span>}
                </td>
                <td className="py-1.5 pr-3 text-right">{fmtPct(c.prob, 2)}</td>
                <td className="py-1.5 pr-3 text-right">{c.odds.toFixed(1)}</td>
                <td className="py-1.5 pr-3 text-right">
                  <Badge tone={pxoTone(c.pxo)}>{c.pxo.toFixed(2)}</Badge>
                </td>
                <td className="py-1.5 pr-3 text-right">{(c.kelly * 100).toFixed(1)}%</td>
                <td className="py-1.5 pr-3 text-right text-(--color-muted)">
                  {qk >= 100 ? `¥${qk.toLocaleString()}` : "—"}
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={tierTone(c.tier)}>{tierLabel(c.tier)}</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TopRecommendationCard({
  d,
  finish,
}: {
  d: PredictionDetail;
  finish?: number[];
}) {
  // 厳密版 (backend joint Kelly) があればそれを描画。無い古い snapshot は
  // frontend 近似 Kelly ランキングに fallback。
  if (d.recommended_bundle) {
    return <BundleCard bundle={d.recommended_bundle} finish={finish} />;
  }
  const cands = collectEfficientCandidates(d, finish);
  const top = cands.slice(0, 8);
  const best = top[0];
  const usedEvidence = !!d.evidence_rows;
  return (
    <Card
      tone={best ? "alert" : "default"}
      title={
        <span className="flex items-center gap-2">
          <span className="text-(--color-highlight) font-black">Claude 総合オススメ</span>
          <span className="text-xs text-(--color-muted) font-normal">
            全 bet type 横断 · Kelly 効率順 f*=(P×O−1)/(O−1) · P×O≥
            {TOP_REC_PXO_FLOOR.toFixed(2)} で足切り · 独立サイジング(近似){usedEvidence ? " · LLM 補強反映" : ""}
          </span>
        </span>
      }
    >
      {top.length === 0 ? (
        <p className="text-sm text-(--color-muted)">
          +EV (P×O≥{TOP_REC_PXO_FLOOR.toFixed(2)}) の効率的な買い目なし。
          <span className="font-bold text-(--color-foreground)">見送り推奨</span>。
        </p>
      ) : (
        <>
          {best && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
              <Stat
                label="本命 (最効率)"
                value={
                  <span className="flex items-center gap-1.5">
                    <Badge tone={BET_TYPE_TONE[best.betType] ?? "muted"}>
                      {betLabel(best.betType)}
                    </Badge>
                    <span className="mono font-bold">{best.key.join("-")}</span>
                  </span>
                }
              />
              <Stat label="推定的中率" value={fmtPct(best.prob, 2)} />
              <Stat label="オッズ" value={best.odds.toFixed(1)} />
              <Stat label="EV (P×O)" value={best.pxo.toFixed(2)} />
              <Stat
                label="Kelly → ¼K 配分"
                value={
                  <span>
                    {(best.kelly * 100).toFixed(1)}%
                    <span className="text-(--color-muted) text-xs ml-1">
                      / {quarterKelly(best.kelly) >= 100 ? `¥${quarterKelly(best.kelly).toLocaleString()}` : "<¥100"}
                    </span>
                  </span>
                }
              />
            </div>
          )}
          <TopRecTable cands={top} />
          <p className="mt-3 text-xs text-(--color-muted)">
            効率 = Kelly 資金成長率 f*。低オッズで堅い +EV ほど上位、高オッズの 3 連単は
            EV が高く見えても f* が小さく下位になる (CLAUDE.md: robust に +EV と確認できたのは
            単勝 β=0.78 のみ)。¼K = ¥{TOP_REC_BUDGET.toLocaleString()} に対する 1/4 Kelly
            配分目安 (¥100 単位)。各行は独立サイジングでありポートフォリオ合算ではない。
          </p>
        </>
      )}
    </Card>
  );
}

function BundleLegsTable({ legs, finish }: { legs: BundleLeg[]; finish?: number[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabnum table-zebra">
        <thead className="text-left text-(--color-muted) text-xs">
          <tr className="border-b border-(--color-line)">
            <th className="py-2 pr-3">種別</th>
            <th className="py-2 pr-3">買い目</th>
            <th className="py-2 pr-3 text-right">オッズ</th>
            <th className="py-2 pr-3 text-right">推定 P</th>
            <th className="py-2 pr-3 text-right">P×O</th>
            <th className="py-2 pr-3 text-right">Kelly</th>
            <th className="py-2 pr-3 text-right">配分</th>
            <th className="py-2 pr-3 text-right">的中時 払戻</th>
            <th className="py-2 pr-3">帯</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l) => {
            const k = l.key.join("-");
            const hit = betHits(l.bet_type, l.key, finish);
            return (
              <tr
                key={`${l.bet_type}:${k}`}
                className={`border-b border-(--color-line)/60 ${hit ? "bg-emerald-500/5" : ""}`}
              >
                <td className="py-1.5 pr-3">
                  <Badge tone={BET_TYPE_TONE[l.bet_type] ?? "muted"}>{betLabel(l.bet_type)}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {hit && <span className="ml-2 text-(--color-good)">●</span>}
                </td>
                <td className="py-1.5 pr-3 text-right">{l.odds.toFixed(1)}</td>
                <td className="py-1.5 pr-3 text-right">{fmtPct(l.prob, 2)}</td>
                <td className="py-1.5 pr-3 text-right">
                  <Badge tone={pxoTone(l.px_o)}>{l.px_o.toFixed(2)}</Badge>
                </td>
                <td className="py-1.5 pr-3 text-right">{(l.kelly * 100).toFixed(1)}%</td>
                <td className="py-1.5 pr-3 text-right font-bold">¥{l.stake.toLocaleString()}</td>
                <td className="py-1.5 pr-3 text-right text-(--color-good)">¥{l.payout_if_hit.toLocaleString()}</td>
                <td className="py-1.5 pr-3">
                  <Badge tone={tierTone(l.tier)}>{tierLabel(l.tier)}</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// 厳密版「総合オススメ」: backend で完全 top-3 分布上に joint Kelly 最適化した
// まとめ買い束 (相関考慮・独立 Kelly の単純和ではない)。
function BundleCard({
  bundle,
  finish,
}: {
  bundle: RecommendedBundle;
  finish?: number[];
}) {
  const legs = bundle.legs ?? [];
  const half = Math.round(bundle.total_stake / 2 / 100) * 100;
  const nTypes = new Set(legs.map((l) => l.bet_type)).size;
  const bundleHit = finish ? legs.some((l) => betHits(l.bet_type, l.key, finish)) : false;
  // 束はまずモデル (portfolio.build_bundle) のみで生成され、その後 claude -p の web 調査で
  // 検証される。検証が済むまでは「Claude のオススメ」ではなく「モデル提案」として扱い、
  // Claude branding を付けない (検証前に Claude が裏取りしたかのような誤認を防ぐ)。
  const validated = bundle.llm_review?.validated === true;
  // トリガミ防止マージン (保存オッズからの下振れ緩衝)。古い snapshot は未保存 → 1 扱い。
  const margin = bundle.torigami_margin ?? 1;
  const driftPct = margin > 1 ? Math.round((1 - 1 / margin) * 100) : 0;
  return (
    <Card
      tone={legs.length ? "alert" : "default"}
      title={
        <span className="flex items-center gap-2">
          <span className="text-(--color-highlight) font-black">
            {validated ? "Claude 総合オススメ" : "総合オススメ (モデル)"} — まとめ買い
          </span>
          <span className="text-xs text-(--color-muted) font-normal">
            全 bet type 横断 · joint (同時) Kelly 最適配分
            {legs.length > 0 && ` · 完全 top-3 分布 ${bundle.n_outcomes} 通りで E[log 資金] 最大化`}
          </span>
          {validated ? (
            <Badge tone="magenta">claude -p 検証済{bundle.llm_review?.confidence ? ` (${bundle.llm_review.confidence})` : ""}</Badge>
          ) : (
            legs.length > 0 && <Badge tone="warn">Claude 検証前 (モデルのみ)</Badge>
          )}
          {finish && legs.length > 0 &&
            (bundleHit ? <Badge tone="good">束 的中</Badge> : <Badge tone="bad">束 不的中</Badge>)}
        </span>
      }
    >
      {legs.length === 0 ? (
        <p className="text-sm text-(--color-muted)">
          +EV (P×O≥{bundle.pxo_floor.toFixed(2)}) のまとめ買いなし。
          <span className="font-bold text-(--color-foreground)">見送り推奨</span> (市場効率的)。
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <Stat
              label="まとめ買い総額 (full Kelly)"
              value={
                <span>
                  ¥{bundle.total_stake.toLocaleString()}
                  <span className="text-(--color-muted) text-xs ml-1">
                    / ¥{bundle.bankroll.toLocaleString()} ({(bundle.total_fraction * 100).toFixed(0)}%)
                  </span>
                </span>
              }
            />
            <Stat label="½ Kelly (保守・推奨)" value={`¥${half.toLocaleString()}`} />
            <Stat label="束の的中率 (1点以上)" value={fmtPct(bundle.bundle_hit_prob, 1)} />
            <Stat
              label={margin > 1 ? `最小 払戻/投資 (目標 ≥×${margin.toFixed(2)})` : "最小 払戻/投資 (トリガミ無)"}
              value={
                bundle.min_payout_ratio != null ? (
                  <span
                    className={
                      bundle.min_payout_ratio >= margin - 1e-9
                        ? "text-(--color-good)"
                        : bundle.min_payout_ratio >= 1
                          ? "text-(--color-warn)"
                          : "text-(--color-bad)"
                    }
                  >
                    ×{bundle.min_payout_ratio.toFixed(2)}
                  </span>
                ) : "—"
              }
            />
          </div>
          <BundleLegsTable legs={legs} finish={finish} />
          {bundle.llm_review?.validated && (bundle.llm_review.summary || (bundle.llm_review.cuts?.length ?? 0) > 0) && (
            <div className="mt-3 rounded-md border border-(--color-magenta)/30 bg-(--color-magenta)/5 p-3 text-xs">
              <span className="font-bold text-(--color-magenta)">claude -p 調査:</span>{" "}
              {bundle.llm_review.summary || "—"}
              {(bundle.llm_review.cuts?.length ?? 0) > 0 && (
                <span className="ml-1 text-(--color-bad)">
                  / cut {bundle.llm_review.cuts!.length} 脚: {bundle.llm_review.cuts!.join(", ")}
                </span>
              )}
            </div>
          )}
          <p className="mt-3 text-xs text-(--color-muted)">
            {legs.length} 点 · {nTypes} 種。束全体で{" "}
            <span className="font-bold text-(--color-foreground)">
              E[log(資金)]={bundle.expected_log_growth.toFixed(3)}
            </span>{" "}
            を最大化した成長率最適配分 (相関・排他性を考慮、独立 Kelly の単純和ではない)。
            <span className="text-(--color-good)">
              {" "}トリガミ防止済: 各脚の的中時払戻 ≥ 投資総額 ¥{bundle.total_stake.toLocaleString()}
              {margin > 1 ? ` ×${margin.toFixed(2)} (実オッズ ${driftPct}% 下振れまで吸収)` : ""}
              {bundle.dropped_torigami ? ` / トリガミ脚 ${bundle.dropped_torigami} 本を除外` : ""}
            </span>
            。モデル期待回収 ×{bundle.expected_return.toFixed(2)}{" "}
            <span className="text-(--color-warn)">(確率モデルの楽観バイアス込み・参考値)</span>。
            full Kelly は攻め過ぎになりやすく、確率推定が楽観な本モデルでは
            <span className="font-bold"> ½ Kelly (各 stake 半額)</span> が実運用の推奨。
            候補 {bundle.n_candidates} 点から選択。
          </p>
        </>
      )}
    </Card>
  );
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
            各馬の相対適性 0-100 (同レース内 max=100) · 総合 = 9 因子重み付け平均
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
  // key.length ガード必須: 空 key だと [].every(...) が true を返し誤的中になる
  // (backend _bet_hits は len(key)==2 を強制。frontend も揃える)。
  if (!finish || finish.length < 3 || key.length !== 2) return false;
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
