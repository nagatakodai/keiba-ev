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
  // Claude 総合オススメが「見送り」(束 legs 空) なら「不的中」ではなく「未参加」表示。
  // **bundleSkipped は anyHit より優先**: 賭けていない race は plan_X_hit (理論値) が
  // 立っていても「的中」ではない (見送り = 不参加)。
  const bundleSkipped = !!(hit && hit.bundle_participated === false);
  // 回収優先 bundle hit OR 的中優先 bundle hit のどちらかで的中扱い
  const anyHit =
    !bundleSkipped &&
    !!(hit && (hit.bundle_hit || hit.bundle_hit_first_hit));
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
            {hit.bundle_hit && <Badge tone="good">回収 Claude</Badge>}
            {hit.bundle_hit_first_hit && <Badge tone="info">的中 Claude</Badge>}
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

  const claudeBundle = cal?.claude_bundle;
  const claudeBundleHit = cal?.claude_bundle_hit;

  const raceHitMap = new Map<string, RaceHit>();
  for (const r of cal?.races ?? []) raceHitMap.set(r.race_id, r);
  const closeAtMap = new Map<string, number>();
  const startAtMap = new Map<string, number>();
  for (const h of watchHist.items) {
    if (h.race_id && h.close_at) closeAtMap.set(h.race_id, h.close_at);
    if (h.race_id && h.start_at != null) startAtMap.set(h.race_id, h.start_at);
  }

  const nowMs = Date.now();
  // 予測履歴セクションを削除したので related な集計は不要。
  const confidence = cal ? calibrationConfidence(cal.race_count) : null;
  const lastUpdated = cal ? fmtRelativeFromNow(cal.last_updated_at, nowMs) : "—";

  return (
    <Page>
      <AutoRefresh seconds={15} />
      <PageHeader
        title="ダッシュボード"
        subtitle="競馬オーケストレーション AI の実弾運用と AI 比較の俯瞰。15 秒おきに自動更新。"
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {/* 1) 集計対象レース を左端に (2026-05-29 ユーザ指示) */}
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
          label="watch-auto"
          value={watch?.running ? "稼働中" : "停止"}
          hint={
            watch?.running
              ? `発走${watch.config.window}〜${(watch.config.window ?? 0) + (watch.config.tolerance ?? 0)}分前 / ${watch.config.interval_sec}s`
              : "—"
          }
          tone={watch?.running ? "good" : "default"}
        />
        {/* 回収優先 AI (recommended_bundle = 実弾で買う) の回収率・的中率 (見送りは含まない) */}
        <Stat
          label="回収優先AI 回収率"
          value={
            !claudeBundle || claudeBundle.participated_races === 0
              ? "—"
              : fmtRoiPct(claudeBundle.roi)
          }
          hint={
            claudeBundle && claudeBundle.participated_races > 0
              ? `${claudeBundle.participated_races} 参加 / ${claudeBundle.skipped_races} 見送り · 賭金 ${fmtYen(claudeBundle.stake)} → 払戻 ${fmtYen(claudeBundle.payout)}`
              : "賭けたレースなし"
          }
          tone={
            !claudeBundle || claudeBundle.participated_races < 30
              ? "warn"
              : claudeBundle.roi >= 1
              ? "good"
              : claudeBundle.roi >= 0.85
              ? "warn"
              : "bad"
          }
        />
        <Stat
          label="回収優先AI 的中率"
          value={
            !claudeBundle || claudeBundle.participated_races === 0
              ? "—"
              : fmtPct(claudeBundle.hit_rate, 1)
          }
          hint={
            claudeBundle && claudeBundle.participated_races > 0
              ? `${claudeBundle.hits} 的中 / ${claudeBundle.participated_races} 参加 (見送り除く)`
              : ""
          }
          tone={
            !claudeBundle || claudeBundle.participated_races < 30
              ? "warn"
              : claudeBundle.hit_rate >= 0.3
              ? "good"
              : claudeBundle.hit_rate >= 0.15
              ? "warn"
              : "bad"
          }
        />
      </div>

      {/* 1.5 的中優先 AI (おまけ計測): 実弾では買わないが「もし的中優先で賭けたら」を測る */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Stat
          label="的中優先AI 回収率"
          value={
            !claudeBundleHit || claudeBundleHit.participated_races === 0
              ? "—"
              : fmtRoiPct(claudeBundleHit.roi)
          }
          hint={
            claudeBundleHit && claudeBundleHit.participated_races > 0
              ? `${claudeBundleHit.participated_races} 参加 / 賭金 ${fmtYen(claudeBundleHit.stake)} → 払戻 ${fmtYen(claudeBundleHit.payout)}`
              : "新スキーマ待ち (本日以降の analyze から蓄積)"
          }
          tone={
            !claudeBundleHit || claudeBundleHit.participated_races < 30
              ? "warn"
              : claudeBundleHit.roi >= 1
              ? "good"
              : claudeBundleHit.roi >= 0.85
              ? "warn"
              : "bad"
          }
        />
        <Stat
          label="的中優先AI 的中率"
          value={
            !claudeBundleHit || claudeBundleHit.participated_races === 0
              ? "—"
              : fmtPct(claudeBundleHit.hit_rate, 1)
          }
          hint={
            claudeBundleHit && claudeBundleHit.participated_races > 0
              ? `${claudeBundleHit.hits} 的中 / ${claudeBundleHit.participated_races} 参加`
              : "新スキーマ待ち (本日以降の analyze から蓄積)"
          }
          tone={
            !claudeBundleHit || claudeBundleHit.participated_races < 30
              ? "warn"
              : claudeBundleHit.hit_rate >= 0.3
              ? "good"
              : claudeBundleHit.hit_rate >= 0.15
              ? "warn"
              : "bad"
          }
        />
      </div>

      {/* 旧 Plan 別 ROI カードは廃止 (2026-05-29 後半): 集計対象を bundle 2 つだけに集約。 */}

      {/* 予測履歴 (最新予測 / 最新的中 / 本日全件) はダッシュボードから削除
          (2026-05-29 ユーザ指示: ダッシュボードページ内に予測履歴は不要)。
          /predictions ページや /calibrate ページから参照する。 */}
    </Page>
  );
}
