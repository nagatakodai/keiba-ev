import Link from "next/link";
import {
  CalendarDays,
  ChevronRight,
  CircleCheck,
  CircleSlash,
  CircleX,
  Eye,
  EyeOff,
  Inbox,
  Sparkles,
} from "lucide-react";
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
  raceClassTone,
  raceTimingRowBg,
  raceTimingStatus,
  savedAtDate,
} from "@/components/ui";

type RaceHit = CalibrationReport["races"][number];

// 的中表示 ON/OFF の segmented control。Server Component のまま Link 遷移で
// `?hits=off` を切り替える (client 化せず、従来の RSC roundtrip 機構を維持)。
export function HitsToggle({
  basePath,
  showHits,
}: {
  basePath: string;
  showHits: boolean;
}) {
  const seg =
    "inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-bold transition-colors";
  return (
    <div
      className="inline-flex items-center p-0.5 rounded-lg bg-(--color-surface-2) border border-(--color-line)"
      role="group"
      aria-label="的中表示切替"
    >
      <Link
        href={basePath}
        aria-pressed={showHits}
        className={`${seg} ${
          showHits
            ? "bg-emerald-500/20 text-emerald-300 shadow-[inset_0_0_0_1px_rgba(52,211,153,0.4)]"
            : "text-(--color-muted) hover:text-(--color-foreground)"
        }`}
      >
        <Eye className="size-3.5" />
        的中 ON
      </Link>
      <Link
        href={`${basePath}?hits=off`}
        aria-pressed={!showHits}
        className={`${seg} ${
          !showHits
            ? "bg-(--color-surface-3) text-(--color-foreground) shadow-[inset_0_0_0_1px_rgba(148,163,184,0.3)]"
            : "text-(--color-muted) hover:text-(--color-foreground)"
        }`}
      >
        <EyeOff className="size-3.5" />
        OFF
      </Link>
    </div>
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

// アーカイブ用: saved_at の日付 (JST "YYYY-MM-DD") ごとに新しい順でまとめる。
function groupItemsByDate(
  items: PredictionSummary[],
): Array<[string, PredictionSummary[]]> {
  const map = new Map<string, PredictionSummary[]>();
  for (const p of items) {
    const d = savedAtDate(p.saved_at) || "(日付不明)";
    if (!map.has(d)) map.set(d, []);
    map.get(d)!.push(p);
  }
  return [...map.entries()].sort(([a], [b]) => (a < b ? 1 : a > b ? -1 : 0));
}

type ListCommonProps = {
  nowMs: number;
  raceHitMap?: Map<string, RaceHit>;
  closeAtMap?: Map<string, number>;
  startAtMap?: Map<string, number>;
  showHits: boolean;
};

function RaceRow({
  p,
  nowMs,
  raceHitMap,
  closeAtMap,
  startAtMap,
  showHits,
}: ListCommonProps & { p: PredictionSummary }) {
  const closeAt = p.close_at ?? closeAtMap?.get(p.race_id) ?? null;
  const startAt = p.start_at ?? startAtMap?.get(p.race_id) ?? null;
  const timing = raceTimingStatus(closeAt, startAt, p.has_result, nowMs);
  const hit = showHits ? raceHitMap?.get(p.race_id) : undefined;
  // 「的中」ラベルは**実弾投票束**で判定: EV束計測対象 (ev_measured,
  // 2026-06-10〜 実弾既定束) は EV束 (bundle_*)、それ以前は 3連単束
  // (無ければ旧実弾だった EV束に fallback)。
  // **bundleSkipped は anyHit より優先**: 賭けていない race は理論値が
  // 偶然立っていても「的中」ではない (見送り = 不参加)。
  const useEv = !!(hit && hit.ev_measured);
  const useTrifecta = !useEv && !!(hit && hit.trifecta_bundle_participated);
  const bundleSkipped = !!(
    hit &&
    (useEv
      ? hit.bundle_participated === false
      : !useTrifecta && hit.bundle_participated === false)
  );
  const anyHit =
    !bundleSkipped &&
    !!(hit &&
      (useEv
        ? hit.bundle_hit
        : useTrifecta
          ? hit.trifecta_bundle_hit
          : hit.bundle_hit));
  const rowBg = hit
    ? raceTimingRowBg(anyHit ? "good" : bundleSkipped ? "muted" : "bad")
    : raceTimingRowBg(timing.tone);
  return (
    <li
      className={`group rounded-lg border border-(--color-line-soft) hover:border-(--color-line) transition-colors px-3 py-2.5 ${
        rowBg || "hover:bg-(--color-surface-2)"
      }`}
    >
      {/* タイトル行: 会場 + R番号 (mono で大きく) + race_class。状態バッジは右端固定。
          race_class が空 (園田など) の場合は Badge を非表示 (空チップが出ないように)。 */}
      <div className="flex items-center justify-between gap-2">
        <Link
          href={`/predictions/${p.race_id}`}
          className="flex items-center gap-2 min-w-0 hover:underline"
        >
          {p.venue_name && (
            <span className="text-sm font-bold text-(--color-foreground) truncate">
              {p.venue_name}
            </span>
          )}
          <span className="mono text-xl font-black tnum leading-none shrink-0">
            {p.race_number}
            <span className="text-[11px] font-bold text-(--color-muted) ml-0.5">
              R
            </span>
          </span>
          {p.race_class && (
            <Badge tone={raceClassTone(p.race_class)}>{p.race_class}</Badge>
          )}
          {/* score 段の暫定プレビュー (Claude 指数出力時に早出し)。bet 段で確定版に上書きされる。 */}
          {p.stage === "score" && <Badge tone="muted">暫定</Badge>}
        </Link>
        {/* **1 ラベル原則** (2026-05-29 ユーザ指示):
            優先順 hit (的中/見送り/不的中) > 補強済 > timing。
            timing.label="結果待ち" は 評価待ち 文脈で冗長なので非表示。 */}
        <div className="shrink-0">
          {hit ? (
            anyHit ? (
              <Badge tone="good">
                <CircleCheck className="size-3 mr-0.5" />
                的中
              </Badge>
            ) : bundleSkipped ? (
              <Badge tone="muted">
                <CircleSlash className="size-3 mr-0.5" />
                見送り
              </Badge>
            ) : (
              <Badge tone="bad">
                <CircleX className="size-3 mr-0.5" />
                不的中
              </Badge>
            )
          ) : p.has_evidence ? (
            <Badge tone="magenta">
              <Sparkles className="size-3 mr-0.5" />
              補強済
            </Badge>
          ) : timing.label !== "結果待ち" ? (
            <Badge tone={timing.tone}>{timing.label}</Badge>
          ) : null}
        </div>
      </div>
      {/* コンテンツ行: 締切/発走・保存・hit info・適性チップを左、詳細リンクを右下に。 */}
      <div className="mt-1.5 flex items-end justify-between gap-3">
        <Link href={`/predictions/${p.race_id}`} className="flex-1 min-w-0 block">
          {(closeAt != null || startAt != null) && (
            <div className="text-sm tnum font-bold leading-tight flex flex-wrap items-baseline gap-x-2 gap-y-0">
              <span>
                <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase mr-1">
                  締切
                </span>
                <span className="mono">{fmtTime(closeAt)}</span>
              </span>
              <span className="text-(--color-muted) font-normal">→</span>
              <span>
                <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase mr-1">
                  発走
                </span>
                <span className="mono">{fmtTime(startAt)}</span>
              </span>
            </div>
          )}
          <div className="text-[11px] text-(--color-muted) tnum">
            保存 {fmtServerDateTime(p.saved_at)}
          </div>
          {hit ? (
            <div className="text-xs tnum mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
              <span>
                着順{" "}
                <span className="font-bold mono text-(--color-foreground)">
                  {hit.finish.join("-")}
                </span>
              </span>
              <span className="text-(--color-muted)">·</span>
              {hit.trifecta_bundle_hit && (
                <Badge tone={useEv ? "muted" : "magenta"}>
                  3連単束{useEv ? "(参考)" : ""} 的中
                </Badge>
              )}
              {hit.bundle_hit && (
                <Badge tone={useTrifecta ? "muted" : "good"}>
                  EV束{useTrifecta ? "(参考)" : ""}的中
                  {(hit.bundle_hit_bet_types?.length ?? 0) > 0
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
                  <span className="font-bold tnum text-emerald-300">
                    ¥{hit.payout.toLocaleString()}
                  </span>
                </>
              )}
            </div>
          ) : (
            <div className="text-xs text-(--color-muted) tnum mt-0.5 flex flex-wrap gap-x-1.5">
              <span>候補 {p.row_count}</span>
            </div>
          )}
          {p.top_aptitude && p.top_aptitude.length > 0 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1">
              <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase mr-0.5">
                適性
              </span>
              {p.top_aptitude.map((a, i) => (
                <span
                  key={a.number}
                  className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[11px] tnum ${
                    i === 0
                      ? "bg-amber-500/10 border-amber-500/30 text-amber-200 font-bold"
                      : "bg-(--color-surface-2) border-(--color-line-soft) text-(--color-foreground)"
                  }`}
                >
                  <span
                    className={i === 0 ? "text-amber-300" : "text-(--color-muted)"}
                  >
                    {i === 0 ? "◎" : i === 1 ? "○" : "▲"}
                  </span>
                  <span className="mono font-bold">{a.number}</span>
                  <span className="max-w-[7em] truncate">{a.name}</span>
                  <span className="text-(--color-muted)">
                    {a.total.toFixed(0)}
                  </span>
                </span>
              ))}
            </div>
          )}
        </Link>
        <Link
          href={`/predictions/${p.race_id}`}
          className="shrink-0 inline-flex items-center gap-0.5 text-xs font-bold text-(--color-accent) opacity-80 group-hover:opacity-100 hover:underline whitespace-nowrap"
        >
          詳細
          <ChevronRight className="size-3.5" />
        </Link>
      </div>
    </li>
  );
}

// 会場ごとの Card grid。3 列レイアウト (2026-05-29 ユーザ指示):
// 多会場日 (NAR 5-6 場開催等) でも 1 画面に収まりやすい。
function VenueGrid({
  items,
  ...common
}: ListCommonProps & { items: PredictionSummary[] }) {
  const groups = groupByVenue(items);
  return (
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
                <span className="text-xs text-(--color-muted) tnum">
                  最新 {fmtServerDateTime(latestSavedAt)}
                </span>
              ) : undefined
            }
          >
            <ul className="space-y-2">
              {races.map((p) => (
                <RaceRow key={`${p.race_id}-${p.saved_at}`} p={p} {...common} />
              ))}
            </ul>
          </Card>
        );
      })}
    </div>
  );
}

// 予測一覧を会場ごとにまとめて描画する共有コンポーネント (Server-rendered)。
// /predictions と /predictions/archive で同じ見た目を使い回す。
//
// raceHitMap が渡された場合 (= calibration から的中情報が取れた場合) かつ
// showHits=true のときは、実弾投票束の的中・払戻を表示する。
// groupByDate=true (アーカイブ) なら日付セクションで区切る。
export function PredictionsList({
  items,
  nowMs,
  raceHitMap,
  closeAtMap,
  startAtMap,
  showHits = true,
  groupByDate = false,
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
  groupByDate?: boolean;
  emptyMessage?: string;
}) {
  if (items.length === 0) {
    return (
      <Card>
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <Inbox className="size-8 text-(--color-muted)/60" />
          <p className="text-sm text-(--color-muted)">{emptyMessage}</p>
        </div>
      </Card>
    );
  }
  const common: ListCommonProps = {
    nowMs,
    raceHitMap,
    closeAtMap,
    startAtMap,
    showHits,
  };
  if (!groupByDate) {
    return <VenueGrid items={items} {...common} />;
  }
  const dateGroups = groupItemsByDate(items);
  return (
    <div className="space-y-6">
      {dateGroups.map(([date, dayItems]) => (
        <section key={date} className="space-y-3">
          <header className="flex items-center gap-2">
            <CalendarDays className="size-3.5 text-(--color-muted)" />
            <span className="text-[11px] font-bold tracking-widest uppercase text-(--color-muted) tnum">
              {date}
            </span>
            <span className="text-[11px] text-(--color-muted) tnum">
              {dayItems.length} 件
            </span>
            <span className="flex-1 h-px bg-(--color-line-soft)" />
          </header>
          <VenueGrid items={dayItems} {...common} />
        </section>
      ))}
    </div>
  );
}
