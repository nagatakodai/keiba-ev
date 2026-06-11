"use client";

import type { ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// ============================================================
// recharts ベースの共通チャート (dark terminal トーン)。
// wave-2 のページ刷新エージェントはここから import して使う。
// 色は globals.css の CSS 変数に直結 (SVG の fill/stroke は var() 可)。
// ============================================================

export const CHART_COLORS = {
  positive: "var(--positive)", // emerald: 利益 / +EV
  negative: "var(--negative)", // red: 損失
  warning: "var(--warning)",   // amber
  info: "var(--info)",         // sky
  llm: "var(--llm)",           // violet: Claude/LLM 系列
  accent: "var(--accent)",
  magenta: "var(--magenta)",
  muted: "var(--muted)",
  grid: "rgba(148, 163, 184, 0.10)",
  axis: "rgba(148, 163, 184, 0.55)",
} as const;

// 系列を複数並べる時のローテーション (1本目 = ブランド emerald)
export const SERIES_PALETTE: string[] = [
  CHART_COLORS.accent,
  CHART_COLORS.info,
  CHART_COLORS.warning,
  CHART_COLORS.llm,
  CHART_COLORS.magenta,
  CHART_COLORS.negative,
];

const AXIS_TICK = { fill: "var(--muted)", fontSize: 11 } as const;

type Datum = Record<string, string | number | null | undefined>;

// ---- 共通 dark ツールチップ (tabular nums) -------------------------------

type TooltipEntry = {
  name?: string | number;
  value?: string | number;
  color?: string;
  dataKey?: string | number;
};

function DarkTooltip({
  active,
  label,
  payload,
  formatter,
}: {
  active?: boolean;
  label?: string | number;
  payload?: TooltipEntry[];
  formatter?: (value: number, key: string) => ReactNode;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-(--color-line) bg-(--color-surface-3)/95 glass px-3 py-2 shadow-[0_4px_16px_rgba(0,0,0,0.5)]">
      {label != null && label !== "" && (
        <div className="text-[10px] text-(--color-muted) font-bold mb-1">{label}</div>
      )}
      <div className="space-y-0.5">
        {payload.map((p, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            <span
              className="inline-block w-2 h-2 rounded-full shrink-0"
              style={{ background: p.color ?? CHART_COLORS.muted }}
            />
            <span className="text-(--color-muted)">{p.name}</span>
            <span className="ml-auto pl-3 tnum mono font-semibold">
              {formatter && typeof p.value === "number"
                ? formatter(p.value, String(p.dataKey ?? p.name ?? ""))
                : typeof p.value === "number"
                ? p.value.toLocaleString()
                : p.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---- TrendChart: 時系列トレンド (AreaChart + グラデ塗り) ------------------

export type TrendSeries = {
  key: string;        // data の値キー
  label?: string;     // 凡例/ツールチップ表示名 (省略時 key)
  color?: string;     // 省略時 SERIES_PALETTE から
};

export function TrendChart({
  data,
  series,
  xKey = "x",
  height = 220,
  yDomain,
  valueFormatter,
  xTickFormatter,
  referenceY,
  showLegend = false,
}: {
  data: Datum[];
  series: TrendSeries[];
  xKey?: string;
  height?: number;
  yDomain?: [number | "auto", number | "auto"];
  valueFormatter?: (value: number, key: string) => ReactNode;
  xTickFormatter?: (value: string | number) => string;
  referenceY?: number; // 基準線 (例: ROI 100%)
  showLegend?: boolean;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
        <defs>
          {series.map((s, i) => {
            const color = s.color ?? SERIES_PALETTE[i % SERIES_PALETTE.length];
            return (
              <linearGradient key={s.key} id={`trend-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.28} />
                <stop offset="100%" stopColor={color} stopOpacity={0.02} />
              </linearGradient>
            );
          })}
        </defs>
        <CartesianGrid stroke={CHART_COLORS.grid} vertical={false} />
        <XAxis
          dataKey={xKey}
          tick={AXIS_TICK}
          tickFormatter={xTickFormatter}
          stroke={CHART_COLORS.grid}
          tickLine={false}
        />
        <YAxis
          tick={AXIS_TICK}
          stroke="transparent"
          tickLine={false}
          domain={yDomain ?? ["auto", "auto"]}
          width={56}
        />
        <Tooltip
          content={<DarkTooltip formatter={valueFormatter} />}
          cursor={{ stroke: CHART_COLORS.axis, strokeDasharray: "3 3" }}
        />
        {showLegend && (
          <Legend wrapperStyle={{ fontSize: 11, color: "var(--muted)" }} iconType="circle" iconSize={8} />
        )}
        {referenceY != null && (
          <ReferenceLine y={referenceY} stroke={CHART_COLORS.axis} strokeDasharray="4 4" />
        )}
        {series.map((s, i) => {
          const color = s.color ?? SERIES_PALETTE[i % SERIES_PALETTE.length];
          return (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label ?? s.key}
              stroke={color}
              strokeWidth={2}
              fill={`url(#trend-${s.key})`}
              dot={false}
              activeDot={{ r: 3.5, strokeWidth: 0 }}
              connectNulls
            />
          );
        })}
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ---- ROIBars: 正負で色分けする棒グラフ (ROI / 損益) -----------------------

export function ROIBars({
  data,
  height = 220,
  baseline = 0,
  valueFormatter,
  labelKey = "label",
  valueKey = "value",
}: {
  // 例: [{ label: "EV束", value: -48.5 }, ...] (value は baseline 比の偏差でも実値でも可)
  data: Datum[];
  height?: number;
  baseline?: number; // この値を上回れば positive 色 (ROI% なら 100 を渡す)
  valueFormatter?: (value: number, key: string) => ReactNode;
  labelKey?: string;
  valueKey?: string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
        <CartesianGrid stroke={CHART_COLORS.grid} vertical={false} />
        <XAxis dataKey={labelKey} tick={AXIS_TICK} stroke={CHART_COLORS.grid} tickLine={false} />
        <YAxis tick={AXIS_TICK} stroke="transparent" tickLine={false} width={56} />
        <Tooltip
          content={<DarkTooltip formatter={valueFormatter} />}
          cursor={{ fill: "rgba(148, 163, 184, 0.06)" }}
        />
        <ReferenceLine y={baseline} stroke={CHART_COLORS.axis} strokeDasharray="4 4" />
        <Bar dataKey={valueKey} radius={[4, 4, 0, 0]} maxBarSize={48}>
          {data.map((d, i) => {
            const v = Number(d[valueKey] ?? 0);
            return (
              <Cell
                key={i}
                fill={v >= baseline ? CHART_COLORS.positive : CHART_COLORS.negative}
                fillOpacity={0.85}
              />
            );
          })}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---- OddsTimeline: 馬ごとのオッズ時系列 (capture stage 横断) ---------------

export type OddsHorse = {
  key: string;        // data の値キー (例: "h7" や馬番文字列)
  label?: string;     // 表示名 (例: "7 サンプルホース")
};

export function OddsTimeline({
  data,
  horses,
  xKey = "stage",
  height = 260,
  logScale = false,
  topFinishers = [],
  valueFormatter,
}: {
  // 例: [{ stage: "score", h1: 2.4, h2: 11.8 }, { stage: "bet", ... }, { stage: "final", ... }]
  data: Datum[];
  horses: OddsHorse[];
  xKey?: string;
  height?: number;
  logScale?: boolean; // オッズは裾が重いので log 表示オプション
  topFinishers?: string[]; // 上位 3 着の horse key を渡すと金/銀/銅で強調、他は減光
  valueFormatter?: (value: number, key: string) => ReactNode;
}) {
  const medal = ["#fbbf24", "#cbd5e1", "#d97706"]; // 1着=金 2着=銀 3着=銅
  const fmt = valueFormatter ?? ((v: number) => `${v.toFixed(1)}倍`);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -8 }}>
        <CartesianGrid stroke={CHART_COLORS.grid} vertical={false} />
        <XAxis dataKey={xKey} tick={AXIS_TICK} stroke={CHART_COLORS.grid} tickLine={false} />
        <YAxis
          tick={AXIS_TICK}
          stroke="transparent"
          tickLine={false}
          width={52}
          scale={logScale ? "log" : "auto"}
          domain={logScale ? ["auto", "auto"] : [0, "auto"]}
          allowDataOverflow={false}
        />
        <Tooltip
          content={<DarkTooltip formatter={fmt} />}
          cursor={{ stroke: CHART_COLORS.axis, strokeDasharray: "3 3" }}
        />
        {horses.map((h, i) => {
          const finishIdx = topFinishers.indexOf(h.key);
          const highlighted = finishIdx >= 0;
          const color = highlighted
            ? medal[finishIdx] ?? CHART_COLORS.positive
            : SERIES_PALETTE[i % SERIES_PALETTE.length];
          return (
            <Line
              key={h.key}
              type="monotone"
              dataKey={h.key}
              name={h.label ?? h.key}
              stroke={color}
              strokeWidth={highlighted ? 2.5 : 1.5}
              strokeOpacity={topFinishers.length > 0 && !highlighted ? 0.35 : 0.9}
              dot={{ r: highlighted ? 3 : 2, strokeWidth: 0, fill: color }}
              activeDot={{ r: 4, strokeWidth: 0 }}
              connectNulls
            />
          );
        })}
      </LineChart>
    </ResponsiveContainer>
  );
}
