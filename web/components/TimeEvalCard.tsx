// タイム換算比較カード (予測詳細ページの主役カード, 換算モデル te1〜)。
//
// snapshot の `time_eval` を表示する: 各馬の過去走の走破時計を当レース条件 (同場・同馬場・
// 同距離・同馬場状態) に換算し、集約した「推定タイム」の速い順に並べる決定論評価。
// 同条件の走は生時計のまま、それ以外は換算モデルで当条件へ寄せる。横に Claude 指数の順位と
// 市場指数の順位 (= 人気順) を並べ、3 つの見立てを一目で比較できるようにする。
// server component (client JS なし)。各馬の換算内訳はネイティブ <details> で展開する。
import { CircleCheck, Timer, TriangleAlert } from "lucide-react";
import { Badge, type BadgeTone, Card } from "@/components/ui";
import type { PredictionDetail, TimeEval, TimeEvalHorse, TimeEvalRun } from "@/lib/api";
import { fmtRaceTime, fmtSignedSec } from "@/lib/raceTime";

type IndexCompareRow = NonNullable<PredictionDetail["index_compare"]>[number];

// 予測詳細ページ (app/predictions/[raceId]/page.tsx) の HIT_ROW_BG / HitMark と同じ見た目。
const HIT_ROW_BG = "bg-emerald-500/10";
// 馬ごとに「本行 + 内訳行」の 2 行を 1 組で塗るため、.table-zebra (行単位の縞) は使わず
// 同じ色を手動で敷く。hover も組単位 (tbody.group) で .table-zebra と同色。
const ZEBRA_BG = "bg-[rgba(148,163,184,0.045)]";
const HOVER_BG = "group-hover:bg-[rgba(52,211,153,0.07)]";
// 本表の列数 (内訳行の colSpan 計算用)。
const N_COLS = 12;
// Claude / 市場の順位がタイム順位とこれ以上ずれたら色で示す。
const RANK_GAP_MARK = 3;

function HitMark({ place }: { place: number }) {
  return (
    <span className="ml-2 inline-flex items-center gap-0.5 align-middle text-emerald-300 text-xs font-bold whitespace-nowrap">
      <CircleCheck className="w-3.5 h-3.5 shrink-0" aria-hidden />
      <span className="tnum">{place}着</span>
    </span>
  );
}

type KindMeta = { label: string; tone: BadgeTone; title: string };

const KIND_META = {
  same: { label: "同条件", tone: "good", title: "同じ競馬場・馬場・距離 — 走破時計をそのまま比較" },
  same_dist: { label: "同距離", tone: "info", title: "他場の同じ馬場・距離 — 場の差を換算" },
  distance: { label: "距離換算", tone: "default", title: "同じ馬場の別距離 — 距離差を換算" },
  surface: { label: "芝ダ換算", tone: "warn", title: "芝↔ダートの換算 (信頼度低)" },
  excluded: {
    label: "対象外",
    tone: "muted",
    title: "換算対象外 (障害 / ばんえい / 海外 / 時計欠落 / コース不明)",
  },
} satisfies Record<TimeEvalRun["kind"], KindMeta>;

const KIND_ORDER: readonly string[] = ["same", "same_dist", "distance", "surface", "excluded"];

// 未知の kind (backend 側の将来拡張) でも落ちないよう muted で素通しする。
function kindMeta(kind: string): KindMeta {
  return (
    (KIND_META as Record<string, KindMeta | undefined>)[kind] ?? {
      label: kind || "—",
      tone: "muted",
      title: "",
    }
  );
}

const CONF_META = {
  high: { label: "高", tone: "good" },
  mid: { label: "中", tone: "default" },
  low: { label: "低", tone: "warn" },
  none: { label: "—", tone: "muted" },
} satisfies Record<TimeEvalHorse["confidence"], { label: string; tone: BadgeTone }>;

function confMeta(c: string): { label: string; tone: BadgeTone } {
  return (
    (CONF_META as Record<string, { label: string; tone: BadgeTone } | undefined>)[c] ?? {
      label: "—",
      tone: "muted",
    }
  );
}

// "ダート" → "ダ" / "障害" → "障" / "芝" と "" はそのまま。
function surfaceAbbr(surface: string): string {
  if (surface.startsWith("ダ")) return "ダ";
  if (surface.startsWith("障")) return "障";
  return surface;
}

// 過去走の 芝ダ距離 (例 "ダ1400")。両方不明なら "—"。
function courseLabel(surface: string | null | undefined, distance: number | null | undefined): string {
  const d = distance != null && distance > 0 ? String(distance) : "";
  return `${surfaceAbbr(surface ?? "")}${d}` || "—";
}

// 当レース条件 (例 "大井 ダート1200m 良")。
function targetLabel(t: TimeEval["target"]): string {
  const course = t.distance > 0 ? `${t.surface}${t.distance}m` : t.surface;
  return [t.venue, course, t.going].filter(Boolean).join(" ");
}

// "2026-09-13" → "2026/09/13"
function fmtRunDate(date: string | null | undefined): string {
  return date ? date.replace(/-/g, "/") : "—";
}

// 指数 (高い = 上位) → 順位。同値は同順位 (1, 2, 2, 4)。値が無い馬は含めない。
function rankByIndex(
  rows: IndexCompareRow[],
  pick: (r: IndexCompareRow) => number | null | undefined,
): Map<number, number> {
  const vals: Array<[number, number]> = [];
  for (const r of rows) {
    const v = pick(r);
    if (v != null && Number.isFinite(v)) vals.push([r.number, v]);
  }
  return new Map(vals.map(([num, v]) => [num, 1 + vals.filter(([, w]) => w > v).length]));
}

function topNumbers(ranks: Map<number, number>): number[] {
  return [...ranks.entries()]
    .filter(([, r]) => r === 1)
    .map(([n]) => n)
    .sort((a, b) => a - b);
}

// タイム換算指数 (50 = レース中央値、1 秒速いごとに +10) のセル色。
function indexCellClass(v: number | null): string {
  if (v == null) return "text-(--color-muted)";
  if (v >= 65) return "text-(--color-good) font-bold";
  if (v >= 55) return "text-(--color-good)";
  if (v >= 45) return "";
  return "text-(--color-muted)";
}

function kindCountsText(runs: TimeEvalRun[]): string {
  const counts = new Map<string, number>();
  for (const r of runs) counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1);
  const order = [...KIND_ORDER, ...[...counts.keys()].filter((k) => !KIND_ORDER.includes(k))];
  return order
    .filter((k) => counts.has(k))
    .map((k) => `${kindMeta(k).label} ${counts.get(k)}`)
    .join(" · ");
}

function finishCell(pos: number | null | undefined) {
  if (pos == null || pos < 1 || pos > 3) {
    return (
      <span className="text-(--color-muted)" title="4着以下または不明">
        —
      </span>
    );
  }
  const cls = pos === 1 ? "text-amber-300" : pos === 2 ? "text-slate-200" : "text-orange-300/90";
  return <span className={`font-bold ${cls}`}>{pos}着</span>;
}

// 比較列 (Claude / 市場) の順位セル。タイム順位から RANK_GAP_MARK 以上ずれていれば色付け
// (緑 = その指標よりタイムの方が高評価 / 赤 = タイムの方が低評価)。
function CompareRankCell({
  rank,
  timeRank,
  who,
  topClass,
  first = false,
}: {
  rank: number | undefined;
  timeRank: number | null;
  who: string;
  topClass: string;
  first?: boolean;
}) {
  const base = `py-1.5 pr-3 text-right ${first ? "pl-4" : ""}`;
  if (rank == null) {
    return <td className={`${base} text-(--color-muted)`}>—</td>;
  }
  // 正 = その指標ではタイム順位より下位 (= タイムの方が高く評価している)。
  const delta = timeRank != null ? rank - timeRank : null;
  let color = rank === 1 ? topClass : "";
  let note = "";
  if (delta != null && delta >= RANK_GAP_MARK) {
    color = "text-emerald-300";
    note = ` — タイムの方が ${delta} 位上 (タイム高評価)`;
  } else if (delta != null && delta <= -RANK_GAP_MARK) {
    color = "text-rose-300/90";
    note = ` — タイムの方が ${-delta} 位下 (タイム低評価)`;
  }
  return (
    <td
      className={`${base} ${color} ${rank === 1 ? "font-bold" : ""}`}
      title={`${who} ${rank}位${timeRank != null ? ` / タイム ${timeRank}位` : ""}${note}`}
    >
      {rank}
    </td>
  );
}

// 「タイム1位 / Claude1位 / 市場1位」のチップ。同順位 1 位が複数なら並べる。
function TopChip({
  label,
  labelClass,
  items,
}: {
  label: string;
  labelClass: string;
  items: Array<{ number: number; name: string; extra?: string }>;
}) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-md border border-(--color-line) bg-(--color-surface-2) px-2 py-1">
      <span className={`text-[10px] font-black tracking-wider ${labelClass}`}>{label}</span>
      {items.map((h, i) => (
        <span key={h.number} className="inline-flex items-center gap-1">
          {i > 0 && <span className="text-(--color-muted)">・</span>}
          <span className="font-bold tnum">{h.number}</span>
          <span>{h.name}</span>
          {h.extra && <span className="tnum text-(--color-muted)">{h.extra}</span>}
        </span>
      ))}
    </span>
  );
}

function RunsTable({ runs }: { runs: TimeEvalRun[] }) {
  return (
    <table className="w-full text-[11px] tnum">
      <thead className="text-left text-(--color-muted)">
        <tr className="border-b border-(--color-line)/60 whitespace-nowrap">
          <th className="py-1 pr-2.5 font-medium">日付</th>
          <th className="py-1 pr-2.5 font-medium">場</th>
          <th className="py-1 pr-2.5 font-medium" title="芝/ダ + 距離 (m)">
            芝ダ距離
          </th>
          <th className="py-1 pr-2.5 font-medium">馬場</th>
          <th className="py-1 pr-2.5 font-medium">クラス</th>
          <th className="py-1 pr-2.5 text-right font-medium" title="1〜3着のみ判明 (— は 4着以下または不明)">
            着
          </th>
          <th className="py-1 pr-2.5 text-right font-medium" title="その走の生の走破時計">
            走破
          </th>
          <th className="py-1 pr-2.5 text-right font-medium" title="当レース条件に換算したタイム">
            → 換算
          </th>
          <th
            className="py-1 pr-2.5 text-right font-medium"
            title="開催日の馬場差補正 (秒)。+ = その日は時計の掛かる馬場だった → 換算タイムを速く補正"
          >
            馬場差補正
          </th>
          <th className="py-1 pr-2.5 font-medium">種別</th>
          <th
            className="py-1 pr-1 text-right font-medium"
            title="推定タイムへの集約重み (近走ほど・距離が近いほど重い)"
          >
            重み
          </th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r, i) => {
          const meta = kindMeta(r.kind);
          const excluded = r.kind === "excluded";
          return (
            <tr
              key={`${r.date}-${i}`}
              className={`border-b border-(--color-line)/40 last:border-b-0 ${
                excluded ? "text-(--color-muted)" : "text-(--color-foreground)"
              }`}
            >
              <td className="py-1 pr-2.5 whitespace-nowrap">{fmtRunDate(r.date)}</td>
              <td className="py-1 pr-2.5 whitespace-nowrap">{r.venue || "—"}</td>
              <td className="py-1 pr-2.5 whitespace-nowrap">{courseLabel(r.surface, r.distance)}</td>
              <td className="py-1 pr-2.5">{r.going || "—"}</td>
              <td className="py-1 pr-2.5 whitespace-nowrap">{r.race_class || "—"}</td>
              <td className="py-1 pr-2.5 text-right whitespace-nowrap">{finishCell(r.finish_pos)}</td>
              <td className="py-1 pr-2.5 text-right">{fmtRaceTime(r.own_sec)}</td>
              <td className={`py-1 pr-2.5 text-right ${excluded ? "" : "font-bold"}`}>
                {fmtRaceTime(r.conv_sec)}
              </td>
              <td className="py-1 pr-2.5 text-right text-(--color-muted)">
                {r.day_adj_sec != null ? fmtSignedSec(r.day_adj_sec) : "—"}
              </td>
              <td className="py-1 pr-2.5">
                <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5" title={meta.title}>
                  <Badge tone={meta.tone}>{meta.label}</Badge>
                  {r.note && <span className="text-[10px] text-(--color-muted)">{r.note}</span>}
                </span>
              </td>
              <td className="py-1 pr-1 text-right">{r.weight != null ? r.weight.toFixed(2) : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function TimeEvalCard({
  timeEval,
  finish,
  indexCompare,
}: {
  timeEval: TimeEval;
  // 結果の着順 (3連単 着順)。入着馬の行を他カードと同じく緑で強調する。
  finish?: number[];
  // snapshot の index_compare (Claude 指数 / 市場指数)。順位比較列に使う。
  indexCompare?: PredictionDetail["index_compare"] | null;
}) {
  const te = timeEval;
  const target = te.target;
  const cond = target ? targetLabel(target) : "";

  // 換算不能 (ばんえい / 当レース条件不明 等) は理由を控えめに出す (黙って消さない)。
  if (!te.available) {
    return (
      <section className="rounded-xl border border-(--color-line) bg-(--color-card)/70 px-4 py-2.5 text-xs text-(--color-muted) flex flex-wrap items-center gap-x-2 gap-y-1">
        <Timer className="w-3.5 h-3.5 shrink-0" aria-hidden />
        <span className="font-bold">タイム換算比較</span>
        {cond && <span className="tnum">{cond}</span>}
        <span>— {te.reason?.trim() || "換算できるデータがありません"}</span>
      </section>
    );
  }

  // backend は rank 順 (順位なしは末尾) で返すが、念のため並べ直す (同順位は馬番順)。
  const horses = [...(te.horses ?? [])].sort(
    (a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity) || a.number - b.number,
  );
  const finishList = finish ?? [];
  const icRows = indexCompare ?? [];
  const claudeRank = rankByIndex(icRows, (r) => r.claude_index);
  const marketRank = rankByIndex(icRows, (r) => r.market_index);
  const icName = new Map(icRows.map((r) => [r.number, r.name]));
  const timeTop = horses.filter((h) => h.rank === 1);
  const timeTopNums = timeTop.map((h) => h.number);
  const claudeTop = topNumbers(claudeRank);
  const marketTop = topNumbers(marketRank);
  const overlaps = (a: number[], b: number[]) => a.some((n) => b.includes(n));
  const assumed = target?.going_source === "assumed";
  const par = target?.par_sec ?? null;
  const parN = target?.par_n ?? null;

  const model = te.model ?? null;
  const dayEffects = !!model?.day_effects;
  const modelNote = model?.data_until
    ? `（${model.data_until} までの走破時計で学習${dayEffects ? "・開催日の馬場差補正あり" : ""}）`
    : dayEffects
      ? "（開催日の馬場差補正あり）"
      : "";
  const modelTitle = [
    `換算モデル ${te.version}`,
    model?.fitted_at ? `学習 ${model.fitted_at}` : "",
    model?.data_until ? `データ 〜${model.data_until}` : "",
    model?.n_runs ? `${model.n_runs.toLocaleString()} 走` : "",
    dayEffects ? "開催日の馬場差補正あり" : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Timer className="w-4 h-4 text-(--color-accent) shrink-0" aria-hidden />
          <span>タイム換算比較</span>
          {cond && (
            <span className="px-1.5 py-0.5 rounded-md border border-(--color-line) bg-(--color-surface-2) text-xs font-bold tnum">
              {cond}
            </span>
          )}
          {assumed && (
            <span className="inline-flex items-center gap-1 text-xs font-normal text-amber-300/90">
              <TriangleAlert className="w-3.5 h-3.5 shrink-0" aria-hidden />
              (馬場状態未発表 → 良で換算)
            </span>
          )}
          <span className="hidden md:inline text-xs text-(--color-muted) font-normal">
            過去走の走破時計を当レース条件に換算した推定タイム順
          </span>
        </span>
      }
      right={
        <span title={modelTitle}>
          <Badge tone="muted">換算 {te.version}</Badge>
        </span>
      }
    >
      {(timeTop.length > 0 || par != null) && (
        <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
          {timeTop.length > 0 && (
            <TopChip
              label="タイム1位"
              labelClass="text-(--color-accent)"
              items={timeTop.map((h) => ({
                number: h.number,
                name: h.name,
                extra: fmtRaceTime(h.est_sec),
              }))}
            />
          )}
          {claudeTop.length > 0 && (
            <TopChip
              label="Claude1位"
              labelClass="text-violet-300"
              items={claudeTop.map((n) => ({ number: n, name: icName.get(n) ?? "" }))}
            />
          )}
          {marketTop.length > 0 && (
            <TopChip
              label="市場1位"
              labelClass="text-sky-300"
              items={marketTop.map((n) => ({ number: n, name: icName.get(n) ?? "" }))}
            />
          )}
          {timeTop.length > 0 && claudeTop.length > 0 &&
            (overlaps(timeTopNums, claudeTop) ? (
              <Badge tone="muted">タイム1位 = Claude1位</Badge>
            ) : (
              <Badge tone="magenta">タイム1位 ≠ Claude1位</Badge>
            ))}
          {timeTop.length > 0 && marketTop.length > 0 &&
            (overlaps(timeTopNums, marketTop) ? (
              <Badge tone="muted">タイム1位 = 市場1位</Badge>
            ) : (
              <Badge tone="warn">タイム1位 ≠ 市場1位</Badge>
            ))}
          {par != null && (
            <span
              className="sm:ml-auto text-(--color-muted)"
              title="当条件 (同場・同馬場・同距離・同馬場状態) の標準的な勝ちタイム (参考)"
            >
              当条件の標準勝ちタイム{" "}
              <span className="font-bold text-(--color-foreground) tnum">{fmtRaceTime(par)}</span>
              {parN ? <span className="tnum"> (n={parN})</span> : null}
            </span>
          )}
        </div>
      )}

      {horses.length === 0 ? (
        <p className="text-xs text-(--color-muted)">換算対象の馬がいません。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm tnum">
            <thead className="text-left text-(--color-muted) text-xs">
              <tr className="border-b border-(--color-line) whitespace-nowrap">
                <th className="py-2 pr-3 text-right" title="推定タイムの速い順 (1 = 最速)。— = 換算できる過去走なし">
                  順位
                </th>
                <th className="py-2 pr-3 text-right">馬番</th>
                <th className="py-2 pr-3">馬名</th>
                <th className="py-2 pr-3 text-right" title="当レース条件での推定タイム (各走の換算タイムの重み付き平均)">
                  推定タイム
                </th>
                <th className="py-2 pr-3 text-right" title="推定タイム最速の馬との差 (秒)">
                  差
                </th>
                <th className="py-2 pr-3 text-right" title="タイム換算指数 0-100 (50 = レース中央値、1 秒速いごとに +10)">
                  指数
                </th>
                <th className="py-2 pr-3" title="同じ競馬場・馬場・距離での走数とベスト走破時計 (換算なしの生時計・括弧は馬場状態)">
                  同条件
                </th>
                <th className="py-2 pr-3 text-right" title="換算タイムのベスト">
                  換算ベスト
                </th>
                <th className="py-2 pr-3 text-right" title="推定タイムに使った走 / 対象とした過去走">
                  使用走
                </th>
                <th className="py-2 pr-3" title="推定タイムの信頼度 (使った走の数・換算の近さから)">
                  信頼度
                </th>
                <th
                  className="py-2 pl-4 pr-3 text-right text-violet-300/90"
                  title="Claude 指数の順位 (高い順)。タイム順位から 3 以上ずれると色付け (緑 = タイムの方が高評価 / 赤 = 低評価)"
                >
                  Claude順位
                </th>
                <th
                  className="py-2 pr-2 text-right text-sky-300/90"
                  title="市場指数 (単勝オッズ由来) の順位 = 人気順。タイム順位から 3 以上ずれると色付け (緑 = タイムの方が高評価 / 赤 = 低評価)"
                >
                  市場順位
                </th>
              </tr>
            </thead>
            {horses.map((h, i) => {
              const place = finishList.indexOf(h.number) + 1; // 0 = 着外 or 結果なし
              const hit = place > 0;
              const rank = h.rank;
              const ranked = rank != null;
              const top3 = rank != null && rank <= 3;
              const runs = h.runs ?? [];
              const rowBg = hit ? HIT_ROW_BG : i % 2 === 1 ? ZEBRA_BG : "";
              const conf = confMeta(h.confidence);
              const gapClose = rank !== 1 && h.gap_sec != null && h.gap_sec < 0.5;
              return (
                <tbody key={h.number} className="group">
                  <tr
                    className={`${runs.length ? "" : "border-b border-(--color-line)/60"} ${rowBg} ${HOVER_BG} ${
                      hit ? "text-emerald-300" : ranked ? "" : "text-(--color-muted)"
                    }`}
                  >
                    <td className="py-1.5 pr-3 text-right">
                      {ranked ? (
                        <span
                          className={
                            rank === 1 ? "font-black text-(--color-accent)" : top3 ? "font-bold" : ""
                          }
                        >
                          {rank}
                        </span>
                      ) : (
                        <span className="text-(--color-muted)">—</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-bold">{h.number}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      {h.name}
                      {hit && <HitMark place={place} />}
                    </td>
                    <td
                      className={`py-1.5 pr-3 text-right ${
                        rank === 1 ? "font-black text-(--color-accent)" : top3 ? "font-bold" : "font-medium"
                      }`}
                    >
                      {fmtRaceTime(h.est_sec)}
                    </td>
                    <td className={`py-1.5 pr-3 text-right ${gapClose ? "" : "text-(--color-muted)"}`}>
                      {rank !== 1 && h.gap_sec != null ? `+${h.gap_sec.toFixed(1)}` : "—"}
                    </td>
                    <td className={`py-1.5 pr-3 text-right ${indexCellClass(h.index)}`}>
                      {h.index != null ? h.index.toFixed(0) : "—"}
                    </td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      {h.same_n > 0 ? (
                        <>
                          <span className="text-(--color-muted)">{h.same_n}走</span>
                          {h.same_best_sec != null && (
                            <>
                              {" "}
                              <span className="font-medium">{fmtRaceTime(h.same_best_sec)}</span>
                              {h.same_best_going && (
                                <span className="text-(--color-muted)">({h.same_best_going})</span>
                              )}
                            </>
                          )}
                        </>
                      ) : (
                        <span className="text-(--color-muted)">—</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right">{fmtRaceTime(h.conv_best_sec)}</td>
                    <td className="py-1.5 pr-3 text-right text-(--color-muted)">
                      {h.n_used}/{h.n_runs}
                    </td>
                    <td className="py-1.5 pr-3">
                      <Badge tone={conf.tone}>{conf.label}</Badge>
                    </td>
                    <CompareRankCell
                      first
                      rank={claudeRank.get(h.number)}
                      timeRank={rank}
                      who="Claude"
                      topClass="text-violet-300"
                    />
                    <CompareRankCell
                      rank={marketRank.get(h.number)}
                      timeRank={rank}
                      who="市場"
                      topClass="text-sky-300"
                    />
                  </tr>
                  {runs.length > 0 && (
                    <tr className={`border-b border-(--color-line)/60 ${rowBg} ${HOVER_BG}`}>
                      <td colSpan={2} className="pb-1.5 pt-0" />
                      <td colSpan={N_COLS - 2} className="pb-1.5 pt-0 pr-2 align-top">
                        <details className="text-xs">
                          <summary className="w-fit cursor-pointer select-none text-sky-300/80 hover:text-sky-200 marker:text-sky-300/50">
                            換算内訳 <span className="tnum">{runs.length}</span>走
                            <span className="ml-1.5 text-[11px] text-(--color-muted) tnum">
                              {kindCountsText(runs)}
                            </span>
                          </summary>
                          {/* w-0 + min-w-full: 内訳表の幅で本表の列を引き伸ばさず、
                              溢れる分はこの枠内で横スクロールさせる (モバイル対策)。 */}
                          <div className="mt-1 mb-1 w-0 min-w-full overflow-x-auto">
                            <RunsTable runs={runs} />
                          </div>
                        </details>
                      </td>
                    </tr>
                  )}
                </tbody>
              );
            })}
          </table>
        </div>
      )}

      <p className="mt-3 text-xs text-(--color-muted)">
        推定タイム = 各走の走破時計を当レース条件に換算し、近走ほど・距離が近いほど重く平均した値。換算モデル{" "}
        {te.version}
        {modelNote}。市場・Claude の順位と並べて比較してください。
      </p>
      <p className="mt-1 text-[11px] text-(--color-muted)">
        順位比較: Claude / 市場の順位がタイム順位より {RANK_GAP_MARK} 以上下 ={" "}
        <span className="text-emerald-300">緑</span> (タイムの方が高評価)、{RANK_GAP_MARK} 以上上 ={" "}
        <span className="text-rose-300/90">赤</span> (タイムの方が低評価)。
      </p>
    </Card>
  );
}
