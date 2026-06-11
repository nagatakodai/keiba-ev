"use client";

// 3連単的中モード束の掛金倍率プレビュー (client island)。
// 予想詳細ページの TrifectaCard (server 描画) 内に差し込む。倍率 N (小数可) を選ぶと各脚 stake を
// ×N して ¥100 単位で切り捨て (floor) し、投資総額・的中時払戻・最小払戻比を再計算して表示する
// 見積り計算機。実投票も同じ floor (src/oddspark_bet.py / ipat_bet.py の _apply_stake_multiplier)。
import { useState } from "react";
import { Calculator, TriangleAlert } from "lucide-react";
import { Stat } from "@/components/ui";
import type { TrifectaHitmaxBundle } from "@/lib/api";

const PRESETS = [1, 1.5, 2, 3, 5];
const TORIGAMI_DEFAULT = 1.1; // src/portfolio.py TORIGAMI_MARGIN (古い snapshot で torigami_margin 欠落時)

// stake × 倍率 を ¥100 単位で切り捨て (floor)。¥100 未満は 0 (= 脚を除去)。
// vote 時の _apply_stake_multiplier (bughunt #4 修正後) と一致 — 旧実装の max(100, ...)
// 張り付けは倍率<1 で実投票 (脚除去) と乖離した見積りを出していた (2026-06-11 第5R)。
function floorStake(stake: number, mult: number): number {
  return Math.floor((stake * mult) / 100) * 100;
}

export function TrifectaStakePreview({ bundle }: { bundle: TrifectaHitmaxBundle }) {
  const [raw, setRaw] = useState("1");
  const parsed = parseFloat(raw);
  // 小数倍をそのまま使う (整数 snap しない)。NaN/非正は ×1。
  const mult = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
  const legs = bundle.legs ?? [];

  // 各脚を floor して再計算 (floor で脚間比率が動くので payout/total/最小比は base の単純 ×mult では出せない)。
  // ¥100 未満になる脚は実投票どおり除去して total/minPayout/比率から外す。
  const scaled = legs
    .map((l) => {
      const stake = floorStake(l.stake, mult);
      return { ...l, stake, payout: Math.round(l.odds * stake) };
    })
    .filter((l) => l.stake >= 100);
  const dropped = legs.length - scaled.length;
  const total = scaled.reduce((a, l) => a + l.stake, 0);
  const payouts = scaled.map((l) => l.payout);
  const minPayout = payouts.length ? Math.min(...payouts) : 0;
  const maxPayout = payouts.length ? Math.max(...payouts) : 0;
  // floor 後の実トリガミ比 = min(払戻) / 投資総額 (整数倍と違い 1.0 を割り得るので実値を出す)。
  const ratio = total > 0 ? minPayout / total : 0;
  const margin = bundle.torigami_margin ?? TORIGAMI_DEFAULT;
  const ratioOk = ratio >= margin;

  return (
    <div className="mt-4 rounded-xl border border-(--color-line) bg-(--color-surface-2) p-3">
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <span className="inline-flex items-center gap-1.5 text-[10px] font-bold text-(--color-muted) tracking-widest uppercase">
          <Calculator className="w-3.5 h-3.5 shrink-0" aria-hidden />
          掛金倍率プレビュー
        </span>
        {PRESETS.map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => setRaw(String(p))}
            className={`px-2 py-0.5 rounded-md text-sm border transition-colors tnum ${
              mult === p
                ? "bg-amber-500/15 border-amber-500/40 text-amber-300 font-bold"
                : "border-(--color-line) text-(--color-muted) hover:text-(--color-foreground) hover:border-white/25"
            }`}
          >
            ×{p}
          </button>
        ))}
        <input
          type="number"
          min={0.1}
          step={0.1}
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          className="w-16 px-2 py-0.5 rounded-md border border-(--color-line) bg-(--color-card) text-sm tnum focus:outline-none focus:border-(--color-accent)"
          aria-label="掛金倍率"
        />
        <span className="text-xs text-(--color-muted)">
          ×{mult.toLocaleString()}・各脚を ¥100 単位で切り捨て (¥100 未満の脚は除去)
        </span>
      </div>
      {dropped > 0 && (
        <p className="mb-3 text-xs text-amber-300 flex items-center gap-1.5">
          <TriangleAlert className="w-3.5 h-3.5 shrink-0" aria-hidden />
          <span>
            倍率 ×{mult} で ¥100 未満になる {dropped} 脚は実投票どおり除去して計算
            {scaled.length === 0 && " — 全脚除去のためこのレースは投入されません"}
          </span>
        </p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Stat label={`投資総額 (×${mult})`} value={`¥${total.toLocaleString()}`} />
        <Stat
          label={`当たれば払戻 (×${mult})`}
          value={`¥${minPayout.toLocaleString()}〜¥${maxPayout.toLocaleString()}`}
        />
        <Stat
          label="最小 払戻/投資 (floor後)"
          value={
            <span className={ratioOk ? "text-(--color-good)" : "text-(--color-warn)"}>
              ×{ratio.toFixed(2)}
            </span>
          }
        />
      </div>
      {scaled.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm tnum table-zebra">
            <thead className="text-left text-(--color-muted) text-xs">
              <tr className="border-b border-(--color-line)">
                <th className="py-1.5 pr-3">買い目</th>
                <th className="py-1.5 pr-3 text-right">オッズ</th>
                <th className="py-1.5 pr-3 text-right">配分 (×{mult})</th>
                <th className="py-1.5 pr-3 text-right">的中時払戻 (×{mult})</th>
              </tr>
            </thead>
            <tbody>
              {scaled.map((l) => (
                <tr
                  key={l.key.join("-")}
                  className="border-b border-(--color-line)/60"
                >
                  <td className="py-1 pr-3 mono">{l.key.join("-")}</td>
                  <td className="py-1 pr-3 text-right">{l.odds.toFixed(1)}</td>
                  <td className="py-1 pr-3 text-right font-bold">
                    ¥{l.stake.toLocaleString()}
                  </td>
                  <td className="py-1 pr-3 text-right text-(--color-good)">
                    ¥{l.payout.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-2 text-xs text-(--color-muted)">
        実投票も watch-auto の <b>掛金倍率 — 3連単束 (×N)</b> で同じ ¥100 単位切り捨てが各脚 stake に
        適用されます (per-race 上限も倍率連動)。切り捨てで脚間比率が僅かに動くため、最小 払戻/投資 が
        margin (×{margin}) を下回ると <b className="text-(--color-warn)">トリガミ</b>になり得ます (色で警告)。
      </p>
    </div>
  );
}
