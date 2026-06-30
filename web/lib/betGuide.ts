// Claude 指数の「自信度」(1位の抜け具合) から **買い方タイプ** を判定する参考ガイド。
//
// 出典: scripts/strategy_by_confidence.py の分析 (市場非依存 ~72R)。所見は方向性が一貫:
//   #1 が抜けている (1-2位差が大) ほど **本命系** (単勝#1 / 複勝#1 / 3連複・BOX) の回収率が上がり、
//   上位が拮抗する (差が小) ほど **組合せ系** (馬連 / 馬単 / ワイド) の回収率が上がる傾向。
// ただし標本が小さく **統計的に有意ではない** (Δ の 95%CI が 0 を跨ぐものが大半)。あくまで参考。

// この pt 以上の「1位−2位 指数差」で本命型とみなす (分析の median=8 を閾値に採用)。
export const DOMINANT_GAP12 = 8;

export type BetStyleGuide = {
  kind: "dominant" | "close";
  top1: number;
  top2: number;
  gap12: number;
  styleLabel: string; // 本命型 / 拮抗型
  reason: string; // #1が抜けている / 上位が拮抗
  recommend: string; // 寄せるべき券種
};

/** Claude 指数 (降順でなくてもよい) の配列から買い方ガイドを返す。2頭未満や指数なしは null。 */
export function betStyleGuide(
  claudeIndices: Array<number | null | undefined>,
): BetStyleGuide | null {
  const vals = claudeIndices
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v))
    .sort((a, b) => b - a);
  if (vals.length < 2) return null;
  const top1 = vals[0];
  const top2 = vals[1];
  const gap12 = top1 - top2;
  const dominant = gap12 >= DOMINANT_GAP12;
  return {
    kind: dominant ? "dominant" : "close",
    top1,
    top2,
    gap12,
    styleLabel: dominant ? "本命型" : "拮抗型",
    reason: dominant ? "#1 が抜けています" : "上位が拮抗しています",
    recommend: dominant
      ? "単勝#1 ・ 複勝#1 ・ 3連複BOX (指数1-2-3-4)"
      : "馬連#1-2 ・ 馬単#1→2 ・ ワイド#1-2",
  };
}
