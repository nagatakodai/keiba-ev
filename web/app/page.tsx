import Link from "next/link";
import {
  api,
  type CalibrationReport,
  type PredictionSummary,
  type WatchAutoHistoryItem,
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
  fmtServerDateTime,
  fmtYen,
  parsePlanLabel,
  planAccentClass,
  planBarClass,
  raceTimingRowBg,
  raceTimingStatus,
  savedAtDate,
  todayJST,
  type PlanLetter,
} from "@/components/ui";
import { AutoRefresh } from "@/components/AutoRefresh";
import { PredictionsList } from "@/components/PredictionsList";

export const dynamic = "force-dynamic";

type RaceHit = CalibrationReport["races"][number];

function PlanHitTag({ plan, hit }: { plan: PlanLetter; hit: boolean }) {
  return hit ? (
    <span className={`font-bold ${planAccentClass(plan)}`}>{plan} ✓</span>
  ) : (
    <span className="font-bold text-(--color-muted)">{plan} ×</span>
  );
}

function roiTone(roi: number): "good" | "warn" | "bad" {
  if (roi >= 1) return "good";
  if (roi >= 0.85) return "warn";
  return "bad";
}

type PlanWithCi = {
  hits: number;
  participated_races: number;
  hit_rate: number;
  roi: number;
  roi_ci_low?: number;
  roi_ci_high?: number;
};

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}

// 表示方針: CI 範囲を常に [low, high] で出す。
// ±X 形式は CI が対称な前提だが、ROI の bootstrap CI は小サンプルだと
// 強く skewed (下限 0 ・上限大) になるので「±1849%」のように本来の
// 情報量より広く見える誤った印象を与える。
function planRoiHint(p: PlanWithCi): string {
  const base = `hit ${p.hits}/${p.participated_races} (${fmtPct(p.hit_rate, 1)})`;
  if (p.roi_ci_low === undefined || p.roi_ci_high === undefined) return base;
  const tail =
    p.participated_races < 30 ? " · 参考値 (n<30)" : "";
  return `${base} · CI [${fmtRoiPct(p.roi_ci_low)}, ${fmtRoiPct(p.roi_ci_high)}]${tail}`;
}

// 小サンプル時は tone を warn に落として、ROI 値だけで緑表示しない。
function planStatTone(
  p: PlanWithCi | undefined,
): "default" | "good" | "warn" | "bad" {
  if (!p || p.participated_races === 0) return "default";
  if (p.participated_races < 30) return "warn"; // n<30 は判断材料未満
  return roiTone(p.roi);
}

// 最新 / 最新の的中 で共用する 1 行レンダラ。
function PredictionRowItem({
  p,
  hit,
  nowMs,
  closeAtMap,
  startAtMap,
}: {
  p: PredictionSummary;
  hit: RaceHit | undefined;
  nowMs: number;
  closeAtMap?: Map<string, number>;
  startAtMap?: Map<string, number>;
}) {
  const closeAt = p.close_at ?? closeAtMap?.get(p.race_id) ?? null;
  const startAt = p.start_at ?? startAtMap?.get(p.race_id) ?? null;
  const timing = raceTimingStatus(closeAt, startAt, p.has_result, nowMs);
  const anyHit = !!(
    hit &&
    (hit.bundle_hit ||
      hit.plan_a_hit ||
      hit.plan_b_hit ||
      hit.plan_c_hit ||
      hit.plan_g_hit ||
      hit.plan_h1_hit ||
      hit.plan_h2_hit ||
      hit.plan_f_hit)
  );
  // Claude 総合オススメが「見送り」(束 legs 空) なら「不的中」ではなく「未参加」表示
  const bundleSkipped = !!(hit && hit.bundle_participated === false);
  const rowBg = hit
    ? raceTimingRowBg(anyHit ? "good" : bundleSkipped ? "muted" : "bad")
    : raceTimingRowBg(timing.tone);

  return (
    <li
      className={`py-2.5 flex items-center gap-3 -mx-4 px-4 ${rowBg}`}
    >
      <Link
        href={`/predictions/${p.race_id}`}
        className="flex-1 group min-w-0"
      >
        <div className="flex items-center gap-2 text-sm flex-wrap">
          <span className="font-medium truncate">
            {p.venue_name} {p.race_number}R
          </span>
          <Badge tone="muted">{p.race_class}</Badge>
          {hit ? (
            anyHit ? (
              <Badge tone="good">的中</Badge>
            ) : bundleSkipped ? (
              <Badge tone="muted">見送り</Badge>
            ) : (
              <Badge tone="bad">不的中</Badge>
            )
          ) : (
            <Badge tone={timing.tone}>{timing.label}</Badge>
          )}
          {p.has_evidence ? (
            <Badge tone="magenta">補強済</Badge>
          ) : !p.has_result ? (
            <Badge tone="muted">評価待ち</Badge>
          ) : null}
        </div>
        {hit ? (
          <div className="text-xs tabnum mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <span>
              着順{" "}
              <span className="font-bold mono">{hit.finish.join("-")}</span>
            </span>
            <span className="text-(--color-muted)">·</span>
            <PlanHitTag plan="F" hit={!!hit.plan_f_hit} />
            <PlanHitTag plan="A" hit={hit.plan_a_hit} />
            <PlanHitTag plan="B" hit={hit.plan_b_hit} />
            <PlanHitTag plan="C" hit={hit.plan_c_hit} />
            <PlanHitTag plan="G" hit={!!hit.plan_g_hit} />
            <PlanHitTag plan="H1" hit={!!hit.plan_h1_hit} />
            <PlanHitTag plan="H2" hit={!!hit.plan_h2_hit} />
            {hit.payout > 0 && (
              <>
                <span className="text-(--color-muted)">·</span>
                <span className="font-bold text-(--color-good)">
                  ¥{hit.payout.toLocaleString()}
                </span>
              </>
            )}
          </div>
        ) : (
          <div className="text-xs text-(--color-muted) mt-0.5 tabnum flex flex-wrap gap-x-1.5">
            <span>{fmtServerDateTime(p.saved_at)}</span>
            <span>·</span>
            <span>候補 {p.row_count}</span>
            <span>·</span>
            {(p.plan_f_count ?? 0) > 0 && (
              <>
                <span className={`font-bold ${planAccentClass("F")}`}>
                  F·{p.plan_f_count}
                </span>
                <span>·</span>
              </>
            )}
            <span className={`font-bold ${planAccentClass("A")}`}>A{p.plan_a_count}</span>
            <span className={`font-bold ${planAccentClass("B")}`}>B{p.plan_b_count}</span>
            <span className={`font-bold ${planAccentClass("C")}`}>C{p.plan_c_count}</span>
            {(p.plan_g_count ?? 0) > 0 && (
              <span className={`font-bold ${planAccentClass("G")}`}>
                G·{p.plan_g_count}
              </span>
            )}
            {(p.plan_h1_count ?? 0) > 0 && (
              <span className={`font-bold ${planAccentClass("H1")}`}>
                H1·{p.plan_h1_count}
              </span>
            )}
            {(p.plan_h2_count ?? 0) > 0 && (
              <span className={`font-bold ${planAccentClass("H2")}`}>
                H2·{p.plan_h2_count}
              </span>
            )}
          </div>
        )}
      </Link>
      <Link
        href={`/predictions/${p.race_id}`}
        className="text-xs text-(--color-accent) hover:underline shrink-0"
      >
        詳細
      </Link>
    </li>
  );
}

export default async function DashboardPage() {
  const [preds, cal, watch, watchHist] = await Promise.all([
    api.listPredictions(200).catch(() => ({
      items: [] as PredictionSummary[],
    })),
    api.calibrate().catch(() => null),
    api.watchStatus().catch(() => null),
    api
      .watchHistory(500)
      .catch(() => ({ items: [] as WatchAutoHistoryItem[] })),
  ]);

  const planA = cal?.plans.find((p) => p.plan === "Plan A");
  const planH1 = cal?.plans.find((p) => p.plan === "Plan H1");

  const raceHitMap = new Map<string, RaceHit>();
  for (const r of cal?.races ?? []) raceHitMap.set(r.race_id, r);
  const closeAtMap = new Map<string, number>();
  const startAtMap = new Map<string, number>();
  for (const h of watchHist.items) {
    if (h.race_id && h.close_at) closeAtMap.set(h.race_id, h.close_at);
    if (h.race_id && h.start_at != null) startAtMap.set(h.race_id, h.start_at);
  }

  const nowMs = Date.now();
  const today = todayJST(nowMs);
  const todaysPreds = preds.items.filter(
    (p) => savedAtDate(p.saved_at) === today,
  );
  // 最新 (全期間, 最新 10)
  const latestPreds = preds.items.slice(0, 10);
  // 最新の的中: raceHitMap に entry があって any plan が hit したもの。
  // RaceRow 内の anyHit (line 100-109) と plan セットを一致させる
  // (Plan G/F のみ的中したレースが latest hits に出ない不整合の修正)。
  const latestHits = preds.items
    .filter((p) => {
      const h = raceHitMap.get(p.race_id);
      return !!(
        h &&
        (h.plan_a_hit ||
          h.plan_b_hit ||
          h.plan_c_hit ||
          h.plan_g_hit ||
          h.plan_h1_hit ||
          h.plan_h2_hit ||
          h.plan_f_hit)
      );
    })
    .slice(0, 10);

  const confidence = cal ? calibrationConfidence(cal.race_count) : null;
  const lastUpdated = cal ? fmtRelativeFromNow(cal.last_updated_at, nowMs) : "—";

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        title="ダッシュボード"
        subtitle="長期 +EV のためのリアルタイム俯瞰。15 秒おきに自動更新。"
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="watch-auto"
          value={watch?.running ? "稼働中" : "停止"}
          hint={
            watch?.running
              ? `発走${watch.config.window}〜${(watch.config.window ?? 0) + (watch.config.tolerance ?? 0)}分前 / ${watch.config.interval_sec}s`
              : "—"
          }
          tone={watch?.running ? "good" : "default"}
        />
        <Stat
          label="集計対象レース"
          value={cal?.race_count ?? 0}
          hint={
            confidence ? (
              <span className="flex items-center gap-1">
                <Badge tone={confidence.tone}>{confidence.label}</Badge>
                <span>· 最終更新 {lastUpdated}</span>
              </span>
            ) : (
              "—"
            )
          }
          tone={confidence?.tone === "good" ? "good" : confidence?.tone === "warn" ? "warn" : "bad"}
        />
        <Stat
          label="Plan A 回収率 (EV 枠)"
          value={!planA ? "—" : planA.participated_races === 0 ? "未参加" : fmtRoiPct(planA.roi)}
          hint={
            planA
              ? planA.participated_races === 0
                ? "買い目を出したレースなし"
                : planRoiHint(planA)
              : ""
          }
          tone={planStatTone(planA)}
        />
        <Stat
          label="Plan H1 回収率 (当て枠)"
          value={!planH1 ? "—" : planH1.participated_races === 0 ? "未参加" : fmtRoiPct(planH1.roi)}
          hint={
            planH1
              ? planH1.participated_races === 0
                ? "新スキーマ待ち (本日以降の analyze から蓄積)"
                : planRoiHint(planH1)
              : ""
          }
          tone={planStatTone(planH1)}
        />
      </div>

      {/* 2. Plan 別 ROI — ダッシュボード 2 番目 */}
      {cal && cal.plans.length > 0 && (
        <Card title={`Plan 別 回収率 (1点 ¥${cal.point_cost})`}>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
            {cal.plans.map((p) => {
              const letter = parsePlanLabel(p.plan);
              const avgPoints =
                p.participated_races > 0
                  ? p.total_points / p.participated_races
                  : 0;
              const perPointRealistic =
                avgPoints > 0 ? p.assumed_budget_slot / avgPoints : 0;
              const notParticipated = p.participated_races === 0;
              return (
                <div
                  key={p.plan}
                  className={`border border-(--color-line) bg-white p-4 ${
                    notParticipated ? "opacity-60" : ""
                  }`}
                >
                  <div className="flex items-center gap-2">
                    {letter && (
                      <span
                        className={`inline-block w-1 h-4 ${planBarClass(letter)}`}
                      />
                    )}
                    <div
                      className={`text-sm font-bold ${
                        letter ? planAccentClass(letter) : ""
                      }`}
                    >
                      {p.plan}
                    </div>
                  </div>
                  {notParticipated ? (
                    <>
                      <div className="mt-2 text-3xl font-black tabnum text-(--color-muted)">
                        未参加
                      </div>
                      <div className="text-xs text-(--color-muted) mt-1">
                        買い目を出したレースなし
                        <br />
                        <span className="text-[10px]">
                          旧 snapshot は plan_h*_keys 不在で silent 除外。
                          本日以降の analyze から蓄積開始。
                        </span>
                      </div>
                    </>
                  ) : (
                    <>
                      <div
                        className={`mt-2 text-3xl font-black tabnum ${
                          // 参加 < 30 は判断材料未満なので、ROI による緑表示はしない
                          p.participated_races < 30
                            ? "text-(--color-warn)"
                            : p.roi >= 1
                            ? "text-(--color-good)"
                            : p.roi >= 0.85
                            ? "text-(--color-warn)"
                            : "text-(--color-bad)"
                        }`}
                      >
                        {fmtRoiPct(p.roi)}
                      </div>
                      <div className="text-xs text-(--color-muted) mt-1 tabnum">
                        hit {p.hits}/{p.participated_races} (
                        {fmtPct(p.hit_rate, 1)})
                      </div>
                      {p.roi_ci_low !== undefined && p.roi_ci_high !== undefined && (
                        <div className="text-[11px] text-(--color-muted) mt-0.5 tabnum">
                          CI [{fmtRoiPct(p.roi_ci_low)}, {fmtRoiPct(p.roi_ci_high)}]
                          {p.participated_races < 30 && (
                            <span className="ml-1 text-(--color-warn)">
                              · 参考値
                            </span>
                          )}
                        </div>
                      )}
                      <div className="text-xs text-(--color-muted) mt-0.5 tabnum">
                        賭金 {fmtYen(p.stake)} · 払戻 {fmtYen(p.payout)}
                      </div>
                      <div className="text-[11px] text-(--color-muted) mt-1 tabnum">
                        枠 {fmtYen(p.assumed_budget_slot)} ÷ 平均{" "}
                        {avgPoints.toFixed(1)}点 ≒{" "}
                        {perPointRealistic > 0
                          ? fmtYen(Math.round(perPointRealistic))
                          : "—"}
                        /点
                      </div>
                    </>
                  )}
                </div>
              );
            })}
          </div>
          <p className="text-xs text-(--color-muted) mt-3 flex items-center gap-2 flex-wrap">
            {confidence && <Badge tone={confidence.tone}>{confidence.label}</Badge>}
            <span>
              サンプル数 {cal.race_count}・最終更新 {lastUpdated}
              {confidence && confidence.tone === "good"
                ? " · 数値は判断材料に使える"
                : " · 数値は参考程度に"}
            </span>
          </p>
        </Card>
      )}

      {/* 3. 最新 / 最新の的中 を横並び */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card
          title="最新の予測 (直近 10 件)"
          right={
            <Link
              href="/predictions"
              className="text-xs text-(--color-accent) hover:underline"
            >
              すべて見る →
            </Link>
          }
        >
          {latestPreds.length === 0 ? (
            <p className="text-sm text-(--color-muted)">
              まだ予測はありません。<Link className="text-(--color-accent)" href="/analyze">解析</Link> から URL を投入してください。
            </p>
          ) : (
            <ul className="divide-y divide-(--color-line)">
              {latestPreds.map((p) => (
                <PredictionRowItem
                  key={`${p.race_id}-${p.saved_at}`}
                  p={p}
                  hit={raceHitMap.get(p.race_id)}
                  nowMs={nowMs}
                  closeAtMap={closeAtMap}
                  startAtMap={startAtMap}
                />
              ))}
            </ul>
          )}
        </Card>

        <Card
          title={
            <span className="flex items-center gap-2">
              <span>最新の的中</span>
              <Badge tone="good">{latestHits.length}</Badge>
            </span>
          }
          right={
            <Link
              href="/predictions"
              className="text-xs text-(--color-accent) hover:underline"
            >
              すべて見る →
            </Link>
          }
        >
          {latestHits.length === 0 ? (
            <p className="text-sm text-(--color-muted)">
              まだ的中レースがありません。
            </p>
          ) : (
            <ul className="divide-y divide-(--color-line)">
              {latestHits.map((p) => (
                <PredictionRowItem
                  key={`${p.race_id}-${p.saved_at}`}
                  p={p}
                  hit={raceHitMap.get(p.race_id)}
                  nowMs={nowMs}
                  closeAtMap={closeAtMap}
                  startAtMap={startAtMap}
                />
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* 4. 本日全件 (venue 別) */}
      <div className="space-y-3">
        <div className="flex items-baseline justify-between gap-3 px-1">
          <h2 className="flex items-center gap-2 text-sm font-bold tracking-tight">
            <span className="inline-block w-1 h-4 bg-(--color-highlight)" />
            <span>予測履歴 — 本日 {today} 分 (会場ごと)</span>
            <span className="text-xs text-(--color-muted) font-normal">
              {todaysPreds.length} 件
            </span>
          </h2>
          <Link
            href="/predictions"
            className="text-xs text-(--color-accent) hover:underline shrink-0"
          >
            予測履歴ページへ →
          </Link>
        </div>
        <PredictionsList
          items={todaysPreds}
          nowMs={nowMs}
          raceHitMap={raceHitMap}
          closeAtMap={closeAtMap}
          startAtMap={startAtMap}
          emptyMessage="本日の予測はまだありません。"
        />
      </div>
    </Page>
  );
}
