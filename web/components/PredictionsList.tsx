import Link from "next/link";
import { type CalibrationReport, type PredictionSummary } from "@/lib/api";

const BET_LABELS: Record<string, string> = {
  win: "単勝", place: "複勝", quinella: "馬連", wide: "ワイド",
  exacta: "馬単", trio: "3連複", trifecta: "3連単",
};
import {
  Badge,
  Card,
  fmtServerDateTime,
  fmtTime,
  planAccentClass,
  type PlanLetter,
  raceClassTone,
  raceTimingRowBg,
  raceTimingStatus,
} from "@/components/ui";

type RaceHit = CalibrationReport["races"][number];

function PlanHitTag({
  plan,
  hit,
}: {
  plan: PlanLetter;
  hit: boolean;
}) {
  return hit ? (
    <span className={`font-bold ${planAccentClass(plan)}`}>{plan} ✓</span>
  ) : (
    <span className="font-bold text-(--color-muted)">{plan} ×</span>
  );
}

function groupByVenue(
  items: PredictionSummary[],
): Array<[string, PredictionSummary[]]> {
  const map = new Map<string, PredictionSummary[]>();
  const order: string[] = [];
  for (const p of items) {
    const v = p.venue_name || "(不明)";
    if (!map.has(v)) {
      map.set(v, []);
      order.push(v);
    }
    map.get(v)!.push(p);
  }
  for (const v of order) {
    map.get(v)!.sort((a, b) => a.race_number - b.race_number);
  }
  return order.map((v) => [v, map.get(v)!]);
}

// 予測一覧を会場ごとにまとめて描画する共有コンポーネント (Server-rendered)。
// /predictions と /predictions/archive で同じ見た目を使い回す。
//
// raceHitMap が渡された場合 (= calibration から的中情報が取れた場合) かつ
// showHits=true のときは、Plan A/B/C の的中・払戻を表示する。
export function PredictionsList({
  items,
  nowMs,
  raceHitMap,
  closeAtMap,
  startAtMap,
  showHits = true,
  emptyMessage = "まだ予測はありません。",
}: {
  items: PredictionSummary[];
  nowMs: number;
  raceHitMap?: Map<string, RaceHit>;
  // snapshot に close_at/start_at が無い (claude 評価未完了 / 旧 snapshot) 場合の
  // watch-auto history 由来 fallback。fresh fetch でも履歴は届くので、ユーザが
  // 「結果待ち」「時刻不明」を見ずに済む。
  closeAtMap?: Map<string, number>;
  startAtMap?: Map<string, number>;
  showHits?: boolean;
  emptyMessage?: string;
}) {
  if (items.length === 0) {
    return (
      <Card>
        <p className="text-sm text-(--color-muted)">{emptyMessage}</p>
      </Card>
    );
  }
  const groups = groupByVenue(items);
  return (
    <>
      {groups.map(([venue, races]) => {
        const latestSavedAt = races
          .map((r) => r.saved_at)
          .filter(Boolean)
          .sort()
          .at(-1);
        return (
          <Card
            key={venue}
            title={
              <span className="flex items-center gap-2">
                <span className="font-semibold">{venue}</span>
                <Badge tone="muted">{races.length}R</Badge>
              </span>
            }
            right={
              latestSavedAt ? (
                <span className="text-xs text-(--color-muted) tabnum">
                  最新 {fmtServerDateTime(latestSavedAt)}
                </span>
              ) : undefined
            }
          >
            <ul className="divide-y divide-(--color-line)">
              {races.map((p) => {
                const closeAt = p.close_at ?? closeAtMap?.get(p.race_id) ?? null;
                const startAt = p.start_at ?? startAtMap?.get(p.race_id) ?? null;
                const timing = raceTimingStatus(
                  closeAt,
                  startAt,
                  p.has_result,
                  nowMs,
                );
                const hit = showHits ? raceHitMap?.get(p.race_id) : undefined;
                const anyHit = !!(
                  hit &&
                  (hit.bundle_hit ||   // 総合オススメ束 (ワイド/馬連等を含む) の的中も的中扱い
                    hit.plan_a_hit ||
                    hit.plan_b_hit ||
                    hit.plan_c_hit ||
                    hit.plan_g_hit ||
                    hit.plan_h1_hit ||
                    hit.plan_h2_hit ||
                    hit.plan_f_hit)
                );
                // Claude 総合オススメが「見送り」(束 legs 空) で、他 Plan も全て外し / 不在 の race は
                // 「不的中」(=賭けて外れた) ではなく「未参加」(=賭けていない) として中立色で表示する。
                const bundleSkipped = !!(hit && hit.bundle_participated === false);
                const rowBg = hit
                  ? raceTimingRowBg(anyHit ? "good" : bundleSkipped ? "muted" : "bad")
                  : raceTimingRowBg(timing.tone);
                return (
                <li
                  key={`${p.race_id}-${p.saved_at}`}
                  className={`py-2.5 flex flex-wrap md:flex-nowrap items-start gap-3 hover:bg-(--color-panel-2) -mx-4 px-4 ${rowBg}`}
                >
                  <Link
                    href={`/predictions/${p.race_id}`}
                    className="flex items-start gap-3 flex-1 min-w-0"
                  >
                    <div className="w-12 text-center shrink-0">
                      <div className="text-2xl font-bold tabnum leading-none">
                        {p.race_number}
                      </div>
                      <div className="text-[10px] text-(--color-muted) mt-0.5">R</div>
                    </div>
                    <div className="shrink-0">
                      <Badge tone={raceClassTone(p.race_class)}>{p.race_class}</Badge>
                    </div>
                    <div className="flex-1 min-w-0">
                      {(closeAt != null || startAt != null) && (
                        <div className="text-sm tabnum font-bold leading-tight flex flex-wrap items-baseline gap-x-2 gap-y-0">
                          <span>
                            <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase mr-1">締切</span>
                            <span className="mono">{fmtTime(closeAt)}</span>
                          </span>
                          <span className="text-(--color-muted) font-normal">→</span>
                          <span>
                            <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase mr-1">発走</span>
                            <span className="mono">{fmtTime(startAt)}</span>
                          </span>
                        </div>
                      )}
                      <div className="text-[11px] text-(--color-muted) tabnum">
                        保存 {fmtServerDateTime(p.saved_at)}
                      </div>
                      {hit ? (
                        <div className="text-xs tabnum mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                          <span>
                            着順{" "}
                            <span className="font-bold mono text-(--color-foreground)">
                              {hit.finish.join("-")}
                            </span>
                          </span>
                          <span className="text-(--color-muted)">·</span>
                          {hit.bundle_hit && (
                            <Badge tone="good">
                              束的中{(hit.bundle_hit_bet_types?.length ?? 0) > 0
                                ? ` (${hit.bundle_hit_bet_types!.map((bt) => BET_LABELS[bt] ?? bt).join("/")})`
                                : ""}
                            </Badge>
                          )}
                          {bundleSkipped && (
                            // 見送り (Claude 総合オススメが空束) は「不的中」ではなく「見送り」表示
                            <Badge tone="muted">束 見送り</Badge>
                          )}
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
                        <div className="text-xs text-(--color-muted) tabnum mt-0.5 flex flex-wrap gap-x-1.5">
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
                      {p.top_aptitude && p.top_aptitude.length > 0 && (
                        <div className="text-[11px] tabnum mt-0.5 flex flex-wrap gap-x-2">
                          <span className="text-(--color-muted) font-bold tracking-wider uppercase">
                            適性
                          </span>
                          {p.top_aptitude.map((a, i) => (
                            <span key={a.number} className={i === 0 ? "font-bold" : ""}>
                              <span className="text-(--color-muted)">
                                {i === 0 ? "◎" : i === 1 ? "○" : "▲"}
                              </span>
                              {a.number} {a.name}
                              <span className="text-(--color-muted) ml-0.5">
                                ({a.total.toFixed(0)})
                              </span>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </Link>
                  <div className="flex gap-1 shrink-0 items-start">
                    {hit ? (
                      anyHit ? (
                        <Badge tone="good">的中</Badge>
                      ) : bundleSkipped ? (
                        // 見送り = 賭けてないので「不的中」ではなく「見送り」
                        <Badge tone="muted">見送り</Badge>
                      ) : (
                        <Badge tone="bad">不的中</Badge>
                      )
                    ) : (
                      <Badge tone={timing.tone}>{timing.label}</Badge>
                    )}
                    {/* has_evidence=true なら Claude 評価が完了 (検索補強反映済)、
                        false なら未完了 (分析途中・失敗・cancel いずれか) */}
                    {p.has_evidence ? (
                      <Badge tone="magenta">補強済</Badge>
                    ) : !p.has_result ? (
                      <Badge tone="muted">評価待ち</Badge>
                    ) : null}
                  </div>
                  <Link
                    href={`/predictions/${p.race_id}`}
                    className="text-xs text-(--color-accent) hover:underline shrink-0"
                  >
                    詳細 →
                  </Link>
                </li>
                );
              })}
            </ul>
          </Card>
        );
      })}
    </>
  );
}
