"use client";

import { useState } from "react";
import type { ClaudeBundleAggregate } from "@/lib/api";
import { Stat, fmtPct, fmtYen } from "@/components/ui";

// 3連単束セクションの統計カード (mode 切替つき)。
// snapshot の recommended_bundle_t.mode で「的中モード (hit)」と「回収モード (recovery)」を
// 判別できる (mode 欠落の旧 snapshot は API 側で "hit" に正規化済) ので、ダッシュボードを
// mode 別に絞って見られるようにする (2026-06-12 ユーザ要望: 的中モードのみで見たい)。
// 既定は「的中」。チャート (累積収支等) は従来通り mode 混在の全体系列のまま。

type ModeKey = "hit" | "recovery" | "all";

const MODE_LABELS: Array<[ModeKey, string]> = [
  ["hit", "的中モード"],
  ["recovery", "回収モード"],
  ["all", "全体"],
];

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}

export function TrifectaModeStats({
  all,
  hit,
  recovery,
}: {
  all?: ClaudeBundleAggregate;
  hit?: ClaudeBundleAggregate;
  recovery?: ClaudeBundleAggregate;
}) {
  const [mode, setMode] = useState<ModeKey>("hit");
  // 旧 API (trifecta_bundle_modes 無し) では hit/recovery が undefined → 全体に fallback
  const agg = (mode === "hit" ? hit : mode === "recovery" ? recovery : all) ?? all;
  const participated = (agg?.participated_races ?? 0) > 0;
  const pay = agg ? agg.payout_final ?? agg.payout : 0;
  const roi = agg ? agg.roi_final ?? agg.roi : 0;
  const pl = agg ? pay - agg.stake : 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1.5 px-1">
        {MODE_LABELS.map(([key, label]) => {
          const active = mode === key;
          return (
            <button
              key={key}
              type="button"
              onClick={() => setMode(key)}
              className={`mono text-[10px] tracking-wider px-2.5 py-1 rounded-full border transition-colors ${
                active
                  ? "border-fuchsia-400/60 bg-fuchsia-500/15 text-fuchsia-200"
                  : "border-(--color-line) text-(--color-muted) hover:text-(--color-fg) hover:border-(--color-line-strong)"
              }`}
            >
              {label}
            </button>
          );
        })}
        <span className="text-[10px] text-(--color-muted) ml-1">
          ※ mode は snapshot に記録済 (旧 snapshot は的中モード相当)
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="的中率"
          value={!agg || !participated ? "—" : fmtPct(agg.hit_rate, 1)}
          hint={
            agg && participated
              ? `${agg.hits} 的中 / ${agg.participated_races} 参加`
              : "賭けたレースなし"
          }
          tone="default"
          accentTone="magenta"
        />
        <Stat
          label="回収率"
          value={!agg || !participated ? "—" : fmtRoiPct(roi)}
          hint={
            agg && participated
              ? `賭金 ${fmtYen(agg.stake)} → 払戻(最終) ${fmtYen(pay)}`
              : "—"
          }
          tone={!agg || !participated ? "default" : roi > 1 ? "default" : "bad"}
          accentTone="magenta"
        />
        <Stat
          label="収支"
          value={!agg || !participated ? "—" : `${pl >= 0 ? "+" : ""}${fmtYen(pl)}`}
          hint={agg ? `参加 ${agg.participated_races} / 集計 ${agg.races}` : "—"}
          tone={!agg || !participated ? "default" : pl < 0 ? "bad" : "default"}
          accentTone="magenta"
        />
        <Stat
          label="見送りレース数"
          value={agg?.skipped_races ?? 0}
          hint={
            agg && agg.races > 0
              ? `見送り率 ${Math.round((agg.skipped_races / agg.races) * 100)}%`
              : "—"
          }
          accentTone="muted"
        />
      </div>
    </div>
  );
}
