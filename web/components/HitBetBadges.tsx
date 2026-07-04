import { type HitBetLabel } from "@/lib/api";

// ダッシュボード仮想購入 (Claude 指数上位N頭3連単BOX + 戦略くらべ) の的中券種バッジ列。
// 勝負レースカードと予測分析履歴カードで共用 (ユーザ指示 2026-07-04)。
// EV束/3連単束 (実弾) の的中とは無関係。hits が null (判定不能) / 空 (的中なし) なら何も出さない。
export function HitBetBadges({ hits }: { hits?: HitBetLabel[] | null }) {
  if (!hits || hits.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase mr-0.5">
        仮想的中
      </span>
      {hits.map((h) => (
        <span
          key={h.key}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[11px] tnum bg-emerald-500/10 border-emerald-500/30 text-emerald-200 font-bold"
          title={`ダッシュボード仮想購入の的中 (¥100/脚換算の払戻)`}
        >
          {h.label}
          {h.payout > 0 && (
            <span className="text-emerald-300/80 font-normal">
              ¥{h.payout.toLocaleString()}
            </span>
          )}
        </span>
      ))}
    </div>
  );
}
