import type { PropsWithChildren, ReactNode } from "react";

export function Page({ children }: PropsWithChildren) {
  return <div className="max-w-7xl mx-auto px-4 py-6 space-y-5">{children}</div>;
}

export function PageHeader({
  title,
  subtitle,
  eyebrow,
  right,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-4 pb-2 border-b-2 border-(--color-accent)">
      <div className="flex items-stretch gap-3">
        <span className="inline-block w-1.5 bg-(--color-highlight) self-stretch" />
        <div>
          {eyebrow && (
            <div className="text-[10px] font-bold tracking-widest uppercase text-(--color-highlight) mb-0.5">
              {eyebrow}
            </div>
          )}
          <h1 className="text-2xl font-black tracking-tight leading-tight">{title}</h1>
          {subtitle && <p className="text-xs text-(--color-muted) mt-1">{subtitle}</p>}
        </div>
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}

export function Card({
  title,
  right,
  children,
  className = "",
  tone = "default",
}: PropsWithChildren<{
  title?: ReactNode;
  right?: ReactNode;
  className?: string;
  tone?: "default" | "active" | "alert";
}>) {
  const headBg =
    tone === "active"
      ? "bg-sky-50 border-b-sky-200"
      : tone === "alert"
      ? "bg-orange-50 border-b-orange-200"
      : "bg-(--color-section-head) border-b-(--color-line)";
  const headBar =
    tone === "active"
      ? "bg-sky-500"
      : tone === "alert"
      ? "bg-(--color-highlight)"
      : "bg-(--color-accent)";
  return (
    <section
      className={`bg-(--color-panel) border border-(--color-line) shadow-[0_1px_2px_rgba(0,0,0,0.04)] ${className}`}
    >
      {(title || right) && (
        <header
          className={`flex items-center justify-between gap-2 px-4 py-2 border-b ${headBg}`}
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className={`inline-block w-1 h-4 ${headBar} shrink-0`} />
            <h2 className="text-sm font-bold tracking-tight">{title}</h2>
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "default",
  accentTone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "good" | "warn" | "bad" | "info";
  // optional 左 border の色のみ別系統で指定 (value 色は tone が制御)。
  // 例: tone=default (数値は標準色) + accentTone="info" (左 border は青)。
  accentTone?: "default" | "good" | "warn" | "bad" | "info" | "muted" | "magenta";
}) {
  const color =
    tone === "good"
      ? "text-(--color-good)"
      : tone === "warn"
      ? "text-(--color-warn)"
      : tone === "bad"
      ? "text-(--color-bad)"
      : tone === "info"
      ? "text-(--color-info)"
      : "text-(--color-foreground)";
  const accentSrc = accentTone ?? tone;
  const accent =
    accentSrc === "good"
      ? "border-l-(--color-good)"
      : accentSrc === "warn"
      ? "border-l-(--color-warn)"
      : accentSrc === "bad"
      ? "border-l-(--color-bad)"
      : accentSrc === "info"
      ? "border-l-(--color-info)"
      : accentSrc === "muted"
      ? "border-l-(--color-line)"
      : accentSrc === "magenta"
      ? "border-l-fuchsia-500"
      : "border-l-(--color-accent)";
  return (
    <div
      className={`bg-white border border-(--color-line) border-l-4 ${accent} px-4 py-3 shadow-[0_1px_2px_rgba(0,0,0,0.04)]`}
    >
      <div className="text-[11px] text-(--color-muted) font-bold tracking-wider uppercase">{label}</div>
      <div className={`text-2xl font-black mt-1 tabnum tracking-tight ${color}`}>{value}</div>
      {hint && <div className="text-[11px] text-(--color-muted) mt-1">{hint}</div>}
    </div>
  );
}

export type BadgeTone =
  | "default"
  | "good"
  | "warn"
  | "bad"
  | "magenta"
  | "rose"
  | "muted"
  | "info"
  | "pending";

export function Badge({
  children,
  tone = "default",
}: PropsWithChildren<{ tone?: BadgeTone }>) {
  const cls = {
    default: "bg-white text-(--color-foreground) border-(--color-line)",
    // 的中/不的中 などの結果系で「鮮やか」に視認できるよう一段濃いめに
    // (2026-05-29 ユーザ指示)。bg 200 / border 500 / text 900。
    good: "bg-emerald-200 text-emerald-900 border-emerald-500 font-semibold",
    warn: "bg-amber-50 text-amber-800 border-amber-400",
    bad: "bg-red-200 text-red-900 border-red-500 font-semibold",
    magenta: "bg-fuchsia-50 text-fuchsia-800 border-fuchsia-300",
    // Plan F の最終買い目を示す赤ピンク。border-400 で他より一段強調。
    rose: "bg-rose-50 text-rose-800 border-rose-400",
    info: "bg-sky-100 text-sky-900 border-sky-500 font-semibold",
    // 白に近い淡いオレンジ。"結果待ち" 等の "進行中だが警告ではない" 状態用。
    pending: "bg-orange-50 text-orange-700 border-orange-200",
    muted: "bg-(--color-panel-2) text-(--color-muted) border-(--color-line)",
  }[tone];
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 border text-[11px] font-bold ${cls}`}>
      {children}
    </span>
  );
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  className = "",
  disabled,
  ...rest
}: PropsWithChildren<
  React.ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: "primary" | "ghost" | "danger";
    size?: "sm" | "md" | "lg";
  }
>) {
  const sizeCls = {
    sm: "px-2.5 py-1 text-xs",
    md: "px-4 py-2 text-sm",
    lg: "px-6 py-2.5 text-base min-w-[140px]",
  }[size];
  const base =
    `inline-flex items-center justify-center gap-2 font-bold tracking-tight transition-all disabled:opacity-40 disabled:cursor-not-allowed border shadow-[0_1px_2px_rgba(0,0,0,0.06)] active:shadow-none active:translate-y-[1px] ${sizeCls}`;
  const styles = {
    primary:
      "bg-(--color-highlight) text-white border-(--color-highlight) hover:brightness-110",
    ghost:
      "bg-white border-(--color-line) hover:bg-(--color-panel-2) text-(--color-foreground)",
    danger:
      "bg-(--color-bad) text-white border-(--color-bad) hover:brightness-110",
  }[variant];
  return (
    <button {...rest} disabled={disabled} className={`${base} ${styles} ${className}`}>
      {children}
    </button>
  );
}

export function Input({
  label,
  hint,
  className = "",
  ...rest
}: React.InputHTMLAttributes<HTMLInputElement> & { label?: string; hint?: string }) {
  return (
    <label className="block">
      {label && <span className="block text-xs text-(--color-muted) font-medium mb-1">{label}</span>}
      <input
        {...rest}
        className={`w-full bg-white border border-(--color-line) px-3 py-2 text-sm placeholder:text-(--color-muted) focus:outline-none focus:border-(--color-accent) ${className}`}
      />
      {hint && <span className="block text-xs text-(--color-muted) mt-1">{hint}</span>}
    </label>
  );
}

export function Select({
  label,
  hint,
  className = "",
  children,
  ...rest
}: React.SelectHTMLAttributes<HTMLSelectElement> & { label?: string; hint?: string }) {
  return (
    <label className="block">
      {label && <span className="block text-xs text-(--color-muted) font-medium mb-1">{label}</span>}
      <select
        {...rest}
        className={`w-full bg-white border border-(--color-line) px-3 py-2 text-sm focus:outline-none focus:border-(--color-accent) ${className}`}
      >
        {children}
      </select>
      {hint && <span className="block text-xs text-(--color-muted) mt-1">{hint}</span>}
    </label>
  );
}

// Plan A / B / C (EV 枠), 当て枠 Plan H1 / H2, 最終買い目 Plan F を一貫した色で示すヘルパ。
//   A (本命: 5点バランス)    → info (青 / sky)
//   B (高 EV 集中)            → magenta (ピンク紫 / fuchsia)
//   C (広め保険)              → good (緑 / emerald)
//   H1 (当て枠: 確率最優先)   → amber/gold (オレンジ寄り。highlight CTA と色相が近い)
//   H2 (当て枠: 確率 + P×O ≥ 1.0) → violet (青紫)
//   F (最終買い目 union)      → rose (赤ピンク。実際に賭ける plan として最も目立つ)
export type PlanLetter = "A" | "B" | "C" | "G" | "H1" | "H2" | "F";

export function planTone(plan: PlanLetter): BadgeTone {
  switch (plan) {
    case "A":
      return "info";
    case "B":
      return "magenta";
    case "C":
      return "good";
    case "G":
      return "magenta";
    case "H1":
      return "warn";
    case "H2":
      return "magenta";
    case "F":
      return "rose";
  }
}

export function planAccentClass(plan: PlanLetter): string {
  switch (plan) {
    case "A":
      return "text-sky-700";
    case "B":
      return "text-fuchsia-700";
    case "C":
      return "text-emerald-700";
    case "G":
      return "text-purple-700";
    case "H1":
      return "text-amber-700";
    case "H2":
      return "text-violet-700";
    case "F":
      return "text-rose-700";
  }
}

export function planBarClass(plan: PlanLetter): string {
  switch (plan) {
    case "A":
      return "bg-sky-500";
    case "B":
      return "bg-fuchsia-500";
    case "C":
      return "bg-emerald-500";
    case "G":
      return "bg-purple-500";
    case "H1":
      return "bg-amber-500";
    case "H2":
      return "bg-violet-500";
    case "F":
      return "bg-rose-500";
  }
}

// API レスポンスの "Plan A" / "Plan H1" / "Plan F" など文字列を PlanLetter に。
// 未知の文字列は null を返す。
export function parsePlanLabel(label: string): PlanLetter | null {
  const m = label.match(/Plan\s+(A|B|C|G|H1|H2|F)/i);
  if (!m) return null;
  return m[1].toUpperCase() as PlanLetter;
}

export function raceClassTone(
  c: string,
): "good" | "warn" | "magenta" | "muted" | "default" {
  const u = (c ?? "").toUpperCase();
  if (u.includes("GP") || u.includes("G1")) return "magenta";
  if (u.includes("G2") || u.includes("G3")) return "warn";
  if (u.includes("F1")) return "good";
  if (u.includes("F2") || u.includes("チャレンジ")) return "muted";
  return "default";
}

export function tierLabel(tier: string): string {
  return ({ honsen: "本線", chuana: "中穴", oana: "大穴", minus: "−EV" } as Record<string, string>)[
    tier
  ] ?? tier;
}

export function tierTone(tier: string): "good" | "warn" | "magenta" | "muted" | "default" {
  return ({ honsen: "good", chuana: "warn", oana: "magenta", minus: "muted" } as const)[
    tier as "honsen" | "chuana" | "oana" | "minus"
  ] ?? "default";
}

export function fmtPct(x: number, digits = 2): string {
  return `${(x * 100).toFixed(digits)}%`;
}

export function fmtKey(key: number[]): string {
  return key.join("-");
}

export function fmtYen(n: number): string {
  return `¥${n.toLocaleString()}`;
}

export function pxoTone(pxo: number): "good" | "warn" | "magenta" | "muted" | "default" {
  if (pxo >= 3.0) return "magenta";
  if (pxo >= 1.5) return "good";
  if (pxo >= 1.05) return "good";
  if (pxo >= 0.95) return "default";
  return "muted";
}

// 全ての時刻表示は JST 固定。サーバ (Vercel: UTC) ⇄ クライアント (ユーザのローカル TZ)
// で表示がズレないように、`timeZone: "Asia/Tokyo"` を明示する。
const JST_DATETIME_OPTS: Intl.DateTimeFormatOptions = {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
};

const JST_TIME_OPTS: Intl.DateTimeFormatOptions = {
  timeZone: "Asia/Tokyo",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
};

function toDate(unix: number): Date {
  // 秒 / ミリ秒の両方を受け取る (auto-detect)。
  return new Date(unix > 1e12 ? unix : unix * 1000);
}

export function fmtTs(unix?: number | null): string {
  if (!unix) return "—";
  return toDate(unix).toLocaleString("ja-JP", JST_DATETIME_OPTS);
}

export function fmtTime(unix?: number | null): string {
  if (!unix) return "—";
  return toDate(unix).toLocaleTimeString("ja-JP", JST_TIME_OPTS);
}

// 現在の JST 日付を "YYYY-MM-DD" で返す。saved_at が JST naive ISO (先頭 10 文字が
// JST 日付) であることを前提に、当日 / 過去のフィルタに使う。
const JST_DATE_FMT = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
export function todayJST(nowMs: number = Date.now()): string {
  return JST_DATE_FMT.format(new Date(nowMs));
}

// `saved_at` (JST naive ISO) の頭 10 文字 ("YYYY-MM-DD") を抜き出す。
// 不正形式は空文字を返す。
export function savedAtDate(s: string | null | undefined): string {
  if (!s) return "";
  return s.slice(0, 10);
}

// バックエンド (ev-api) の saved_at は JST 時刻として書かれている前提
// (Cloud Run は TZ=Asia/Tokyo を指定 / Python の datetime.now() が JST を返す)。
//
// naive ISO ("YYYY-MM-DD[T ]HH:MM:SS") を JS Date が LOCAL TZ で解釈すると、
// SSR (Vercel UTC) と CSR (ブラウザ JST) で結果が 9 時間ズレる。これを避ける
// ために、TZ 表記なしの場合は正規表現で要素を取り出し、JST (UTC+9) として
// UTC 時刻を組み立てる。TZ 表記 (Z または ±HH:MM) があればそのまま信用する。
export function parseServerDateTime(s?: string | null): Date | null {
  if (!s) return null;
  const trimmed = s.trim();
  if (/[Zz]|[+-]\d{2}:?\d{2}$/.test(trimmed)) {
    const d = new Date(trimmed);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const m = trimmed.match(
    /^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2})(?::(\d{2}))?/,
  );
  if (!m) return null;
  const [, Y, M, D, hh, mm, ss] = m;
  return new Date(
    Date.UTC(
      Number(Y),
      Number(M) - 1,
      Number(D),
      Number(hh) - 9,
      Number(mm),
      ss ? Number(ss) : 0,
    ),
  );
}

export function fmtServerDateTime(s?: string | null): string {
  const d = parseServerDateTime(s);
  if (!d) return s ?? "—";
  return d.toLocaleString("ja-JP", JST_DATETIME_OPTS);
}

// JST naive ISO (バックエンド由来) を相対時刻にする。1 分未満 / N 分前 / N 時間前 / N 日前。
export function fmtRelativeFromNow(s?: string | null, nowMs: number = Date.now()): string {
  const d = parseServerDateTime(s);
  if (!d) return "—";
  const diffSec = Math.max(0, (nowMs - d.getTime()) / 1000);
  if (diffSec < 60) return "たった今";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 時間前`;
  return `${Math.floor(diffSec / 86400)} 日前`;
}

// サンプル数 (race_count) を信頼度ラベルに。CLAUDE.md の保守化原則に基づく:
// 30 未満は判断材料不足、30〜99 は中程度、100+ で信頼可。
export function calibrationConfidence(raceCount: number): {
  label: string;
  tone: BadgeTone;
} {
  if (raceCount < 30) return { label: "サンプル不足 (n<30)", tone: "bad" };
  if (raceCount < 100) return { label: "中程度 (n<100)", tone: "warn" };
  return { label: "十分サンプル", tone: "good" };
}

// 締切時刻と結果取得状況からレース進行の状態を導出。
// 色は他コンポーネントとの整合を取り、Badge tone を返す:
//   予測中 (close_at 不明・解析直後など)  → info (青)
//   予定 (締切 5 分以上前)                → muted (灰)
//   締切 5 分前 (1 〜 5 分前)              → warn (黄)
//   締切直前 (1 分以下)                   → bad (赤)
//   発走待ち (締切後〜発走前 ≈ 0〜60 秒)  → info (青)
//   発走中 (発走後 5 分以内)              → info (青)
//   結果待ち (発走後 5 分以上経過)         → pending (白に近い淡オレンジ)
//   結果あり                              → default (白)
// raceTimingStatus の tone を、行/カードに敷く控えめな背景クラスに変換する。
// バッジと同じ色相 (青/赤/淡オレンジ等) を弱いアルファで敷くことで、
// 一覧でも状態がパッと見で判別できるようにする。
export function raceTimingRowBg(tone: BadgeTone): string {
  return (
    {
      default: "",
      // 的中/不的中 行 bg も連動して一段濃く (badge と合わせて鮮やかに)
      good: "bg-emerald-100/80",
      warn: "bg-amber-50/50",
      bad: "bg-red-100/70",
      magenta: "bg-fuchsia-50/40",
      rose: "bg-rose-50/60",
      info: "bg-sky-50/50",
      pending: "bg-orange-50/50",
      // 見送り (投票束が空) はグレー系で「不参加」を視覚的に明示
      // (2026-05-29 ユーザ指示)。
      muted: "bg-slate-100/70",
    } satisfies Record<BadgeTone, string>
  )[tone];
}

export function raceTimingStatus(
  closeAt: number | null,
  startAt: number | null,
  hasResult: boolean,
  nowMs: number,
): { label: string; tone: BadgeTone } {
  if (hasResult) return { label: "結果", tone: "default" };
  // 解析は終わっているが close_at がまだ届いていない / 古いスナップショット。
  // 「結果待ち」だと「レース後」と誤読されるので「予測中」を出す。
  if (closeAt == null) return { label: "予測中", tone: "info" };
  const nowSec = nowMs / 1000;
  const secsToClose = closeAt - nowSec;
  // start_at が無ければ close_at を発走時刻と見做して回す。
  const secsToStart = (startAt ?? closeAt) - nowSec;
  if (secsToClose > 5 * 60) return { label: "予定", tone: "muted" };
  if (secsToClose > 60) return { label: "5分前", tone: "warn" };
  if (secsToClose > 0) return { label: "直前", tone: "bad" };
  if (secsToStart > 0) return { label: "発走待ち", tone: "info" };
  if (secsToStart > -5 * 60) return { label: "発走中", tone: "info" };
  return { label: "結果待ち", tone: "pending" };
}
