import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  CircleCheck,
  Coins,
  Crosshair,
  ExternalLink,
  Gauge,
  Layers,
  ListOrdered,
  Scale,
  Sparkles,
  Target,
  Timer,
  Trophy,
} from "lucide-react";
import { TrifectaStakePreview } from "./TrifectaStakePreview";
import { OddsTimelineCard, type LateMoneySnapshot } from "./OddsTimelineCard";
import { isEvMeasured,
  api,
  type BetEvRow,
  type BundleLeg,
  type DroppedLeg,
  type HorseAptitude,
  type HorseBestTime,
  type MarketSignal,
  type PredictionDetail,
  type PredictionRow,
  type RecommendedBundle,
} from "@/lib/api";

// race_id は backend で `${cup_id}-${schedule_index}-${race_number}` 形式
// (src/analyze.py)。venue_name / race_number が空の snapshot (keibago/jra/oddspark
// 由来や旧 snapshot) でもタイトルが必ず出るよう、複数フィールドからフォールバックする。
function raceTitle(d: {
  race_id?: string | null;
  venue_name?: string | null;
  race_number?: number | null;
}): string {
  const venue = (d.venue_name ?? "").trim();
  // race_number が正の整数ならそれを採用。0/欠落なら race_id 末尾セグメントを使う。
  let raceNo: number | null =
    typeof d.race_number === "number" && d.race_number > 0
      ? d.race_number
      : null;
  if (raceNo == null && d.race_id) {
    const tail = d.race_id.split("-").pop();
    const parsed = tail != null ? Number.parseInt(tail, 10) : NaN;
    if (Number.isFinite(parsed) && parsed > 0) raceNo = parsed;
  }
  const rPart = raceNo != null ? `${raceNo}R` : "";
  const title = [venue, rPart].filter(Boolean).join(" ");
  // venue も R 番号も取れない最悪ケースは race_id をそのまま見せる (必ず非空)。
  return title || (d.race_id ?? "予測詳細");
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ raceId: string }>;
}): Promise<Metadata> {
  const { raceId } = await params;
  try {
    const d = await api.getPrediction(raceId);
    return { title: raceTitle(d) };
  } catch {
    return { title: "予測詳細" };
  }
}
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
  pxoTone,
  tierLabel,
  tierTone,
} from "@/components/ui";

export const dynamic = "force-dynamic";

// 的中マーク (旧 unicode "●" の置換)。place を渡すと "N着" を併記する。
function HitMark({ place }: { place?: number }) {
  return (
    <span className="ml-2 inline-flex items-center gap-0.5 align-middle text-emerald-300 text-xs font-bold whitespace-nowrap">
      <CircleCheck className="w-3.5 h-3.5 shrink-0" aria-hidden />
      {place != null && <span className="tnum">{place}着</span>}
    </span>
  );
}

// テーブルの的中行 tint (style guide: emerald-500/10 bg)。
const HIT_ROW_BG = "bg-emerald-500/10";

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
  // 推定当選率ランキング: 3連単 (d.rows) から推定P上位20。
  const allProbRows: RankedRow[] = d.rows.map((r) => ({ betType: "trifecta", ...r }));
  const topByProb = [...allProbRows].sort((a, b) => b.prob - a.prob).slice(0, 20);

  const finish = d.result?.finish_order;
  const nRunners = nRunnersOf(d);

  // オッズタイムライン用: 馬番 → 馬名 (horse_aptitude 優先、index_compare で補完)。
  const horseNames: Record<string, string> = {};
  for (const a of d.horse_aptitude ?? []) horseNames[String(a.number)] = a.name;
  for (const r of d.index_compare ?? []) {
    if (horseNames[String(r.number)] == null) horseNames[String(r.number)] = r.name;
  }
  // late_money は snapshot に保存されるが api.ts の型には未定義 (cast で取り出す)。
  const lateMoney =
    (d as PredictionDetail & { late_money?: LateMoneySnapshot | null }).late_money ?? null;

  return (
    <Page>
      <PageHeader
        title={
          // タイトル本文を独立 span で包む (bare text node のままだと badge 群と
          // baseline がズレる)。venue_name/race_number 欠落 snapshot でも raceTitle が
          // 必ず非空を返すのでタイトルが消えない。badge 群は items-center で揃える。
          <span className="flex items-center gap-3">
            <span>{raceTitle(d)}</span>
            {d.race_class && <Badge tone="muted">{d.race_class}</Badge>}
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
                className="inline-flex items-center gap-1 text-xs font-bold text-(--color-highlight) hover:underline"
              >
                netkeiba で開く
                <ExternalLink className="w-3 h-3 shrink-0" aria-hidden />
              </a>
            )}
            <Link
              href="/predictions"
              className="inline-flex items-center gap-1 text-xs text-(--color-accent) hover:underline"
            >
              <ArrowLeft className="w-3 h-3 shrink-0" aria-hidden />
              一覧
            </Link>
          </div>
        }
      />

      {d.index_compare && d.index_compare.length > 0 && (
        <IndexCompareCard
          items={d.index_compare}
          finish={finish}
          scoredAt={d.llm_scored_at}
          hasClaude={!d.llm_fallback}
        />
      )}

      {d.result && (() => {
        // 的中判定は**実弾投票束**基準: EV束計測対象 (saved_at >= EV_CUTOFF, 2026-06-10〜
        // 実弾既定束) は EV束、それ以前は 3連単束 (無ければ旧実弾だった EV束に fallback)。
        // dashboard / calibrate / PredictionsList と同じ規則 (乖離すると headline が矛盾する)。
        const tLegs = d.recommended_bundle_t?.legs;
        // Claude 指数ゲート: rank_source != "claude" (model 縮退) の3連単束は自動投票
        // されない (auto_watch/daemon の二重ガード) ため、headline でも「見送り」扱い。
        // api/store.py の計測規則と揃える (乖離すると dashboard と headline が矛盾する)。
        const tParticipated = d.recommended_bundle_t?.rank_source === "claude" &&
          Array.isArray(tLegs) && tLegs.length > 0;
        // 同着フォールバック (payoutTableOf): backend の _leg_hit と同規則で判定
        // (乖離すると headline が dashboard/一覧と矛盾する, 2026-06-11 第5R)。
        const pt = payoutTableOf(d.result);
        const tHit = !!(finish && tParticipated &&
          tLegs!.some((l) => betHits(l.bet_type, l.key, finish, nRunners, pt)));
        const bundleLegs = d.recommended_bundle?.legs;
        const bundleEmpty = !Array.isArray(bundleLegs) || bundleLegs.length === 0;
        const bundleHit = !!(finish && bundleLegs && bundleLegs.length > 0 &&
          bundleLegs.some((l) => betHits(l.bet_type, l.key, finish, nRunners, pt)));
        const useEv = isEvMeasured(d.saved_at);
        const useTrifecta = !useEv && tParticipated;
        const skipped = useEv ? bundleEmpty : useTrifecta ? false : bundleEmpty;
        const anyHit = useEv ? bundleHit : useTrifecta ? tHit : bundleHit;
        const headlineBadge = skipped
          ? <Badge tone="muted">見送り</Badge>
          : anyHit
            ? <Badge tone="good">的中</Badge>
            : <Badge tone="bad">不的中</Badge>;
        return (
          <Card
            tone={anyHit ? "active" : "default"}
            // タイトルは「結果」だけ。バッジは right prop に出して title 内の右ズレを回避。
            title={
              <span className="flex items-center gap-2">
                <Trophy className="w-4 h-4 text-(--color-highlight) shrink-0" aria-hidden />
                <span>結果</span>
              </span>
            }
            right={headlineBadge}
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
                Bundle 別
              </span>
              {bundleEmpty ? (
                <Badge tone="muted">EV束{useEv ? " (実弾既定)" : "(参考)"} 見送り</Badge>
              ) : (
                <Badge tone={bundleHit ? "good" : "muted"}>
                  EV束{useEv ? " (実弾既定)" : "(参考)"} {bundleHit ? "✓ 的中" : "× 不的中"}
                </Badge>
              )}
              {tParticipated ? (
                <Badge tone={tHit ? "magenta" : "muted"}>
                  3連単束{useEv ? "(参考)" : " (実弾)"} {tHit ? "✓ 的中" : "× 不的中"}
                </Badge>
              ) : (
                <Badge tone="muted">
                  3連単束 見送り
                  {d.recommended_bundle_t?.legs?.length &&
                    d.recommended_bundle_t.rank_source !== "claude"
                    ? " (Claude指数なし→投票せず)" : ""}
                </Badge>
              )}
            </div>
          </Card>
        );
      })()}

      <TopRecommendationCard d={d} finish={finish} />

      {d.horse_aptitude && d.horse_aptitude.length > 0 && (
        <AptitudeCard items={d.horse_aptitude} finish={finish} />
      )}

      {d.market_signals && d.market_signals.length > 0 && (
        <MarketSignalCard items={d.market_signals} finish={finish} />
      )}

      {/* オッズ変動タイムライン (client island): /api/timeline を fetch。未取得は控えめ表示。 */}
      <OddsTimelineCard
        raceId={d.race_id}
        horseNames={horseNames}
        finish={finish}
        lateMoney={lateMoney}
      />

      {d.horse_best_times && d.horse_best_times.length > 0 && (
        <BestTimesCard items={d.horse_best_times} finish={finish} />
      )}

      {d.bet_tables && Object.keys(d.bet_tables).length > 0 && (
        <BetTablesCard
          tables={d.bet_tables}
          tablesG={d.bet_tables_g}
          finish={finish}
          nRunners={nRunners}
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
                <Sparkles className="w-4 h-4 text-violet-300 shrink-0" aria-hidden />
                <span className="text-violet-300 font-black">Claude オススメ</span>
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

      {/* 新スキーマ (2026-05-29 後半): Plan A/B 廃止。3連単 は bet_tables[trifecta] に入り
          他券種と並ぶ。bundle 表示は TrifectaCard (実弾) + TopRecommendationCard (EV束参考) で代替。 */}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card
          title={
            <span className="flex items-center gap-2">
              <ListOrdered className="w-4 h-4 text-(--color-accent) shrink-0" aria-hidden />
              <span>P×O ランキング 上位 30</span>
            </span>
          }
        >
          <RowsTable rows={topByPxo} finish={finish} />
        </Card>
        <Card
          title={
            <span className="flex items-center gap-2">
              <Crosshair className="w-4 h-4 text-(--color-info) shrink-0" aria-hidden />
              <span>推定当選率ランキング 上位 20</span>
            </span>
          }
        >
          <ProbRankingTable rows={topByProb} finish={finish} />
        </Card>
      </div>

      {/* 3連単束はページ最下段 (2026-06-12 ユーザ指示)。2026-06-10 以降の実弾既定は EV束で、
          この束が実弾になるのは trifecta 選択時のみのため参考扱いの位置に下げた。 */}
      <TrifectaCard d={d} finish={finish} />
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

// ===== EV束 (モデル参考, 全 bet type 横断 / Kelly 効率順) =====
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

// 出走頭数 (複勝の頭数ルール用): snapshot の n_runners (権威値, 2026-06-11〜) を最優先。
// 旧 snapshot は win_probs_model → bet_tables.win → horse_aptitude の順で推定
// (api/store.py と同じ規則)。不明なら null (従来 top-3)。
function nRunnersOf(d: PredictionDetail): number | null {
  if (d.n_runners && d.n_runners > 0) return d.n_runners;
  const wpm = d.win_probs_model;
  if (wpm && Object.keys(wpm).length > 0) return Object.keys(wpm).length;
  const win = d.bet_tables?.win;
  if (win && win.length > 0) return win.length;
  if (d.horse_aptitude && d.horse_aptitude.length > 0) return d.horse_aptitude.length;
  return null;
}

// 同着 (dead heat) フォールバック用の払戻テーブル (api/store.py _leg_hit と同規則,
// 2026-06-11 第5R): netkeiba-html result の final_odds は**払戻があった組のみ**載る
// payout テーブルなので、finish_order (同着の片側しか持てない) と不一致でも
// テーブルに載っていれば実払戻あり = 的中。keibago/jra/auto の final_odds は
// 束の全脚の odds snapshot (的中と無関係に載る) なので使わない。
function payoutTableOf(
  result?: PredictionDetail["result"],
): Record<string, number> | null {
  if (!result || result.source !== "netkeiba-html") return null;
  const fo = result.final_odds;
  return fo && Object.keys(fo).length > 0 ? fo : null;
}

// bet type ごとの finish 的中判定 (既存の placeHits/wideHits/finishKeyForBetType を流用)。
// payoutTable (payoutTableOf の戻り値) を渡すと同着フォールバックも判定する。
function betHits(
  betType: string,
  key: number[],
  finish?: number[],
  nRunners?: number | null,
  payoutTable?: Record<string, number> | null,
): boolean {
  if (payoutTable && payoutTable[`${betType}:${key.join("-")}`] != null) return true;
  if (!finish) return false;
  if (betType === "place") return placeHits(key, finish, nRunners);
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
  const nR = nRunnersOf(d);
  const pt = payoutTableOf(d.result);
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
    out.push({ betType, key, prob, odds, pxo, tier, kelly, hit: betHits(betType, key, finish, nR, pt) });
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
      <table className="w-full text-sm tnum table-zebra">
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
                className={`border-b border-(--color-line)/60 ${c.hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
              >
                <td className="py-1.5 pr-3 text-right text-(--color-muted)">{i + 1}</td>
                <td className="py-1.5 pr-3">
                  <Badge tone={BET_TYPE_TONE[c.betType] ?? "muted"}>{betLabel(c.betType)}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {c.hit && <HitMark />}
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
    return (
      <BundleCard
        bundle={d.recommended_bundle}
        finish={finish}
        finalOdds={d.result?.final_odds}
        evRegime={isEvMeasured(d.saved_at)}
        nRunners={nRunnersOf(d)}
        payoutTable={payoutTableOf(d.result)}
      />
    );
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
          <Coins className="w-4 h-4 text-(--color-highlight) shrink-0" aria-hidden />
          <span className="text-(--color-highlight) font-black">EV束 (モデル参考)</span>
          <span className="text-xs text-(--color-muted) font-normal">
            旧 snapshot 近似表示 · 全 bet type 横断 · Kelly 効率順 f*=(P×O−1)/(O−1) · P×O≥
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

// 3連単的中モード (全力フォーメーション): 3連単のみ・市場無視・Claude 指数フォーメーション・トリガミ防止あり。
// recommended_bundle (EV駆動) とは別物。legs は同形なので BundleLegsTable を流用する。
function TrifectaCard({ d, finish }: { d: PredictionDetail; finish?: number[] }) {
  const b = d.recommended_bundle_t;
  if (!b || !b.legs || b.legs.length === 0) return null;
  const settled = !!finish && finish.length >= 3;
  const nR = nRunnersOf(d);
  const pt = payoutTableOf(d.result);
  const hit = !!(finish && b.legs.some((l) => betHits(l.bet_type, l.key, finish, nR, pt)));
  const osum = b.odds_summary;
  const torigamiOn = (b.dropped_torigami ?? 0) >= 0 && b.min_payout_ratio != null;
  // Claude 指数なし (model 縮退) の3連単束は自動投票されない (auto_watch/daemon が enqueue を skip)。
  const claudeMissing = b.rank_source !== "claude";
  const rankLabel = b.rank_source === "claude" ? "Claude 指数" : "モデル指数 (Claude 未実施)";
  // 締切直前に Claude が買い目自体を選定したか (build_trifecta_from_keys)。
  const claudeSelected = b.selection_source === "claude";
  // 回収モード (穴狙い): 市場1番人気を1着除外 (Claude 指数 > 90 で解禁)。古い snapshot は mode 欠落 = hit。
  const recovery = b.mode === "recovery";
  const modeTitle = recovery ? "3連単回収モード (穴狙い)" : "3連単的中モード — 全力フォーメーション";
  return (
    <Card
      tone={settled && hit ? "active" : "default"}
      title={
        <span className="flex items-center gap-2 flex-wrap">
          <Target className="w-4 h-4 text-(--color-magenta) shrink-0" aria-hidden />
          <span>{modeTitle}</span>
          <Badge tone="warn">市場無視</Badge>
          {recovery && b.excluded_head != null && (
            <Badge tone="info">1着除外: 馬{b.excluded_head} (市場1番人気)</Badge>
          )}
          {recovery && b.excluded_head == null && b.market_favorite != null && (() => {
            // 解禁理由は2系統 (src/analyze.py _recovery_exclude_head): ①鉄板帯
            // (単勝<1.5 — 指数と無関係) ②Claude 指数>90。snapshot に理由が無いので
            // 指数で判別する (旧表示は常に「指数X>90」で、鉄板帯解禁に虚偽の理由が
            // 付いていた, 2026-06-11 第5R)。
            const idx = b.favorite_claude_index;
            const byIndex = idx != null && idx > 90;
            const favOdds = d.bet_tables?.win?.find(
              (r) => r.key[0] === b.market_favorite)?.odds;
            return (
              <Badge tone="magenta">
                1番人気 馬{b.market_favorite}{" "}
                {byIndex
                  ? `指数${Math.round(idx!)}>90 で1着解禁`
                  : `鉄板帯 (単勝${favOdds != null ? ` ${favOdds.toFixed(1)}` : ""}<1.5) で1着解禁`}
              </Badge>
            );
          })()}
          {claudeSelected && <Badge tone="magenta">Claude 買い目選定</Badge>}
          {b.formation && <Badge tone="info">{b.formation} フォーメーション</Badge>}
          {claudeMissing && <Badge tone="bad">Claude指数なし→自動投票対象外</Badge>}
          {settled && <Badge tone={hit ? "good" : "bad"}>{hit ? "的中" : "不的中"}</Badge>}
        </span>
      }
    >
      <p className="text-xs text-(--color-muted) mb-3">
        市場(オッズ)をランキングに一切使わず、<b>{rankLabel}</b>の上位を本命に3連単フォーメーションを組む。
        {recovery && (
          <>
            <b>回収モード (穴狙い)</b>: 市場1番人気は <b>鉄板帯 (単勝1.5倍未満)</b> か
            Claude 指数が 90 を超えない限り<b>1着に置かない</b> (2着・3着は可)。
            市場情報はこの除外判定のみに使用。{" "}
          </>
        )}
        1着は絞り (指数の開きで {b.head_n ?? 1} 頭) ・2着は中くらい・3着は広げる。トリガミ防止あり
        (当たれば投資総額以上を回収)。理論的中率は model 基準なので過信禁物 (楽観バイアス込み)・当たらなければ
        −EV。実弾投票束は <b>watch-auto の「投票束」設定 (env KEIBA_BET_BUNDLE)</b> で切替 —
        2026-06-10 以降の既定は <b>EV束</b> で、この3連単束が実弾になるのは trifecta 選択時のみ。
        {claudeMissing && (
          <>
            {" "}<b className="text-(--color-bad)">この束は Claude 指数が無く model ランキングへ縮退しているため、3連単束の自動投票では
            送信されません</b> (Claude 指数フォーメーションが本質のため)。
          </>
        )}
        {claudeSelected && (
          <>
            {" "}<b className="text-(--color-llm)">締切直前に Claude が買い目を選定</b>
            (指数上位から自由構築・検索なし高速)。配分・トリガミ防止はモデル側。
            {b.llm_select?.summary && <> 選定根拠: {b.llm_select.summary}</>}
          </>
        )}
      </p>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Stat label="点数" value={`${b.n_points} 点`} />
        <Stat label="理論的中率 (model)" value={fmtPct(b.covered_prob, 1)} />
        {b.bankroll != null && (
          <Stat label="購入予算 (1レース)" value={`¥${b.bankroll.toLocaleString()}`} />
        )}
        <Stat label="投資総額 (予算内)" value={`¥${b.total_stake.toLocaleString()}`} />
        {osum && (
          <Stat label="当たれば払戻 (最小〜最大)"
            value={`¥${osum.min_payout.toLocaleString()}〜¥${osum.max_payout.toLocaleString()}`} />
        )}
        {b.min_payout_ratio != null && (
          <Stat label="最小 払戻/投資"
            value={<span className={b.min_payout_ratio >= (b.torigami_margin ?? 1.1) ? "text-(--color-good)" : "text-(--color-warn)"}>×{b.min_payout_ratio.toFixed(2)}</span>} />
        )}
        {osum && <Stat label="加重平均オッズ" value={`×${osum.weighted_avg_odds.toFixed(0)}`} />}
      </div>
      {(b.head_horses?.length || b.mid_horses?.length || b.tail_horses?.length) && (
        <div className="mt-3 rounded-lg bg-(--color-surface-2) border border-(--color-line) px-3 py-2 text-xs text-(--color-muted) space-y-0.5">
          <div className="text-[10px] font-bold tracking-widest uppercase mb-1">フォーメーション</div>
          <div>1着 (絞): <span className="mono tnum text-(--color-foreground)">{(b.head_horses ?? []).join(", ")}</span></div>
          <div>2着 (中): <span className="mono tnum text-(--color-foreground)">{(b.mid_horses ?? []).join(", ")}</span></div>
          <div>3着 (広): <span className="mono tnum text-(--color-foreground)">{(b.tail_horses ?? []).join(", ")}</span></div>
        </div>
      )}
      <div className="mt-4">
        <BundleLegsTable legs={b.legs} finish={finish} finalOdds={d.result?.final_odds} droppedLegs={b.dropped_legs} nRunners={nR} payoutTable={pt} />
      </div>
      <TrifectaStakePreview bundle={b} />
      <p className="mt-3 text-xs text-(--color-muted)">
        ランキング = {rankLabel} (市場オッズ不使用) · {b.formation} フォーメーション
        {b.bankroll != null ? ` · 購入予算 ¥${b.bankroll.toLocaleString()} 内に収める` : ""}
        {(() => {
          const nTori = (b.dropped_legs ?? []).filter((l) => l.reason !== "budget").length;
          const nBudget = (b.dropped_legs ?? []).filter((l) => l.reason === "budget").length;
          if (!torigamiOn && nBudget === 0) return "";
          const parts: string[] = [];
          if (torigamiOn) parts.push(nTori > 0 ? `トリガミ ${nTori}点除去` : "トリガミ全脚クリア");
          if (nBudget > 0) parts.push(`予算外 ${nBudget}点`);
          return ` · ${parts.join(" / ")} (表の取り消し線=買わない)`;
        })()}
        。当たれば投資総額以上を回収 (トリガミ防止) だが、当たらなければ損 = 長期は −EV になり得る。
      </p>
    </Card>
  );
}

function BundleLegsTable({
  legs,
  finish,
  finalOdds,
  droppedLegs,
  nRunners,
  payoutTable,
}: {
  legs: BundleLeg[];
  finish?: number[];
  // result.final_odds: `"<bet_type>:<key-with-->"` → 最終確定オッズ。無ければ「—」。
  finalOdds?: Record<string, number>;
  // 買わなかった脚 (= トリガミ防止 or 予算で除外)。取り消し線で末尾に併記。
  droppedLegs?: DroppedLeg[];
  // 出走頭数 (複勝の頭数ルール用)。null/未指定なら従来 top-3 判定。
  nRunners?: number | null;
  // 同着フォールバック用 payout テーブル (payoutTableOf の戻り値)。
  payoutTable?: Record<string, number> | null;
}) {
  const hasFinal = !!finalOdds && Object.keys(finalOdds).length > 0;
  const dropped = droppedLegs ?? [];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tnum table-zebra">
        <thead className="text-left text-(--color-muted) text-xs">
          <tr className="border-b border-(--color-line)">
            <th className="py-2 pr-3">種別</th>
            <th className="py-2 pr-3">買い目</th>
            <th className="py-2 pr-3 text-right">予想 O</th>
            {hasFinal && <th className="py-2 pr-3 text-right">最終 O</th>}
            <th className="py-2 pr-3 text-right">推定 P</th>
            <th className="py-2 pr-3 text-right">P×O</th>
            <th className="py-2 pr-3 text-right">Kelly</th>
            <th className="py-2 pr-3 text-right">配分</th>
            <th className="py-2 pr-3 text-right" title="その脚が的中したときの払戻。確定後は最終オッズ基準 (最終 O × 配分)、未確定は予想オッズ基準">
              的中時 払戻{hasFinal ? " (最終)" : ""}
            </th>
            <th className="py-2 pr-3">帯</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l) => {
            const k = l.key.join("-");
            const hit = betHits(l.bet_type, l.key, finish, nRunners, payoutTable);
            const fo = finalOdds?.[`${l.bet_type}:${k}`];
            // 予想→最終で乖離した方向に色付け (上昇=緑/下落=赤)。
            const foTone =
              fo == null || fo <= 0
                ? ""
                : fo > l.odds
                  ? "text-(--color-good)"
                  : fo < l.odds
                    ? "text-(--color-bad)"
                    : "";
            return (
              <tr
                key={`${l.bet_type}:${k}`}
                className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
              >
                <td className="py-1.5 pr-3">
                  <Badge tone={BET_TYPE_TONE[l.bet_type] ?? "muted"}>{betLabel(l.bet_type)}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {hit && <HitMark />}
                </td>
                <td className="py-1.5 pr-3 text-right">{l.odds.toFixed(1)}</td>
                {hasFinal && (
                  <td className={`py-1.5 pr-3 text-right ${foTone}`}>
                    {fo != null && fo > 0 ? fo.toFixed(1) : "—"}
                  </td>
                )}
                <td className="py-1.5 pr-3 text-right">{fmtPct(l.prob, 2)}</td>
                <td className="py-1.5 pr-3 text-right">
                  <Badge tone={pxoTone(l.px_o)}>{l.px_o.toFixed(2)}</Badge>
                </td>
                <td className="py-1.5 pr-3 text-right">{(l.kelly * 100).toFixed(1)}%</td>
                <td className="py-1.5 pr-3 text-right font-bold">¥{l.stake.toLocaleString()}</td>
                <td className="py-1.5 pr-3 text-right text-(--color-good)">
                  {(() => {
                    // 確定後は最終オッズ基準 (fo × stake) に揃える。予想 O 基準 (payout_if_hit) は
                    // 取り消し線で併記。最終 O 未取得なら従来どおり予想 O 基準を表示。
                    const finalPay =
                      fo != null && fo > 0 ? Math.round(fo * l.stake) : null;
                    if (finalPay == null) return <>¥{l.payout_if_hit.toLocaleString()}</>;
                    return (
                      <span title={`予想 O 基準 ¥${l.payout_if_hit.toLocaleString()}`}>
                        ¥{finalPay.toLocaleString()}
                        {finalPay !== l.payout_if_hit && (
                          <span className="ml-1 text-(--color-muted) text-xs line-through">
                            ¥{l.payout_if_hit.toLocaleString()}
                          </span>
                        )}
                      </span>
                    );
                  })()}
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={tierTone(l.tier)}>{tierLabel(l.tier)}</Badge>
                </td>
              </tr>
            );
          })}
          {/* 買わなかった脚 (トリガミ防止 or 予算オーバー): 取り消し線+減光で末尾に併記。
              取り消し線は table-row への propagation が不安定なので各セルへ明示的に付与する。 */}
          {dropped.map((l) => {
            const k = l.key.join("-");
            const isBudget = l.reason === "budget";
            const label = isBudget ? "予算外" : "トリガミ";
            const title = isBudget
              ? "予算 (購入予算) を割り当てきれず買わない脚"
              : "トリガミ防止で除去された脚 — 当たっても投資総額を割るため買わない";
            return (
              <tr
                key={`dropped:${l.bet_type}:${k}`}
                className="border-b border-(--color-line)/60 opacity-60 text-(--color-muted)"
                title={title}
              >
                <td className="py-1.5 pr-3">
                  <Badge tone="muted">{betLabel(l.bet_type)}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-medium mono line-through">{k}</td>
                <td className="py-1.5 pr-3 text-right line-through">{l.odds.toFixed(1)}</td>
                {hasFinal && <td className="py-1.5 pr-3 text-right">—</td>}
                <td className="py-1.5 pr-3 text-right line-through">{fmtPct(l.prob, 2)}</td>
                <td className="py-1.5 pr-3 text-right line-through">{l.px_o.toFixed(2)}</td>
                <td className="py-1.5 pr-3 text-right">—</td>
                <td className="py-1.5 pr-3 text-right line-through">¥0</td>
                <td className="py-1.5 pr-3 text-right">—</td>
                <td className="py-1.5 pr-3">
                  <Badge tone={isBudget ? "muted" : "bad"}>{label}</Badge>
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
  variant = "yield",
  finalOdds,
  evRegime = false,
  nRunners,
  payoutTable,
}: {
  bundle: RecommendedBundle;
  finish?: number[];
  // "yield" = EV束 / "hit" = 旧 的中優先 (廃止済の旧 snapshot 用・緑)
  variant?: "yield" | "hit";
  // result.final_odds (leg_id → 最終確定オッズ)。legs テーブルで 予想/最終 を併記。
  finalOdds?: Record<string, number>;
  // EV束計測レジーム (saved_at >= EV_CUTOFF): EV束が実弾既定束 (2026-06-10〜)。
  evRegime?: boolean;
  // 出走頭数 (複勝の頭数ルール用)。null/未指定なら従来 top-3 判定。
  nRunners?: number | null;
  // 同着フォールバック用 payout テーブル (payoutTableOf の戻り値)。
  payoutTable?: Record<string, number> | null;
}) {
  const isHit = variant === "hit";
  const legs = bundle.legs ?? [];
  // ½Kelly がコード適用済 (kelly_fraction=0.5, 2026-06-10〜) なら total_stake は既に実弾額。
  // 旧 snapshot (kelly_fraction=1.0/欠落) のみ「½ Kelly 推奨」の再半減表示を出す。
  const halfApplied = (bundle.kelly_fraction ?? 1) <= 0.75;
  const half = Math.round(bundle.total_stake / 2 / 100) * 100;
  const nTypes = new Set(legs.map((l) => l.bet_type)).size;
  const bundleHit = finish || payoutTable
    ? legs.some((l) => betHits(l.bet_type, l.key, finish, nRunners, payoutTable))
    : false;
  // EV束は 2026-06-06 以降モデルのみの参考値 (claude -p 検証=回収優先AI は撤去、投票しない)。
  // validated バッジは旧 snapshot の記録としてのみ表示される。
  const validated = !isHit && bundle.llm_review?.validated === true;
  // トリガミ防止マージン (保存オッズからの下振れ緩衝)。古い snapshot は未保存 → 1 扱い。
  const margin = bundle.torigami_margin ?? 1;
  const driftPct = margin > 1 ? Math.round((1 - 1 / margin) * 100) : 0;
  // scripts/backfill_bundle.py が後付けした paper 束 (実弾ではない)。api.ts の型には
  // 未定義のため cast で読む (api/store.py は bundle_backfilled として計測から除外済)。
  const backfilled =
    (bundle as RecommendedBundle & { backfilled?: boolean }).backfilled === true;
  // タイトル/色は dashboard 規約に統一: EV束=highlight / 的中優先=緑(good)。
  // EV束はモデルのみの参考値 (実弾投票束は 3連単束 = recommended_bundle_t)。
  const titleLabel = isHit
    ? "的中優先AI — まとめ買い"
    : evRegime
      ? "EV束 (実弾既定束) — まとめ買い"
      : "EV束 (モデル参考) — まとめ買い";
  const titleColor = isHit ? "text-(--color-good)" : "text-(--color-highlight)";
  return (
    <Card
      // 的中優先は「買わない」おまけ計測なので alert ハイライトせず default で従属表示。
      tone={!isHit && legs.length ? "alert" : "default"}
      // タイトルは純テキストだけ。右ズレを避けるためバッジ類は right prop / body に移す。
      title={
        <span className={`flex items-center gap-2 ${titleColor} font-black`}>
          <Coins className="w-4 h-4 shrink-0" aria-hidden />
          <span>{titleLabel}</span>
          {isHit && (
            <span className="text-xs font-normal text-(--color-muted)">
              prob 降順 pool / おまけ計測・買わない
            </span>
          )}
        </span>
      }
      right={
        <span className="flex items-center gap-2">
          {backfilled && (
            <span title="scripts/backfill_bundle.py が後付けした paper 束 — 実弾投票ではない (計測からも除外)">
              <Badge tone="muted">backfill (paper)</Badge>
            </span>
          )}
          {!isHit && (validated ? (
            <Badge tone="magenta">claude -p 検証済 (旧記録){bundle.llm_review?.confidence ? ` (${bundle.llm_review.confidence})` : ""}</Badge>
          ) : (
            legs.length > 0 && (evRegime
              ? <Badge tone="magenta">実弾投票束 (KEIBA_BET_BUNDLE=ev 既定)</Badge>
              : <Badge tone="muted">参考値・投票対象外 (投票は3連単束)</Badge>)
          ))}
          {finish && (legs.length === 0
            ? <Badge tone="muted">束 見送り</Badge>
            : bundleHit
              ? <Badge tone={isHit ? "info" : "good"}>束 的中</Badge>
              : <Badge tone="bad">束 不的中</Badge>)}
        </span>
      }
    >
      <p className="text-xs text-(--color-muted) mb-3">
        全 bet type 横断 · joint (同時) Kelly 最適配分
        {legs.length > 0 && ` · 完全 top-3 分布 ${bundle.n_outcomes} 通りで E[log 資金] 最大化`}
      </p>
      {legs.length === 0 ? (
        <p className="text-sm text-(--color-muted)">
          +EV (P×O≥{bundle.pxo_floor.toFixed(2)}) のまとめ買いなし。
          <span className="font-bold text-(--color-foreground)">見送り推奨</span> (市場効率的)。
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <Stat
              label={halfApplied
                ? "まとめ買い総額 (½ Kelly 適用済 = 実弾額)"
                : "まとめ買い総額 (full Kelly)"}
              value={
                <span>
                  ¥{bundle.total_stake.toLocaleString()}
                  <span className="text-(--color-muted) text-xs ml-1">
                    / ¥{bundle.bankroll.toLocaleString()} ({(bundle.total_fraction * 100).toFixed(0)}%)
                  </span>
                </span>
              }
            />
            {halfApplied ? (
              <Stat label="適用 Kelly 比" value={`×${(bundle.kelly_fraction ?? 1).toFixed(2)}`} />
            ) : (
              <Stat label="½ Kelly (保守・推奨)" value={`¥${half.toLocaleString()}`} />
            )}
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
          <BundleLegsTable legs={legs} finish={finish} finalOdds={finalOdds} nRunners={nRunners} payoutTable={payoutTable} />
          {bundle.llm_review?.validated && (bundle.llm_review.summary || (bundle.llm_review.cuts?.length ?? 0) > 0) && (
            <div className="mt-3 rounded-lg border border-violet-500/30 bg-violet-500/10 p-3 text-xs">
              <span className="inline-flex items-center gap-1 font-bold text-violet-300">
                <Sparkles className="w-3.5 h-3.5 shrink-0" aria-hidden />
                claude -p 調査:
              </span>{" "}
              {bundle.llm_review.summary || "—"}
              {(bundle.llm_review.cuts?.length ?? 0) > 0 && (
                <span className="ml-1 text-rose-300">
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
            {halfApplied ? (
              <>表示の stake は <span className="font-bold">½ Kelly 適用済み (= 自動投票が積む実弾額)</span>。</>
            ) : (
              <>full Kelly は攻め過ぎになりやすく、確率推定が楽観な本モデルでは
              <span className="font-bold"> ½ Kelly (各 stake 半額)</span> が実運用の推奨。</>
            )}
            候補 {bundle.n_candidates} 点から選択。
          </p>
        </>
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

// 全券種ランキング行 (推定P上位20)。BetEvRow に bet_type を付与したもの。
type RankedRow = BetEvRow & { betType: string };

function ProbRankingTable({
  rows,
  finish,
}: {
  rows: RankedRow[];
  finish?: number[];
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tnum table-zebra">
        <thead className="text-left text-(--color-muted) text-xs">
          <tr className="border-b border-(--color-line)">
            <th className="py-2 pr-3 text-right">#</th>
            <th className="py-2 pr-3">種別</th>
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
            const hit = betHits(r.betType, r.key, finish);
            const marketRate = r.odds > 0 ? 100 / r.odds : 0;
            const evaluation = pxoEvaluation(r.px_o);
            return (
              <tr
                key={`${r.betType}:${k}`}
                className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
              >
                <td className="py-1.5 pr-3 text-right text-(--color-muted)">{i + 1}</td>
                <td className="py-1.5 pr-3">
                  <Badge tone={BET_TYPE_TONE[r.betType] ?? "muted"}>{betLabel(r.betType)}</Badge>
                </td>
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {hit && <HitMark />}
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

function diffTone(diff: number | null): BadgeTone {
  if (diff == null) return "muted";
  if (diff >= 10) return "good";      // Claude が市場より強気
  if (diff <= -10) return "bad";      // Claude が市場より弱気
  return "muted";
}

// 直前/軟情報フラグの色分け: 否定 (取消/不利/減/イレ込み 等) = bad、
// 好材料 (勝負気配/強化/有利/良/叩き 等) = good、それ以外 (展開/隊列の中立メモ) = warn。
function alertTone(label: string): BadgeTone {
  const negative = /取消|除外|回避|出遅|不利|詰|包|進路|渋|不安|不適|距離不|イレ込|チャカ|腰|裏|減|マイナス|-\d/;
  const positive = /勝負気配|強化|抜群|良化|仕上|有利|高速|叩き|上昇|気配良|プラス|\+\d/;
  if (negative.test(label)) return "bad";
  if (positive.test(label)) return "good";
  return "warn";
}

function IndexCompareCard({
  items,
  finish,
  scoredAt,
  hasClaude,
}: {
  items: NonNullable<PredictionDetail["index_compare"]>;
  finish?: number[];
  scoredAt?: string | null;
  hasClaude?: boolean;
}) {
  const finishSet = new Set(finish ?? []);
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-violet-300 shrink-0" aria-hidden />
          <span className="text-violet-300">Claude 指数 vs 市場指数 (参考)</span>
          <span className="text-xs text-(--color-muted) font-normal">
            {hasClaude
              ? "Claude 強さ指数と市場指数の乖離を見る参考表 · 確率 P は Claude 指数 ⊗ モデル指数(GBM⊗速度図表) で合成し市場指数は P に未合成 (市場無視) · 市場は P×O の O として効く · 差 = Claude − 市場 (正 = Claude 強気 = contrarian 狙い) · 根=補強根拠件数 · 直前/軟情報=取消/馬体重/前走不利/勝負気配 等のフラグ"
              : "Claude 指数なし (score 未実施) · 市場指数のみ (オッズ由来 0-100、1.0倍で100、参考)"}
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tnum table-zebra">
          <thead className="text-left text-(--color-muted) text-xs">
            <tr className="border-b border-(--color-line)">
              <th className="py-2 pr-3 text-right">馬</th>
              <th className="py-2 pr-3">馬名</th>
              <th className="py-2 pr-3 text-right text-violet-300/90" title="Claude 強さ指数 0-100 (市場独立の相対評価、検索補強で上下)">Claude 指数</th>
              <th className="py-2 pr-3 text-right" title="市場指数 = 100·(1/オッズ)^(1/1.5) (1.0倍で100、温度T=1.5で0-100に分布)。参考: 確率 P には未合成 (市場無視)、P×O の O として効く">市場指数<span className="text-(--color-muted)"> (参考)</span></th>
              <th className="py-2 pr-3 text-right" title="Claude − 市場。正 = Claude が市場より強気、負 = 弱気">差</th>
              <th className="py-2 pr-3 text-right" title="補強根拠件数。多い馬ほどモデルが Claude 勝率を厚く採用 (0=市場どおり)">根</th>
              <th className="py-2 pr-2" title="直前/軟情報フラグ (取消・馬体重増減・前走不利・厩舎勝負気配・展開 等)。市場がまだ織り込みきれない情報。表示/記録用 (確率には未反映)">直前/軟情報</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => {
              const hit = finishSet.has(r.number);
              return (
                <tr
                  key={r.number}
                  className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
                >
                  <td className="py-1.5 pr-3 text-right font-bold">{r.number}</td>
                  <td className="py-1.5 pr-3">
                    {r.name}
                    {hit && finish && <HitMark place={finish.indexOf(r.number) + 1} />}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.claude_index != null ? r.claude_index.toFixed(1) : "—"}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.market_index != null ? r.market_index.toFixed(1) : "—"}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.diff != null ? (
                      <Badge tone={diffTone(r.diff)}>
                        {r.diff > 0 ? `+${r.diff.toFixed(1)}` : r.diff.toFixed(1)}
                      </Badge>
                    ) : (
                      <span className="text-(--color-muted)">—</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.support != null && r.support > 0 ? (
                      <Badge tone={r.support >= 3 ? "good" : "muted"}>{r.support}</Badge>
                    ) : (
                      <span className="text-(--color-muted)">{r.support === 0 ? "0" : "—"}</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-2">
                    {r.alerts && r.alerts.length > 0 ? (
                      <span className="flex flex-wrap gap-1">
                        {r.alerts.map((a, i) => (
                          <Badge key={i} tone={alertTone(a)}>{a}</Badge>
                        ))}
                      </span>
                    ) : (
                      <span className="text-(--color-muted)">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-(--color-muted)">
        この表は Claude 指数と市場指数の乖離を見る参考。live の確率 P は Claude 指数 ⊗ モデル指数
        (GBM ⊗ 速度図表) で合成し、市場指数 (= 100·(1/オッズ)^(1/1.5)、1.0倍で100) は P に混ぜていない
        (市場無視 market_blend=0)。市場は P×O の O (実オッズ) としてのみ効く。Claude 指数 = 各馬の力を
        0-100 で相対評価 (市場には揃えず、全馬を web 検索で補強して上下)。差 = Claude − 市場 (正 =
        Claude が市場より強気 = contrarian の狙い目)。根 (補強根拠件数) が多い馬ほど確率モデルが
        Claude 指数を厚く採用 (0 = 動かさない)。
        {scoredAt ? ` · Claude 指数: ${fmtServerDateTime(scoredAt)}` : ""}
      </p>
    </Card>
  );
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
          <Gauge className="w-4 h-4 text-(--color-accent) shrink-0" aria-hidden />
          <span>適性指数</span>
          <span className="text-xs text-(--color-muted) font-normal">
            各馬の相対適性 0-100 (同レース内 max=100) · 総合 = 9 因子重み付け平均
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tnum table-zebra">
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
                  className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
                >
                  <td className="py-1.5 pr-3 text-right font-bold">{a.number}</td>
                  <td className="py-1.5 pr-3">
                    {a.name}
                    {hit && finish && <HitMark place={finish.indexOf(a.number) + 1} />}
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

// 複勝専用: 「該当 key (1 馬) が finish の払戻対象に入っているか」を判定。
// 出走頭数ルール (2026-06-11 bughunt 第4R, backend portfolio._bet_hits と同じ):
// 7頭以下は複勝2着まで・4頭以下は発売なし。頭数不明 (null) は従来どおり top3。
function placeHits(key: number[], finish?: number[], nRunners?: number | null): boolean {
  if (!finish || finish.length < 3 || key.length !== 1) return false;
  if (nRunners != null && nRunners <= 4) return false;
  const paying = nRunners != null && nRunners <= 7 ? finish.slice(0, 2) : finish.slice(0, 3);
  return paying.includes(key[0]);
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
  nRunners,
}: {
  bt: string;
  rows: BetEvRow[];
  rowsG?: BetEvRow[];
  finish?: number[];
  // 出走頭数 (複勝の頭数ルール用)。null/未指定なら従来 top-3 判定。
  nRunners?: number | null;
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
        <table className="w-full text-sm tnum table-zebra">
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
                    ? placeHits(r.key, finish, nRunners)
                    : finishKey === k;
              const inG = gKeySet.has(k);
              return (
                <tr
                  key={k}
                  className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
                >
                  <td className="py-1.5 pr-3 text-right text-(--color-muted)">
                    {i + 1}
                  </td>
                  <td className="py-1.5 pr-3 font-medium mono">
                    {k}
                    {hit && <HitMark />}
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
  nRunners,
}: {
  tables: Record<string, BetEvRow[]>;
  tablesG?: Record<string, BetEvRow[]>;
  finish?: number[];
  nRunners?: number | null;
}) {
  // bet type の表示順: リスク低 → 高 (単勝 → 複勝 → 馬連 → ワイド → 馬単 → 3 連複)
  const order = ["win", "place", "quinella", "wide", "exacta", "trio"];
  const present = order.filter((bt) => (tables[bt]?.length ?? 0) > 0);
  if (present.length === 0) return null;
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Layers className="w-4 h-4 text-(--color-info) shrink-0" aria-hidden />
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
          nRunners={nRunners}
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
          <Scale className="w-4 h-4 text-(--color-info) shrink-0" aria-hidden />
          <span>市場乖離 (1着型 / 3着型)</span>
          <span className="text-xs text-(--color-muted) font-normal">
            単勝 vs 複勝 implied prob 比率で構造的ミスプライスを検出
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tnum table-zebra">
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
                <tr key={s.number} className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}>
                  <td className="py-1.5 pr-3 text-right font-bold">{s.number}</td>
                  <td className="py-1.5 pr-3">
                    {s.name}
                    {hit && finish && <HitMark place={finish.indexOf(s.number) + 1} />}
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
          <Timer className="w-4 h-4 text-(--color-accent) shrink-0" aria-hidden />
          <span>持ち時計</span>
          <span className="text-xs text-(--color-muted) font-normal">
            同 venue × 同距離 ±100m × 同 surface での past best own_time_sec (速い順)
          </span>
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm tnum table-zebra">
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
                <tr key={t.number} className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}>
                  <td className="py-1.5 pr-3 text-right font-bold">{t.number}</td>
                  <td className="py-1.5 pr-3">
                    {t.name}
                    {hit && finish && <HitMark place={finish.indexOf(t.number) + 1} />}
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
      <table className="w-full text-sm tnum table-zebra">
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
                className={`border-b border-(--color-line)/60 ${hit ? `${HIT_ROW_BG} text-emerald-300` : ""}`}
              >
                <td className="py-1.5 pr-3 font-medium mono">
                  {k}
                  {hit && <HitMark />}
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
