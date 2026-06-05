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
    // 3 列レイアウト (2026-05-29 ユーザ指示): 会場 Card を 3 列の grid。
    // 多会場日 (NAR 5-6 場開催等) でも 1 画面に収まりやすい。
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
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
                // 「的中」ラベルは **Plan T (3連単的中モード, 実弾投票束) のみ**で判定 (2026-06-06 特化)。
                // Plan T 束が無い旧 snapshot は旧実弾だった EV束 (bundle_hit) に fallback。
                // **bundleSkipped は anyHit より優先**: 賭けていない race は理論値が
                // 偶然立っていても「的中」ではない (見送り = 不参加)。
                const usePlanT = !!(hit && hit.plan_t_participated);
                const bundleSkipped = !!(
                  hit && !usePlanT && hit.bundle_participated === false
                );
                const anyHit =
                  !bundleSkipped &&
                  !!(hit && (usePlanT ? hit.plan_t_hit : hit.bundle_hit));
                const rowBg = hit
                  ? raceTimingRowBg(anyHit ? "good" : bundleSkipped ? "muted" : "bad")
                  : raceTimingRowBg(timing.tone);
                return (
                <li
                  key={`${p.race_id}-${p.saved_at}`}
                  className={`py-2.5 flex flex-col gap-1.5 hover:bg-(--color-panel-2) -mx-4 px-4 ${rowBg}`}
                >
                  {/* タイトル行: 会場 + R番号 + race_class を**独立した上段**に左上固定。
                      下段のコンテンツ/右側バッジの幅変動でタイトルが横ズレしないように、
                      この行には他の要素を一切置かない。race_class が空 (園田など) の場合は
                      Badge を非表示 (空チップが出ないように)。 */}
                  <Link
                    href={`/predictions/${p.race_id}`}
                    className="flex items-center gap-2 self-start hover:underline w-fit"
                  >
                    {p.venue_name && (
                      <span className="text-sm font-bold text-(--color-foreground)">
                        {p.venue_name}
                      </span>
                    )}
                    <span className="text-xl font-bold tabnum leading-none">
                      {p.race_number}R
                    </span>
                    {p.race_class && (
                      <Badge tone={raceClassTone(p.race_class)}>{p.race_class}</Badge>
                    )}
                  </Link>
                  {/* コンテンツ行: 締切/発走・保存・hit info を左、状態バッジ列を右に。 */}
                  <div className="flex flex-wrap md:flex-nowrap items-start gap-3">
                  <Link
                    href={`/predictions/${p.race_id}`}
                    className="flex-1 min-w-0"
                  >
                    <div className="min-w-0">
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
                          {hit.plan_t_hit && (
                            <Badge tone="magenta">Plan T 的中</Badge>
                          )}
                          {hit.bundle_hit && (
                            <Badge tone={usePlanT ? "muted" : "good"}>
                              EV束{usePlanT ? "(参考)" : ""}的中{(hit.bundle_hit_bet_types?.length ?? 0) > 0
                                ? ` (${hit.bundle_hit_bet_types!.map((bt) => BET_LABELS[bt] ?? bt).join("/")})`
                                : ""}
                            </Badge>
                          )}
                          {bundleSkipped && (
                            // 見送り (束が空) は「不的中」ではなく「見送り」表示
                            <Badge tone="muted">束 見送り</Badge>
                          )}
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
                  {/* 状態バッジ + 詳細リンクは固定高さ (h-6) の帯に **items-center** で
                      まとめる。content 行は items-start なので各行のこの帯の上端は行上端で
                      揃い、帯内中央寄せにより全行でバッジ/詳細が一直線に並ぶ (左カラムの
                      1 行目高さ=締切/保存 が行ごとに違っても右側はズレない)。 */}
                  <div className="flex items-center gap-2 shrink-0 h-6">
                    {/* **1 ラベル原則** (2026-05-29 ユーザ指示):
                        優先順 hit (的中/見送り/不的中) > 補強済 > timing。
                        timing.label="結果待ち" は 評価待ち 文脈で冗長なので非表示。 */}
                    {hit ? (
                      anyHit ? (
                        <Badge tone="good">的中</Badge>
                      ) : bundleSkipped ? (
                        <Badge tone="muted">見送り</Badge>
                      ) : (
                        <Badge tone="bad">不的中</Badge>
                      )
                    ) : p.has_evidence ? (
                      <Badge tone="magenta">補強済</Badge>
                    ) : timing.label !== "結果待ち" ? (
                      <Badge tone={timing.tone}>{timing.label}</Badge>
                    ) : null}
                    <Link
                      href={`/predictions/${p.race_id}`}
                      className="text-xs text-(--color-accent) hover:underline whitespace-nowrap"
                    >
                      詳細 →
                    </Link>
                  </div>
                  </div>
                </li>
                );
              })}
            </ul>
          </Card>
        );
      })}
    </div>
  );
}
