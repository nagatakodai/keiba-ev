"use client";

// オッズ変動タイムライン (client island)。
// `data/cache/odds_timeline/<race_id>.jsonl` 由来の GET /api/timeline/{race_id} を
// mount 時に 1 回 fetch し、score (締切5-7分前) / bet (締切1-2.5分前) / poll
// (odds_capture daemon) の各 capture 行から **単勝オッズの推移** を馬ごとに描く。
// タイムライン未取得 (404) のレースでは控えめな注記のみ表示する。
// late_money (snapshot の paper 計測 — 確率/束には未使用) があれば
// 「直前に売れた / ドリフトした」馬の表も併記する。
import { useEffect, useMemo, useState } from "react";
import {
  ChartLine,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
} from "lucide-react";
import { api, type TimelineResponse } from "@/lib/api";
import { Badge, type BadgeTone, Card } from "@/components/ui";
import { OddsTimeline, type OddsHorse } from "@/components/charts";
import { Skeleton } from "@/components/Skeleton";

// snapshot (PredictionDetail) の late_money フィールド (src/analyze.py が保存)。
// api.ts の型には未定義のため、ここで局所定義して page.tsx から cast して渡す。
export type LateMoneySnapshot = {
  score_stage?: string | null;          // "score" | "poll" (基準行の stage)
  score_captured_at?: string | null;    // 基準行の取得時刻 (ISO JST naive)
  gap_min?: number | null;              // 基準行 → bet 段の経過分
  ratio?: Record<string, number> | null; // 馬番str → bet/score 単勝オッズ比
  source_mix?: boolean | null;          // 経路混在 (券種集合不一致) フラグ
};

const STAGE_JP: Record<string, string> = {
  score: "score 段",
  bet: "bet 段",
  poll: "poll",
};

const STAGE_TONE: Record<string, BadgeTone> = {
  score: "info",
  bet: "good",
  poll: "muted",
};

// ISO JST naive ("YYYY-MM-DDTHH:MM:SS") → "HH:MM"。不正形式はそのまま返す。
function hhmm(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = iso.match(/[T\s](\d{2}:\d{2})/);
  return m ? m[1] : iso;
}

// 2 つの ISO JST naive の経過分。parse 不能なら null。
function gapMinutes(a: string | null | undefined, b: string | null | undefined): number | null {
  if (!a || !b) return null;
  const ta = Date.parse(a.replace(" ", "T"));
  const tb = Date.parse(b.replace(" ", "T"));
  if (Number.isNaN(ta) || Number.isNaN(tb)) return null;
  return (tb - ta) / 60_000;
}

const SHORTEN_T = 0.95; // ratio < 0.95 = 直前に売れた (オッズ短縮 = 資金流入)
const DRIFT_T = 1.05;   // ratio > 1.05 = 直前に流出 (オッズドリフト)

export function OddsTimelineCard({
  raceId,
  horseNames,
  finish,
  lateMoney,
}: {
  raceId: string;
  // 馬番 (str) → 馬名。snapshot の horse_aptitude / index_compare から構築。
  horseNames?: Record<string, string>;
  // result.finish_order (snapshot 側)。timeline 側 result があればそちらを優先。
  finish?: number[];
  lateMoney?: LateMoneySnapshot | null;
}) {
  // undefined = loading / null = 未取得 (404 等) / TimelineResponse = 取得済
  const [tl, setTl] = useState<TimelineResponse | null | undefined>(undefined);
  const [logScale, setLogScale] = useState(true);

  useEffect(() => {
    let alive = true;
    api
      .getTimeline(raceId)
      .then((res) => {
        if (alive) setTl(res);
      })
      .catch(() => {
        // タイムライン未取得 race は 404 → 「データなし」扱い (api.ts のコメント準拠)
        if (alive) setTl(null);
      });
    return () => {
      alive = false;
    };
  }, [raceId]);

  const rows = useMemo(
    () => (tl?.rows ?? []).filter((r) => r.odds?.win && Object.keys(r.odds.win).length > 0),
    [tl],
  );

  // 馬リスト: 全行の win オッズキーの和集合 (馬番昇順)。
  const horses: OddsHorse[] = useMemo(() => {
    const nums = new Set<string>();
    for (const r of rows) for (const k of Object.keys(r.odds.win ?? {})) nums.add(k);
    return [...nums]
      .sort((a, b) => Number(a) - Number(b))
      .map((n) => ({
        key: n,
        label: horseNames?.[n] ? `${n} ${horseNames[n]}` : `馬${n}`,
      }));
  }, [rows, horseNames]);

  // チャート行: x = "HH:MM stage" + 馬番ごとの単勝オッズ。
  const data = useMemo(
    () =>
      rows.map((r) => ({
        x: `${hhmm(r.captured_at)} ${STAGE_JP[r.stage] ?? r.stage}`,
        ...(r.odds.win ?? {}),
      })),
    [rows],
  );

  const finishOrder = tl?.result?.finish_order?.length
    ? tl.result.finish_order
    : finish ?? [];
  const topFinishers = finishOrder.slice(0, 3).map(String);

  // late money: 短縮 (売れた) / ドリフト (流出) のみ抽出して変化率順に。
  const lmRatio = lateMoney?.ratio ?? null;
  const lmMoved = useMemo(() => {
    if (!lmRatio) return [];
    return Object.entries(lmRatio)
      .filter(([, r]) => r < SHORTEN_T || r > DRIFT_T)
      .sort((a, b) => a[1] - b[1]); // 最短縮 (流入大) が先頭
  }, [lmRatio]);

  // loading 中: 軽量スケルトン (CLS を抑えるため枠だけ出す)。
  if (tl === undefined) {
    return (
      <Card
        title={
          <span className="flex items-center gap-2">
            <ChartLine className="w-4 h-4 text-(--color-info) shrink-0" aria-hidden />
            <span>オッズ変動タイムライン</span>
          </span>
        }
      >
        <Skeleton className="h-40 w-full" />
      </Card>
    );
  }

  // 404 / capture 行なし: 控えめな注記のみ (旧 snapshot や capture 前のレース)。
  if (!rows.length && lmMoved.length === 0) {
    return (
      <p className="text-[11px] text-(--color-muted) flex items-center gap-1.5 px-1">
        <ChartLine className="w-3.5 h-3.5 shrink-0" aria-hidden />
        オッズタイムライン未取得 (score/bet/poll 段の capture なし)
      </p>
    );
  }

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <ChartLine className="w-4 h-4 text-(--color-info) shrink-0" aria-hidden />
          <span>オッズ変動タイムライン</span>
          <span className="text-xs text-(--color-muted) font-normal">
            単勝オッズの capture 推移 (score → bet → poll) · 追加 fetch なしの実測 ·
            上位3着は金/銀/銅で強調
          </span>
        </span>
      }
      right={
        rows.length > 0 ? (
          <button
            type="button"
            onClick={() => setLogScale((v) => !v)}
            className={`px-2 py-0.5 rounded-md text-[11px] font-bold border transition-colors ${
              logScale
                ? "bg-sky-500/15 text-sky-300 border-sky-500/40"
                : "bg-(--color-surface-2) text-(--color-muted) border-(--color-line) hover:text-(--color-foreground)"
            }`}
            title="オッズは裾が重いため log 軸が既定。クリックで linear に切替"
          >
            {logScale ? "log 軸" : "linear 軸"}
          </button>
        ) : undefined
      }
    >
      {rows.length > 0 && (
        <>
          {/* capture 行のステージバッジ + 行間ギャップ (分) */}
          <div className="flex items-center gap-1.5 flex-wrap mb-3">
            {rows.map((r, i) => {
              const gap = i > 0 ? gapMinutes(rows[i - 1].captured_at, r.captured_at) : null;
              return (
                <span key={`${r.stage}-${r.captured_at}-${i}`} className="flex items-center gap-1.5">
                  {gap != null && gap >= 0 && (
                    <span className="text-[10px] text-(--color-muted) tnum">+{gap.toFixed(1)}分</span>
                  )}
                  <Badge tone={STAGE_TONE[r.stage] ?? "muted"}>
                    {STAGE_JP[r.stage] ?? r.stage}{" "}
                    <span className="tnum font-normal ml-0.5">{hhmm(r.captured_at)}</span>
                  </Badge>
                </span>
              );
            })}
            {rows.length === 1 && (
              <span className="text-[10px] text-(--color-muted)">capture 1 点のみ (推移なし)</span>
            )}
          </div>
          <OddsTimeline
            data={data}
            horses={horses}
            xKey="x"
            height={280}
            logScale={logScale}
            topFinishers={topFinishers}
          />
        </>
      )}

      {lmMoved.length > 0 && (
        <div className="mt-4">
          <div className="text-[10px] font-bold tracking-widest uppercase text-(--color-muted) mb-1.5 flex items-center gap-1.5">
            Late money (score → bet 単勝オッズ比)
            {lateMoney?.gap_min != null && (
              <span className="tnum normal-case tracking-normal">
                · 基準 {STAGE_JP[lateMoney.score_stage ?? ""] ?? lateMoney.score_stage ?? "—"}{" "}
                {hhmm(lateMoney.score_captured_at)} から {lateMoney.gap_min.toFixed(1)} 分
              </span>
            )}
          </div>
          {lateMoney?.source_mix && (
            <p className="mb-2 text-[11px] text-amber-300 flex items-center gap-1.5">
              <TriangleAlert className="w-3.5 h-3.5 shrink-0" aria-hidden />
              経路混在 (score/bet で odds 源の券種集合が不一致) — 5% 未満の変動はノイズの可能性
            </p>
          )}
          <div className="overflow-x-auto rounded-lg border border-(--color-line) bg-(--color-surface-2)">
            <table className="w-full text-sm tnum">
              <thead className="text-left text-(--color-muted) text-xs">
                <tr className="border-b border-(--color-line)">
                  <th className="py-2 px-3 text-right">馬</th>
                  <th className="py-2 pr-3">馬名</th>
                  <th className="py-2 pr-3 text-right">bet/score 比</th>
                  <th className="py-2 pr-3">変化</th>
                </tr>
              </thead>
              <tbody>
                {lmMoved.map(([num, ratio]) => {
                  const shortened = ratio < SHORTEN_T;
                  return (
                    <tr key={num} className="border-b border-(--color-line)/60 last:border-b-0">
                      <td className="py-1.5 px-3 text-right font-bold">{num}</td>
                      <td className="py-1.5 pr-3">{horseNames?.[num] ?? "—"}</td>
                      <td
                        className={`py-1.5 pr-3 text-right font-bold ${
                          shortened ? "text-emerald-300" : "text-rose-300"
                        }`}
                      >
                        ×{ratio.toFixed(3)}
                      </td>
                      <td className="py-1.5 pr-3">
                        {shortened ? (
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[11px] font-bold bg-emerald-500/15 text-emerald-300 border-emerald-500/40">
                            <TrendingDown className="w-3 h-3" aria-hidden />
                            短縮 (直前に売れた)
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[11px] font-bold bg-rose-500/15 text-rose-300 border-rose-500/40">
                            <TrendingUp className="w-3 h-3" aria-hidden />
                            ドリフト (流出)
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <p className="mt-3 text-xs text-(--color-muted)">
        capture は解析パイプラインの取得済オッズを記録したもの (追加 fetch ゼロ =
        rate-limit リスクなし)。late money (締切直前のオッズ短縮) は informed money の
        痕跡候補としてペーパー計測中 — <b>確率・束には未使用</b> (arXiv:2509.14645)。
        比 &lt;{SHORTEN_T} を流入 (緑) / &gt;{DRIFT_T} を流出 (赤) として表示。
      </p>
    </Card>
  );
}
