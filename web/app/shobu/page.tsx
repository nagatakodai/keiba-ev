"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  ArrowDownUp,
  ChevronRight,
  Clock,
  Eye,
  EyeOff,
  Flame,
  Loader2,
  Play,
  Settings2,
  Sparkles,
  Swords,
  X,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  Input,
  Page,
  PageHeader,
  Select,
  Stat,
  fmtTime,
  raceTimingStatus,
} from "@/components/ui";
import { LogStream } from "@/components/LogStream";
import { HitBetBadges } from "@/components/HitBetBadges";
import {
  api,
  type JobInfo,
  type ShobuRace,
  type ShobuResult,
  type ShobuScanRequest,
} from "@/lib/api";

// CSS のみのトグル (watch-auto と同型)。
function Toggle({
  checked,
  onChange,
  disabled,
  children,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <label
      className={`inline-flex items-center gap-2.5 text-sm select-none ${
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
      }`}
    >
      <span className="relative inline-flex h-5 w-9 shrink-0">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          disabled={disabled}
        />
        <span className="absolute inset-0 rounded-full border transition-colors bg-(--color-surface-3) border-(--color-line) peer-checked:bg-emerald-500/90 peer-checked:border-emerald-400/70 peer-focus-visible:outline-2 peer-focus-visible:outline-(--color-ring) peer-focus-visible:outline-offset-2" />
        <span className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-slate-400 shadow transition-transform duration-150 peer-checked:translate-x-4 peer-checked:bg-(--color-foreground)" />
      </span>
      <span>{children}</span>
    </label>
  );
}

function FieldGroup({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className="rounded-xl border border-(--color-line) bg-(--color-surface-2)/30 px-4 pb-4 pt-2">
      <legend className="px-1.5 text-[10px] font-bold uppercase tracking-widest text-(--color-muted)">
        {legend}
      </legend>
      {children}
    </fieldset>
  );
}

// 勝負スコアの前回比 (▲ 上昇 / ▼ 下降 / → 横ばい)。2分毎の最新オッズ更新で付く。
function ScoreDelta({ delta }: { delta?: number | null }) {
  if (delta == null) return null;
  const up = delta > 0.05;
  const down = delta < -0.05;
  const cls = up ? "text-emerald-300" : down ? "text-rose-300" : "text-(--color-muted)";
  const arrow = up ? "▲" : down ? "▼" : "→";
  const txt = up || down ? `${delta > 0 ? "+" : ""}${delta.toFixed(1)}` : "±0";
  return (
    <span className={`inline-flex items-center gap-0.5 text-[11px] font-bold tnum ${cls}`} title="前回更新比">
      {arrow}
      {txt}
    </span>
  );
}

// 勝負スコア履歴の極小スパークライン (装飾)。最初→最後 が上昇なら緑、下降なら赤。
function MiniSpark({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const w = 56;
  const h = 16;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const yAt = (v: number) => h - 1 - ((v - min) / range) * (h - 2);
  const d = points
    .map((v, i) => `${((i / (points.length - 1)) * w).toFixed(1)},${yAt(v).toFixed(1)}`)
    .join(" ");
  const stroke = points[points.length - 1] >= points[0] ? "var(--good)" : "var(--bad)";
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-14 h-4" preserveAspectRatio="none" aria-hidden>
      <polyline points={d} fill="none" stroke={stroke} strokeWidth={1.25} strokeOpacity={0.85} />
    </svg>
  );
}

function RaceTypeBadge({ t }: { t: "jra" | "nar" | "banei" }) {
  if (t === "jra") return <Badge tone="info">JRA</Badge>;
  if (t === "banei") return <Badge tone="warn">ばんえい</Badge>;
  return <Badge tone="muted">地方</Badge>;
}

function RaceCard({
  r,
  nowMs,
  rank,
  hidden,
  onToggleHide,
}: {
  r: ShobuRace;
  nowMs: number | null;
  rank?: number;
  hidden?: boolean;
  onToggleHide?: () => void;
}) {
  const claude = r.claude;
  const timing =
    nowMs !== null ? raceTimingStatus(r.close_at || null, r.start_at || null, false, nowMs) : null;
  const rec = r.recommended;
  return (
    <div
      className={`rounded-xl border p-3.5 transition-colors ${
        rec
          ? "border-emerald-500/45 bg-emerald-500/[0.06] shadow-[0_0_20px_rgba(52,211,153,0.08)]"
          : "border-(--color-line) bg-(--color-card) hover:border-(--color-line)"
      }`}
    >
      {/* ── ヘッダ行 ── */}
      <div className="flex items-start justify-between gap-2">
        <Link href={`/predictions/${r.race_id}`} className="flex items-center gap-2 min-w-0 hover:underline">
          {rank != null && (
            <span
              className="shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full bg-emerald-500 text-emerald-950 text-xs font-black tnum"
              title={`勝負レース #${rank}`}
            >
              {rank}
            </span>
          )}
          <span className="text-sm font-bold truncate">{r.venue || "(不明)"}</span>
          <span className="mono text-xl font-black tnum leading-none shrink-0">
            {r.race_no}
            <span className="text-[11px] font-bold text-(--color-muted) ml-0.5">R</span>
          </span>
          <RaceTypeBadge t={r.race_type} />
          {rec && (
            <Badge tone="good">
              <Flame className="size-3 mr-0.5" />
              推奨
            </Badge>
          )}
        </Link>
        <div className="shrink-0 text-right">
          <div className="text-2xl font-black tnum leading-none text-(--color-foreground)">
            {r.shobu_score.toFixed(0)}
          </div>
          <div className="text-[9px] font-bold uppercase tracking-wider text-(--color-muted)">勝負スコア</div>
          {(r.score_delta != null || (r.score_history?.length ?? 0) >= 2) && (
            <div className="mt-0.5 flex items-center justify-end gap-1">
              <ScoreDelta delta={r.score_delta} />
              {(r.score_history?.length ?? 0) >= 2 && (
                <MiniSpark points={r.score_history!.map((p) => p.score)} />
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── 時刻 + matched chips ── */}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] tnum text-(--color-muted)">
        {(r.close_at || r.start_at) && (
          <span>
            締切 <span className="mono text-(--color-foreground)">{fmtTime(r.close_at)}</span>
            <span className="mx-1">→</span>発走{" "}
            <span className="mono text-(--color-foreground)">{fmtTime(r.start_at)}</span>
          </span>
        )}
        {timing && timing.label !== "結果待ち" && <Badge tone={timing.tone}>{timing.label}</Badge>}
        {r.matched.includes("claude") && <Badge tone="magenta">市場乖離</Badge>}
        {r.n_runners != null && <span>{r.n_runners}頭</span>}
        {r.data_source === "none" && <Badge tone="muted">データなし</Badge>}
        {r.data_source === "snapshot" && <Badge tone="muted">snapshot</Badge>}
        {r.snapshot_stage === "score" && <Badge tone="muted">暫定</Badge>}
      </div>

      {/* ── 市場との順位乖離 (基準B) ── */}
      {claude && (claude.top_rank_gap >= 1 || claude.edge_horses.length > 0) && (
        <div className="mt-2.5">
          <div className="flex items-baseline justify-between text-[11px] mb-1">
            <span className="font-bold text-fuchsia-300 inline-flex items-center gap-1">
              <Sparkles className="size-3" />市場乖離
            </span>
            <span className="tnum text-(--color-muted)">score {claude.score.toFixed(0)}</span>
          </div>
          {/* Claude 本命が市場で何番人気か (「市場2番人気なのに Claude 本命」) */}
          {claude.top_pick && claude.top_rank_gap >= 1 && (
            <div className="mb-1 text-[11px] inline-flex items-center gap-1.5 rounded-md border border-fuchsia-500/30 bg-fuchsia-500/10 px-1.5 py-0.5 text-fuchsia-200">
              <span className="font-bold">Claude本命</span>
              <span className="mono font-bold">{claude.top_pick.number}</span>
              {claude.top_pick.name && <span className="max-w-[6em] truncate">{claude.top_pick.name}</span>}
              <span className="text-fuchsia-300/90">= 市場{claude.top_pick.market_rank}番人気</span>
            </div>
          )}
          <div className="flex flex-col gap-0.5">
            {claude.edge_horses.slice(0, 4).map((h, i) => (
              <div key={h.number ?? `edge-${i}`} className="flex items-center gap-2 text-[11px] tnum">
                <span className="mono font-bold w-5 text-right">{h.number}</span>
                {h.name && <span className="max-w-[6.5em] truncate text-(--color-foreground)">{h.name}</span>}
                <span className="text-(--color-muted)">
                  市場{h.market_rank}位 → Claude{h.claude_rank}位
                </span>
                <span className="font-bold text-emerald-300 ml-auto" title="Claude が市場より何順位上に評価したか">
                  ↑{h.rank_gap}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── ダッシュボード仮想購入 (BOX+戦略くらべ) の的中券種 (ユーザ指示 2026-07-04)。
          結果確定済レースのみ付く。EV束の的中とは無関係。 ── */}
      {(r.hit_strategies?.length ?? 0) > 0 && (
        <div className="mt-2.5">
          <HitBetBadges hits={r.hit_strategies} />
        </div>
      )}

      {/* ── reasons + 非表示 / 詳細リンク ── */}
      <div className="mt-2.5 flex items-end justify-between gap-2 pt-2 border-t border-(--color-line-soft)">
        <p className="text-[11px] text-(--color-muted) leading-snug min-w-0">
          {r.reasons.length > 0 ? r.reasons.join(" ／ ") : "—"}
        </p>
        <div className="shrink-0 flex items-center gap-2.5">
          {onToggleHide && (
            <button
              onClick={onToggleHide}
              className="inline-flex items-center gap-0.5 text-[11px] font-bold text-(--color-muted) hover:text-(--color-foreground) transition-colors"
              title={hidden ? "この勝負レースを一覧に戻す" : "このレースを非表示にして一番下へ移動"}
            >
              {hidden ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
              {hidden ? "表示" : "非表示"}
            </button>
          )}
          <Link
            href={`/predictions/${r.race_id}`}
            className="inline-flex items-center gap-0.5 text-[11px] font-bold text-(--color-accent) hover:underline"
          >
            詳細
            <ChevronRight className="size-3.5" />
          </Link>
        </div>
      </div>
    </div>
  );
}

// レースの安定キー (React key + 非表示 Set のメンバー判定に共通使用)。
const keyOf = (r: ShobuRace) => `${r.race_id}-${r.netkeiba_race_id}`;

export default function ShobuPage() {
  // ── 抽出オプション ──
  // 判定は基準B (市場との順位乖離) 単独 (ユーザ指示 2026-06-28: 基準A=強弱は廃止)。
  const [raceType, setRaceType] = useState<"all" | "jra" | "nar" | "banei">("all");
  // 基準B: 市場との順位乖離。edgeThreshold=乖離スコアしきい値 / edgeMargin=指数差フロア。
  const [edgeThreshold, setEdgeThreshold] = useState("25");
  const [edgeMargin, setEdgeMargin] = useState("3");
  const [upcomingOnly, setUpcomingOnly] = useState(true);
  // ボタン押下で全レースの Claude 指数を一括生成 (claude -p)。既定 ON (ユーザ指示 2026-06-20)。
  const [claudeAll, setClaudeAll] = useState(true);
  const [claudeEval, setClaudeEval] = useState("0");
  // 取得レース数の上限 ("" = 全件)。発走日時が近い (早い) 順に N 件だけ評価する。
  const [maxRaces, setMaxRaces] = useState("");
  // Claude 指数を生成するモデル (opus/sonnet/haiku で質・速度・コストを比較したい, 2026-07-05)。
  const [model, setModel] = useState<"opus" | "sonnet" | "haiku">("opus");
  const [showSettings, setShowSettings] = useState(false);
  const [showOthers, setShowOthers] = useState(false);

  const [job, setJob] = useState<(JobInfo & { date?: string }) | null>(null);
  const [scanning, setScanning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [result, setResult] = useState<ShobuResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState<number | null>(null);
  // 勝負レースページを開いている間、2分毎に推奨レースの最新オッズで再採点する (既定 ON)。
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null);
  // 勝負スコア 100 (上限に張り付いた最強シグナル) のレースだけ表示するフィルタ。
  const [onlyScore100, setOnlyScore100] = useState(false);
  // 並び順: "score" = 勝負スコア降順 (同点は発走が近い順) / "time" = 発走時刻の昇順 (近い順)。
  // ボタンで切替 (ユーザ指示 2026-06-28: 100まとめ連動でなく独立したソートボタン)。
  const [sortMode, setSortMode] = useState<"score" | "time">("score");
  // 非表示にしたレース (card key の Set)。一番下の「非表示」セクションに集約。日付ごとに localStorage 永続。
  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(new Set());
  const [showHidden, setShowHidden] = useState(false);

  // 現在のフォーム設定を scan options に変換 (基準B 単独)。
  const buildOptions = (): ShobuScanRequest => ({
    race_type: raceType,
    edge_margin: (() => {
      const v = parseFloat(edgeMargin);
      return Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 3;
    })(),
    edge_threshold: (() => {
      const v = parseFloat(edgeThreshold);
      return Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 25;
    })(),
    upcoming_only: upcomingOnly,
    claude_all: claudeAll,
    claude_eval: (() => {
      const v = parseInt(claudeEval, 10);
      return Number.isFinite(v) && v >= 0 ? Math.min(50, v) : 0;
    })(),
    // 取得レース数の上限 ("" = 全件 → undefined)。発走が近い (早い) 順に N 件。
    max_races: (() => {
      const v = parseInt(maxRaces, 10);
      return Number.isFinite(v) && v > 0 ? Math.min(300, v) : undefined;
    })(),
    model,
  });

  // 締切バッジ用の現在時刻 (5秒毎)。
  useEffect(() => {
    setNowMs(Date.now());
    const t = setInterval(() => setNowMs(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);

  // 初回マウント時に既存のスキャン結果 (当日) があれば表示。
  // (勝負レースの自動更新は行わない方針 — ユーザ指示。更新はボタン再実行で。)
  useEffect(() => {
    api
      .getShobuResult()
      .then((r) => setResult(r))
      .catch(() => {
        /* 未スキャン (404) は無視 */
      });
  }, []);

  // 非表示リストを日付ごとに localStorage から復元 (ページ再読込・自動更新を跨いで維持)。
  useEffect(() => {
    if (!result?.date) return;
    let next: Set<string>;
    try {
      const raw = localStorage.getItem(`shobu-hidden-${result.date}`);
      next = raw ? new Set(JSON.parse(raw) as string[]) : new Set();
    } catch {
      next = new Set();
    }
    // localStorage → state の同期 (date 変化時のみ)。外部ストアの同期なので effect で setState する。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHiddenKeys(next);
  }, [result?.date]);

  // カードの非表示 / 再表示トグル (localStorage に永続)。
  const toggleHide = (r: ShobuRace) => {
    const date = result?.date;
    setHiddenKeys((prev) => {
      const next = new Set(prev);
      const k = keyOf(r);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      if (date) {
        try {
          localStorage.setItem(`shobu-hidden-${date}`, JSON.stringify([...next]));
        } catch {
          /* localStorage 不可 (private mode 等) は無視 — メモリ上の state は更新済 */
        }
      }
      return next;
    });
  };

  // スキャン Job を polling。各 tick で **暫定/進捗中の結果も取得** して表示する
  // (生成前: 基準A中心の暫定一覧 → 各レース生成完了ごとに基準B が確定して live 更新)。
  // Job 完了で最終版を取得して終了。
  useEffect(() => {
    if (!job || !scanning) return;
    let cancelled = false;
    const poll = setInterval(async () => {
      // 1) 進捗中の結果 (暫定→確定) を反映。まだ書かれていない (404) は無視。
      try {
        const r = await api.getShobuResult(job.date);
        if (!cancelled) setResult(r);
      } catch {
        /* 未書き出し (404) / transient は無視 */
      }
      // 2) Job ステータス。終了したら最終版を取得して polling 終了。
      try {
        const j = await api.getJob(job.id);
        if (cancelled) return;
        if (j.status === "done" || j.status === "failed" || j.status === "cancelled") {
          clearInterval(poll);
          setScanning(false);
          if (j.status === "failed") {
            setError("スキャンが失敗しました (ログ参照)");
          } else {
            // done / cancelled: 最新結果を取得して表示。中止時は partial を確定表示にするため
            // generating を下ろす (生成中バナーを消す。生成済みレースは基準B が付いている)。
            try {
              const r = await api.getShobuResult(job.date);
              if (!cancelled)
                setResult(j.status === "cancelled" ? { ...r, generating: false } : r);
            } catch (e) {
              if (!cancelled && j.status === "done") setError(e instanceof Error ? e.message : String(e));
            }
          }
        }
      } catch {
        /* transient */
      }
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(poll);
    };
  }, [job, scanning]);

  const startScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const j = await api.scanShobu(buildOptions());
      setJob(j);
    } catch (e) {
      setScanning(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // 実行中のスキャン Job を中止 (プロセスグループを SIGINT→SIGTERM→SIGKILL)。
  // Job が cancelled になると上の poll が partial 結果を確定表示にして scanning を下ろす。
  const cancelScan = async () => {
    if (!job || cancelling) return;
    setCancelling(true);
    try {
      await api.cancelJob(job.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCancelling(false);
    }
  };

  const summary = result?.summary;
  // 判定は基準B 単独 (server が確定済み)。一覧は server の結果をそのまま使う。
  const races: ShobuRace[] = result?.races ?? [];

  const isHidden = (r: ShobuRace) => hiddenKeys.has(keyOf(r));
  // 並び順の比較関数。"score" = 勝負スコア降順 (同点=発走が近い順 → スコア100まとめ時は発走順)、
  // "time" = 発走時刻の昇順 (近い順、同時刻はスコア降順)。ソートボタンで切替。
  const cmp = (a: ShobuRace, b: ShobuRace) =>
    sortMode === "time"
      ? (a.start_at || 0) - (b.start_at || 0) || b.shobu_score - a.shobu_score
      : b.shobu_score - a.shobu_score || (a.start_at || 0) - (b.start_at || 0);

  const recommended = races.filter((r) => r.recommended);
  const others = races.filter((r) => !r.recommended);

  // 「勝負スコア100のみ」フィルタ適用後の表示リスト (shobu_score は 100 で cap)。
  const isScore100 = (r: ShobuRace) => r.shobu_score >= 100;
  const score100Count = races.filter(isScore100).length;
  // 非表示を除外 → 並び順を適用。非表示レースは一番下の専用セクションへ集約。
  const recShown = (onlyScore100 ? recommended.filter(isScore100) : recommended)
    .filter((r) => !isHidden(r))
    .sort(cmp);
  const othersShown = (onlyScore100 ? others.filter(isScore100) : others)
    .filter((r) => !isHidden(r))
    .sort(cmp);
  const hiddenShown = races.filter(isHidden).sort(cmp);

  // 推奨レースのみ最新オッズで再採点 (Claude は呼ばない・単勝 1 fetch/レース)。手動 & 自動共通。
  const doRefresh = async () => {
    if (!result || refreshing || result.generating) return;
    setRefreshing(true);
    try {
      const updated = await api.refreshShobu(result.date);
      setResult(updated);
      setRefreshedAt(updated.refreshed_at ?? null);
    } catch {
      /* transient (404/ネットワーク) は無視 */
    } finally {
      setRefreshing(false);
    }
  };

  // 2分毎の自動更新。ページを開いている間・推奨レースがある時だけ走る (スキャン中・生成中は止める)。
  // 依存に recommended.length を入れて「0件→再スキャンで出た」場合にも interval を張り直す。
  useEffect(() => {
    if (!autoRefresh || !result || scanning || result.generating || recommended.length === 0)
      return;
    const date = result.date;
    let cancelled = false;
    const t = setInterval(async () => {
      if (cancelled) return;
      setRefreshing(true);
      try {
        const updated = await api.refreshShobu(date);
        if (!cancelled) {
          setResult(updated);
          setRefreshedAt(updated.refreshed_at ?? null);
        }
      } catch {
        /* transient は無視 */
      } finally {
        if (!cancelled) setRefreshing(false);
      }
    }, 120_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, result?.date, scanning, result?.generating, recommended.length]);

  return (
    <Page>
      <PageHeader
        eyebrow="Shobu"
        title="今日の勝負レース"
        subtitle="ボタンで当日の全レース (JRA+地方) を取得し、市場ランクと Claude 指数ランクの乖離が大きいレース (例: 市場2番人気を Claude が本命視) を勝負レースとして抽出します。しきい値はオプションで調整できます。"
      />

      {/* ── サマリー ── */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Stat
          label="勝負レース"
          value={result ? recommended.length : "—"}
          tone={recommended.length > 0 ? "good" : "default"}
          accentTone="good"
          hint={summary ? `市場乖離 ・ 評価 ${summary.evaluated} レース中` : "未スキャン"}
        />
        <Stat
          label="評価レース数"
          value={summary ? summary.evaluated : "—"}
          accentTone="info"
          hint={
            summary
              ? summary.by_type
                ? `JRA ${summary.by_type.jra} / 地方 ${summary.by_type.nar} / ばんえい ${summary.by_type.banei}`
                : `当日開催 ${summary.total_discovered}`
              : "—"
          }
        />
        <Stat
          label="Claude 指数あり"
          value={summary ? summary.with_claude : "—"}
          accentTone="magenta"
          hint={summary ? `snapshot ${summary.with_snapshot}` : "—"}
        />
      </div>

      {/* ── コントロール (ボタン + オプション) ── */}
      <section className="rounded-xl border border-(--color-line) bg-(--color-card) overflow-hidden shadow-[0_2px_12px_rgba(0,0,0,0.35)]">
        <header className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 border-b border-(--color-line) bg-(--color-section-head)">
          <div className="flex items-center gap-2 min-w-0">
            <Swords className="size-4 text-(--color-accent)" />
            <div>
              <h2 className="text-sm font-bold tracking-tight">勝負レース抽出</h2>
              <p className="text-[11px] text-(--color-muted)">
                {result
                  ? `最終スキャン ${result.generated_at.slice(5, 16).replace("T", " ")} (${result.date})`
                  : "未スキャン — 下のボタンで実行"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={() => setShowSettings((v) => !v)} aria-expanded={showSettings}>
              <Settings2 size={15} aria-hidden />
              オプション
              <ChevronRight size={14} className={`transition-transform ${showSettings ? "rotate-90" : ""}`} aria-hidden />
            </Button>
            {scanning && (
              <Button variant="danger" size="lg" disabled={cancelling} onClick={cancelScan}>
                {cancelling ? <Loader2 className="size-4 animate-spin" /> : <X className="size-4" />}
                {cancelling ? "中止中..." : "中止"}
              </Button>
            )}
            <Button size="lg" disabled={scanning} onClick={startScan}>
              {scanning ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" fill="currentColor" />}
              {scanning
                ? "スキャン中..."
                : claudeAll
                  ? "全レース取得 + Claude指数生成"
                  : "全レース取得して抽出"}
            </Button>
          </div>
        </header>

        <div className="p-4 space-y-4">
          {showSettings && (
            <div className="space-y-4">
              <FieldGroup legend="基準 (市場との順位乖離 = 勝負レースの定義)">
                <p className="text-[11px] text-(--color-muted) mb-2">
                  市場ランクと Claude 指数ランクの食い違い (例: 市場2番人気なのに Claude 本命) で抽出します。
                </p>
                <div className="grid grid-cols-2 gap-3">
                  <Input label="市場乖離スコア (0-100)" type="number" min="0" max="100" step="5"
                    hint="高いほど強い乖離のみ推奨"
                    value={edgeThreshold} onChange={(e) => setEdgeThreshold(e.target.value)} disabled={scanning} className="tnum" />
                  <Input label="指数差フロア" type="number" min="0" max="100" step="1"
                    hint="乖離馬は順位差+指数差この値以上"
                    value={edgeMargin} onChange={(e) => setEdgeMargin(e.target.value)} disabled={scanning} className="tnum" />
                </div>
              </FieldGroup>

              <FieldGroup legend="対象 / データ取得">
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <Select label="対象" value={raceType} onChange={(e) => setRaceType(e.target.value as "all" | "jra" | "nar" | "banei")} disabled={scanning}
                    hint="ばんえいは別競技として分離 (確率モデルも segment 分離済)">
                    <option value="all">JRA + 地方 + ばんえい</option>
                    <option value="jra">JRA のみ</option>
                    <option value="nar">地方 (平地) のみ</option>
                    <option value="banei">ばんえい のみ</option>
                  </Select>
                  <Select label="取得レース数" value={maxRaces} onChange={(e) => setMaxRaces(e.target.value)} disabled={scanning}
                    hint="発走日時が近い (早い) 順にこの数だけ評価。全件=制限なし">
                    <option value="">全件 (制限なし)</option>
                    <option value="5">発走が近い 5 件</option>
                    <option value="10">発走が近い 10 件</option>
                    <option value="15">発走が近い 15 件</option>
                    <option value="20">発走が近い 20 件</option>
                    <option value="30">発走が近い 30 件</option>
                    <option value="50">発走が近い 50 件</option>
                  </Select>
                  <Select label="Claude モデル" value={model}
                    onChange={(e) => setModel(e.target.value as "opus" | "sonnet" | "haiku")} disabled={scanning}
                    hint="指数の質・速度・コストを比較。既定 Opus (従来挙動)">
                    <option value="opus">Opus (既定・最も精密)</option>
                    <option value="sonnet">Sonnet (速い・低コスト)</option>
                    <option value="haiku">Haiku (最速・最安)</option>
                  </Select>
                  {!claudeAll && (
                    <Input label="Claude 指数を生成 (発走が近い順 N件)" type="number" min="0" max="50" step="1"
                      value={claudeEval} onChange={(e) => setClaudeEval(e.target.value)} disabled={scanning}
                      hint="0=既存スナップショットのみ" className="tnum" />
                  )}
                </div>
                <div className="mt-3 flex flex-col gap-2.5">
                  <Toggle checked={claudeAll} onChange={setClaudeAll} disabled={scanning}>
                    <span className="font-medium">全レースの Claude 指数を一括生成</span>
                    <span className="text-(--color-muted) ml-1">— ボタンで claude -p を一斉実行</span>
                  </Toggle>
                  <Toggle checked={upcomingOnly} onChange={setUpcomingOnly} disabled={scanning}>
                    発走前のみ
                  </Toggle>
                </div>
                {claudeAll && (
                  <p className="mt-2.5 text-xs text-(--color-warn) leading-relaxed">
                    Claude 指数の無い発走前レースを <b>全件</b> claude -p で生成します
                    (Tavily / WebFetch で各馬を web 検索 → 0-100 指数)。レース数によっては
                    数分〜数十分かかります (claude -p は最大 5 並列)。ライブログで進捗が出ます。
                  </p>
                )}
              </FieldGroup>
            </div>
          )}

          {error && (
            <div className="text-sm text-(--color-bad) bg-rose-500/10 border border-rose-500/40 rounded-lg px-3 py-2 break-all">
              {error}
            </div>
          )}

          {job && (
            <div>
              <div className="mb-1 flex items-center gap-2 text-xs">
                <span className="text-(--color-muted)">スキャンログ</span>
                <Badge tone={scanning ? "warn" : "muted"}>{scanning ? "running" : "done"}</Badge>
                <span className="text-(--color-muted) mono">{job.id.slice(0, 8)}</span>
              </div>
              <LogStream key={job.id} url={`/api/jobs/${job.id}/stream`} height="h-[28vh]" emptyHint="(ログ待機中...)" />
            </div>
          )}
        </div>
      </section>

      {/* ── 結果 ── */}
      {result && (
        <section className="space-y-6">
          {/* ── Claude 指数 一括生成の進捗バナー ──
              生成中は下の一覧が「暫定 (基準A中心)」で、各レースの指数が付き次第 基準B が確定する。 */}
          {result.generating && (result.gen_total ?? 0) > 0 && (
            <div className="rounded-xl border border-fuchsia-500/40 bg-fuchsia-500/[0.07] px-4 py-3">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 min-w-0">
                  <Loader2 className="size-4 animate-spin text-fuchsia-300 shrink-0" />
                  <span className="text-sm font-bold text-fuchsia-100">Claude 指数を生成中</span>
                  <span className="tnum text-sm font-black text-fuchsia-200">
                    {result.gen_done ?? 0}
                    <span className="text-fuchsia-300/70 font-bold">/{result.gen_total}</span>
                  </span>
                </div>
                <span className="text-[11px] text-fuchsia-200/80 leading-snug max-w-xl">
                  下の一覧は<b>暫定</b> です。各レースの Claude 指数が付き次第{" "}
                  <b>市場との順位乖離</b> が確定し、勝負スコアと順位が更新されます。
                </span>
              </div>
              <div className="mt-2 h-1.5 rounded-full bg-fuchsia-500/15 overflow-hidden">
                <div
                  className="h-full bg-fuchsia-400/80 transition-[width] duration-500"
                  style={{
                    width: `${Math.round(
                      ((result.gen_done ?? 0) / Math.max(1, result.gen_total ?? 1)) * 100,
                    )}%`,
                  }}
                />
              </div>
            </div>
          )}

          {/* 勝負レース (推奨) — 番号付きで明確に */}
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 px-1">
              <Flame className="size-4 text-emerald-300" />
              <h2 className="text-base font-bold tracking-tight">
                勝負レース
                <span className="ml-1.5 text-sm font-normal text-(--color-muted)">(推奨)</span>
              </h2>
              <Badge tone={recShown.length > 0 ? "good" : "muted"}>
                {recShown.length} 件
                {recommended.length !== recShown.length ? ` / ${recommended.length}` : ""}
              </Badge>
              {recommended.length > 0 && (
                <span className="text-[11px] text-(--color-muted)">
                  {sortMode === "time" ? "発走時刻順" : "勝負スコア順"} ・ 緑カード = 推奨
                </span>
              )}
              {/* 並び替えボタン: 勝負スコア順 ⇄ 発走時刻順 (近い順)。スコア100でまとめても発走順に並べられる */}
              <button
                onClick={() => setSortMode((m) => (m === "score" ? "time" : "score"))}
                className="inline-flex items-center gap-1 rounded-md border border-(--color-line) bg-(--color-surface-2) px-2 py-0.5 text-[11px] font-bold text-(--color-foreground) hover:border-(--color-accent) transition-colors"
                title="並び順を切り替え (勝負スコア順 / 発走時刻の昇順)"
              >
                {sortMode === "time" ? <Clock className="size-3" /> : <ArrowDownUp className="size-3" />}
                {sortMode === "time" ? "発走時刻順" : "勝負スコア順"}
              </button>
              {/* 勝負スコア100 (上限張り付き) のみ表示フィルタ */}
              <Toggle checked={onlyScore100} onChange={setOnlyScore100} disabled={score100Count === 0}>
                <span className="text-[11px]">
                  勝負スコア100のみ
                  <span className="ml-1 text-(--color-muted) tnum">({score100Count})</span>
                </span>
              </Toggle>
              {/* ── 最新オッズ自動更新 (2分毎) コントロール ── */}
              <div className="ml-auto flex items-center gap-3">
                {refreshing && <Loader2 className="size-3.5 animate-spin text-(--color-muted)" />}
                {(refreshedAt ?? result.refreshed_at) && (
                  <span className="text-[11px] text-(--color-muted) tnum" title="推奨レースの最新オッズで勝負スコアを再計算した時刻">
                    最新オッズ {(refreshedAt ?? result.refreshed_at)!.slice(11, 16)} 更新
                  </span>
                )}
                <button
                  onClick={doRefresh}
                  disabled={refreshing || recommended.length === 0 || result.generating}
                  className="text-[11px] font-bold text-(--color-accent) hover:underline disabled:opacity-40 disabled:no-underline"
                >
                  今すぐ更新
                </button>
                <Toggle checked={autoRefresh} onChange={setAutoRefresh}>
                  <span className="text-[11px]">2分毎に最新オッズ更新</span>
                </Toggle>
              </div>
            </div>

            {recShown.length === 0 ? (
              <Card>
                <div className="py-6 text-center text-sm text-(--color-muted)">
                  {races.length === 0
                    ? "評価対象のレースがありません (当日の開催が無い / 全て締切済の可能性)。"
                    : recommended.length > 0 && hiddenShown.length > 0 && !onlyScore100
                      ? "表示できる勝負レースがありません (すべて非表示)。下の「非表示にしたレース」から戻せます。"
                      : onlyScore100
                        ? `勝負スコア100の勝負レースはありません${recommended.length > 0 ? ` (フィルタを外すと ${recommended.length} 件)` : ""}。`
                        : "市場との順位乖離が基準を満たす勝負レースはありませんでした。下の「その他のレース」をスコア順で確認するか、しきい値 (市場乖離スコア / 指数差フロア) を下げてください。"}
                </div>
              </Card>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                {recShown.map((r, i) => (
                  <RaceCard
                    key={keyOf(r)}
                    r={r}
                    nowMs={nowMs}
                    rank={i + 1}
                    onToggleHide={() => toggleHide(r)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* その他のレース (推奨外) — 折りたたみ */}
          {othersShown.length > 0 && (
            <div className="space-y-3">
              <button
                onClick={() => setShowOthers((v) => !v)}
                aria-expanded={showOthers}
                className="flex items-center gap-2 px-1 text-(--color-muted) hover:text-(--color-foreground) transition-colors"
              >
                <ChevronRight size={15} className={`transition-transform ${showOthers ? "rotate-90" : ""}`} aria-hidden />
                <span className="text-sm font-bold">その他のレース (推奨外)</span>
                <Badge tone="muted">{othersShown.length} 件</Badge>
                <span className="text-[11px]">{showOthers ? "隠す" : "スコア順で表示"}</span>
              </button>
              {showOthers && (
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                  {othersShown.map((r) => (
                    <RaceCard key={keyOf(r)} r={r} nowMs={nowMs} onToggleHide={() => toggleHide(r)} />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 非表示にしたレース — 一番下に集約 (折りたたみ)。各カードの「表示」で一覧に戻せる */}
          {hiddenShown.length > 0 && (
            <div className="space-y-3">
              <button
                onClick={() => setShowHidden((v) => !v)}
                aria-expanded={showHidden}
                className="flex items-center gap-2 px-1 text-(--color-muted) hover:text-(--color-foreground) transition-colors"
              >
                <ChevronRight size={15} className={`transition-transform ${showHidden ? "rotate-90" : ""}`} aria-hidden />
                <EyeOff size={14} aria-hidden />
                <span className="text-sm font-bold">非表示にしたレース</span>
                <Badge tone="muted">{hiddenShown.length} 件</Badge>
                <span className="text-[11px]">{showHidden ? "隠す" : "表示"}</span>
              </button>
              {showHidden && (
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                  {hiddenShown.map((r) => (
                    <RaceCard key={keyOf(r)} r={r} nowMs={nowMs} hidden onToggleHide={() => toggleHide(r)} />
                  ))}
                </div>
              )}
            </div>
          )}

          <p className="text-[10px] text-(--color-muted) px-1">
            ※ <b>推奨 (勝負レース)</b> = <b>市場との順位乖離</b> が基準を満たしたレース。緑枠+番号+「推奨」バッジで表示。
            勝負スコア = 市場乖離スコア (= 市場順位と Claude 順位の食い違い。例: 市場2番人気を Claude が本命視 /
            「市場5位→Claude2位」のように Claude が上位評価する馬。Claude 本命の市場順位ギャップを主軸にスコア化)。
            長期 +EV を保証するものではなく「賭ける価値の高そうなレース」の目安です。
          </p>
        </section>
      )}
    </Page>
  );
}
