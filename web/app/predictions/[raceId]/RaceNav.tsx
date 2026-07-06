import Link from "next/link";
import { api, type PredictionSummary } from "@/lib/api";
import { fmtTime } from "@/components/ui";

// 予測詳細画面の同日レースへの動線 (ユーザ指示 2026-07-06「他Rや他競馬場のRへの動線がほしい」)。
// 同じ日の予測がある全レースを 競馬場ごとの R チップ列 で表示し、1クリックで行き来できる。
// server component: list_predictions (最新400件) から同日分を抽出する。失敗時は描画しない
// (詳細ページ本体を壊さない)。

// 同日判定キー: start_at (JST) 優先、無ければ saved_at (JST ローカル ISO) の日付部。
function dayKey(p: { start_at: number | null; saved_at: string }): string {
  if (p.start_at && p.start_at > 0) {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Tokyo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(p.start_at * 1000));
  }
  return (p.saved_at ?? "").slice(0, 10);
}

export async function RaceNav({ currentId }: { currentId: string }) {
  let items: PredictionSummary[];
  try {
    items = (await api.listPredictions(400)).items;
  } catch {
    return null;
  }
  const cur = items.find((i) => i.race_id === currentId);
  if (!cur) return null; // 古いレース (最新400件の外) はナビ無しで従来表示
  const key = dayKey(cur);

  // 同日レースを競馬場ごとにグループ (同 race_id は最新保存の1件のみ)
  const seen = new Set<string>();
  const byVenue = new Map<string, PredictionSummary[]>();
  for (const it of items) {
    if (dayKey(it) !== key || seen.has(it.race_id)) continue;
    seen.add(it.race_id);
    const v = (it.venue_name || "").trim() || "?";
    const arr = byVenue.get(v) ?? [];
    arr.push(it);
    byVenue.set(v, arr);
  }
  if (seen.size <= 1) return null; // 同日が自分だけならナビ不要

  // 場は最初の発走が早い順、場内は R 昇順
  const venues = [...byVenue.entries()]
    .map(([v, arr]) => {
      arr.sort((a, b) => (a.race_number || 0) - (b.race_number || 0));
      const first = Math.min(
        ...arr.map((r) => (r.start_at && r.start_at > 0 ? r.start_at : Number.MAX_SAFE_INTEGER)),
      );
      return { v, arr, first };
    })
    .sort((a, b) => a.first - b.first);

  return (
    <nav
      aria-label="同日のレースへ移動"
      className="bg-(--color-card) border border-(--color-line) rounded-xl px-3 py-2 flex flex-col gap-1.5"
    >
      {venues.map(({ v, arr }) => (
        <div key={v} className="flex items-center gap-2 flex-wrap">
          <span className="text-[11px] font-bold tracking-wider text-(--color-muted) w-12 shrink-0">
            {v}
          </span>
          <div className="flex items-center gap-1 flex-wrap">
            {arr.map((r) => {
              const isCurrent = r.race_id === currentId;
              const hit = (r.hit_strategies?.length ?? 0) > 0;
              const label = r.race_number > 0 ? `${r.race_number}` : "?";
              const title = [
                `${v} ${label}R`,
                r.start_at && r.start_at > 0 ? `発走 ${fmtTime(r.start_at)}` : null,
                r.has_result ? (hit ? "結果あり・仮想的中" : "結果あり") : null,
                r.stage === "score" ? "暫定 (score段階)" : null,
              ]
                .filter(Boolean)
                .join(" · ");
              const cls = isCurrent
                ? "bg-(--color-accent) text-white border border-(--color-accent)"
                : r.has_result
                  ? "text-(--color-muted) border border-(--color-line) hover:border-(--color-accent) hover:text-(--color-foreground)"
                  : "text-(--color-foreground) border border-(--color-line) hover:border-(--color-accent)";
              return (
                <Link
                  key={r.race_id}
                  href={`/predictions/${encodeURIComponent(r.race_id)}`}
                  title={title}
                  aria-current={isCurrent ? "page" : undefined}
                  className={`relative inline-flex items-center justify-center min-w-7 h-7 px-1 rounded-md text-xs font-bold tabnum ${cls}`}
                >
                  {label}
                  {/* 仮想的中 (ダッシュボード仮想購入の的中券種あり) は緑ドット */}
                  {hit && (
                    <span
                      aria-hidden
                      className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-(--color-good)"
                    />
                  )}
                </Link>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );
}
