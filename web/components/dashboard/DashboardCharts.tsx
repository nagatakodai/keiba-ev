"use client";

// ダッシュボード用チャートの client ラッパ。
// page.tsx (server component) が cal.races から累積系列を計算して plain props で渡し、
// 描画だけをここ (recharts ベースの components/charts.tsx) が担う。
// 系列色は従来の意味を維持: EV束 = sky (info) / 3連単束 = fuchsia (magenta)。

import { CHART_COLORS, ROIBars, TrendChart } from "@/components/charts";

export type BundleTrendPoint = {
  x: string;      // saved_at 由来の "MM-DD" ラベル
  evNet: number;  // EV束 累積収支 (¥)
  tNet: number;   // 3連単束 累積収支 (¥)
  evRoi: number;  // EV束 累積回収率 (%)
  tRoi: number;   // 3連単束 累積回収率 (%)
};

export type BundleRoiBarDatum = {
  label: string;
  value: number; // 回収率 (%)
};

function fmtSignedYen(v: number): string {
  return `${v < 0 ? "-" : "+"}¥${Math.abs(Math.round(v)).toLocaleString()}`;
}

const NET_SERIES = [
  { key: "evNet", label: "EV束 (実弾既定)", color: CHART_COLORS.info },
  { key: "tNet", label: "3連単束", color: CHART_COLORS.magenta },
];

const ROI_SERIES = [
  { key: "evRoi", label: "EV束 (実弾既定)", color: CHART_COLORS.info },
  { key: "tRoi", label: "3連単束", color: CHART_COLORS.magenta },
];

// 累積収支 (¥) の推移。0 円を基準線に。
export function NetTrendChart({ data }: { data: BundleTrendPoint[] }) {
  return (
    <TrendChart
      data={data}
      series={NET_SERIES}
      xKey="x"
      height={230}
      referenceY={0}
      showLegend
      valueFormatter={(v) => fmtSignedYen(v)}
    />
  );
}

// 累積回収率 (%) の推移。100% (break-even) を基準線に。
export function RoiTrendChart({ data }: { data: BundleTrendPoint[] }) {
  return (
    <TrendChart
      data={data}
      series={ROI_SERIES}
      xKey="x"
      height={230}
      referenceY={100}
      showLegend
      valueFormatter={(v) => `${v.toFixed(1)}%`}
    />
  );
}

// 系列別の最終回収率 (%) を 100% 基準で正負色分け (emerald / red)。
export function BundleRoiBarsChart({ data }: { data: BundleRoiBarDatum[] }) {
  return (
    <ROIBars
      data={data}
      baseline={100}
      height={230}
      valueFormatter={(v) => `${v.toFixed(1)}%`}
    />
  );
}
