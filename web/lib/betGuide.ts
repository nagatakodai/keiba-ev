// Claude 指数の「自信度」(1位の抜け具合) から **買い方タイプ** を判定する参考ガイド。
//
// 出典: scripts/strategy_by_confidence.py の分析 (市場非依存 ~72R)。所見は方向性が一貫:
//   #1 が抜けている (1-2位差が大) ほど **本命系** (単勝#1 / 複勝#1 / 3連複・BOX) の回収率が上がり、
//   上位が拮抗する (差が小) ほど **組合せ系** (馬連 / 馬単 / ワイド) の回収率が上がる傾向。
// ただし標本が小さく **統計的に有意ではない** (Δ の 95%CI が 0 を跨ぐものが大半)。あくまで参考。

import type { MarketAgreement } from "@/lib/api";

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

// ── 市場との一致/不一致ガイド (Claude#1 == 市場1番人気 か) ─────────────────
// 蓄積シグナル (api/store.compute_market_agreement) は「一致(consensus)か否か」で券種 ROI を
// 分割して追っている。このレースがどちら側かを判定し、蓄積データ上その状態で相対的に伸びている
// 券種を参考提示する (Δの95%CIが0を跨がなくなれば★確証、それまでは蓄積中)。
export type MarketFavored = {
  label: string;
  roi: number; // この状態 (一致 or 不一致) 側の ROI
  otherRoi: number; // 反対側の ROI
  delta: number; // |一致ROI − 不一致ROI| (この状態が有利な絶対差)
  significant: boolean; // CI が 0 を跨がない (確証)
};

export type MarketAgreementGuide = {
  agree: boolean; // Claude 本命 == 市場1番人気 か
  claudeTop: { number: number; name: string };
  marketTop: { number: number; name: string };
  stateLabel: string; // 市場一致 / 市場不一致
  reason: string;
  favored: MarketFavored[]; // この状態で相対的に伸びる券種 (Δ降順)
  races: number; // 蓄積レース数
  sampleWarning: boolean;
};

type IdxItem = {
  number: number;
  name: string;
  claude_index: number | null;
  market_index: number | null;
};

/**
 * このレースが「Claude 本命 == 市場1番人気」(一致) か否かを判定し、蓄積された市場一致シグナルから
 * その状態で相対的に伸びる券種を付ける。Claude 指数が1頭も無い / 市場指数が2頭未満なら null。
 */
export function marketAgreementGuide(
  items: IdxItem[],
  agreement?: MarketAgreement | null,
): MarketAgreementGuide | null {
  const withClaude = items.filter(
    (i): i is IdxItem & { claude_index: number } => typeof i.claude_index === "number",
  );
  const withMarket = items.filter(
    (i): i is IdxItem & { market_index: number } => typeof i.market_index === "number",
  );
  if (withClaude.length < 1 || withMarket.length < 2) return null;
  const claudeTop = withClaude.reduce((a, b) => (b.claude_index > a.claude_index ? b : a));
  const marketTop = withMarket.reduce((a, b) => (b.market_index > a.market_index ? b : a));
  const agree = claudeTop.number === marketTop.number;

  const favored: MarketFavored[] = [];
  for (const m of agreement?.metrics ?? []) {
    if (m.agree_legs <= 0 || m.disagree_legs <= 0) continue; // 片側しか無い券種は判定不能
    // delta = agree_roi − disagree_roi。この状態が有利 = (一致 && delta>0) || (不一致 && delta<0)。
    const favorsThis = agree ? m.delta > 0 : m.delta < 0;
    if (!favorsThis) continue;
    favored.push({
      label: m.label,
      roi: agree ? m.agree_roi : m.disagree_roi,
      otherRoi: agree ? m.disagree_roi : m.agree_roi,
      delta: Math.abs(m.delta),
      significant: m.significant,
    });
  }
  favored.sort((a, b) => b.delta - a.delta);

  return {
    agree,
    claudeTop: { number: claudeTop.number, name: claudeTop.name },
    marketTop: { number: marketTop.number, name: marketTop.name },
    stateLabel: agree ? "市場一致" : "市場不一致",
    reason: agree
      ? `Claude 本命 = 市場1番人気 (${claudeTop.number} ${claudeTop.name}) — コンセンサス`
      : `Claude 本命 ${claudeTop.number} ${claudeTop.name} ≠ 市場1番人気 ${marketTop.number} ${marketTop.name} — 逆張り (contrarian)`,
    favored,
    races: agreement?.races ?? 0,
    sampleWarning: agreement?.sample_warning ?? true,
  };
}
