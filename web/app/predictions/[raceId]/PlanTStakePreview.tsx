"use client";

// Plan T「全力的中モード」束の掛金倍率プレビュー (client island)。
// 予想詳細ページの PlanTCard (server 描画) 内に差し込む。倍率 N を選ぶと投資総額・各脚 stake・
// 的中時払戻を ×N して表示する見積り計算機。実投票は watch-auto の「掛金倍率 — Plan T (×N)」で
// 同じ整数倍が各脚に適用される (src/oddspark_bet.py / ipat_bet.py の _apply_stake_multiplier)。
import { useState } from "react";
import { Stat } from "@/components/ui";
import type { TrifectaHitmaxBundle } from "@/lib/api";

const PRESETS = [1, 2, 3, 5, 10];

export function PlanTStakePreview({ bundle }: { bundle: TrifectaHitmaxBundle }) {
  const [raw, setRaw] = useState("1");
  const parsed = parseFloat(raw);
  // 整数倍に snap: vote 時の _apply_stake_multiplier (m = max(1, round(N))・stake×m で ¥100 単位
  // 不変) と一致させ、トリガミ保証 (各脚 payout ≥ 投資総額×margin) を崩さない見積りにする。
  const m =
    Number.isFinite(parsed) && parsed > 0 ? Math.max(1, Math.round(parsed)) : 1;
  const legs = bundle.legs ?? [];
  const total = bundle.total_stake * m;
  const osum = bundle.odds_summary;

  return (
    <div className="mt-4 rounded-lg border border-(--color-line) p-3">
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <span className="text-[10px] font-bold text-(--color-muted) tracking-wider uppercase">
          掛金倍率プレビュー
        </span>
        {PRESETS.map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => setRaw(String(p))}
            className={`px-2 py-0.5 rounded text-sm border transition-colors ${
              m === p
                ? "border-(--color-warn) text-(--color-warn) font-bold"
                : "border-(--color-line) text-(--color-muted) hover:text-(--color-fg)"
            }`}
          >
            ×{p}
          </button>
        ))}
        <input
          type="number"
          min={1}
          step={1}
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          className="w-16 px-2 py-0.5 rounded border border-(--color-line) bg-transparent text-sm tabnum"
          aria-label="掛金倍率"
        />
        <span className="text-xs text-(--color-muted)">
          整数倍に丸め (×{m})・トリガミ保証維持
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Stat label={`投資総額 (×${m})`} value={`¥${total.toLocaleString()}`} />
        {osum && (
          <Stat
            label={`当たれば払戻 (×${m})`}
            value={`¥${(osum.min_payout * m).toLocaleString()}〜¥${(
              osum.max_payout * m
            ).toLocaleString()}`}
          />
        )}
        {bundle.min_payout_ratio != null && (
          <Stat
            label="最小 払戻/投資"
            value={`×${bundle.min_payout_ratio.toFixed(2)} (倍率で不変)`}
          />
        )}
      </div>
      {legs.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm tabnum table-zebra">
            <thead className="text-left text-(--color-muted) text-xs">
              <tr className="border-b border-(--color-line)">
                <th className="py-1.5 pr-3">買い目</th>
                <th className="py-1.5 pr-3 text-right">オッズ</th>
                <th className="py-1.5 pr-3 text-right">配分 (×{m})</th>
                <th className="py-1.5 pr-3 text-right">的中時払戻 (×{m})</th>
              </tr>
            </thead>
            <tbody>
              {legs.map((l) => (
                <tr
                  key={l.key.join("-")}
                  className="border-b border-(--color-line)/60"
                >
                  <td className="py-1 pr-3 mono">{l.key.join("-")}</td>
                  <td className="py-1 pr-3 text-right">{l.odds.toFixed(1)}</td>
                  <td className="py-1 pr-3 text-right font-bold">
                    ¥{(l.stake * m).toLocaleString()}
                  </td>
                  <td className="py-1 pr-3 text-right text-(--color-good)">
                    ¥{(l.payout_if_hit * m).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-2 text-xs text-(--color-muted)">
        実投票は watch-auto の <b>掛金倍率 — Plan T (×N)</b> で同じ整数倍が各脚 stake に適用されます
        (per-race 上限も倍率連動)。ここは見積りプレビューです。
      </p>
    </div>
  );
}
