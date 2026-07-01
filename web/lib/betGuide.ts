// Claude 指数の「自信度」(1位の抜け具合) から **買い方タイプ** を判定する参考ガイド。
//
// 出典: scripts/strategy_by_confidence.py の分析 (市場非依存 ~72R)。所見は方向性が一貫:
//   #1 が抜けている (1-2位差が大) ほど **本命系** (単勝#1 / 複勝#1 / 3連複・BOX) の回収率が上がり、
//   上位が拮抗する (差が小) ほど **組合せ系** (馬連 / 馬単 / ワイド) の回収率が上がる傾向。
// ただし標本が小さく **統計的に有意ではない** (Δ の 95%CI が 0 を跨ぐものが大半)。あくまで参考。

import type { MarketAgreement } from "@/lib/api";
import type { BadgeTone } from "@/components/ui";

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

// ── 頭数ガイド (出走頭数 → 効きやすい券種) ─────────────────────────────────
// 出典: 市場非依存 73R を出走頭数でバケットした券種別 ROI (2×2 分析の頭数版):
//   ≤7頭  単勝#1 180% / 複勝#1 130% (本命が素直・標本2Rで極小)
//   8-9頭 馬連#1-2 101% / 組合せ系 90% (組合せ優勢・本命も水準56%)
//   10-11頭 3連複BOX 79% / 単勝#1 108% (BOXの稼ぎどき・#1が抜けていれば的中集中)
//   12+頭 馬連#1-2 131% / 組合せ系 117% / **3連複BOX 的中 0/21・本命系 9%** (BOX/本命は壊滅)
// いずれも n 小 (2〜34R) で参考。12+頭の「BOX 買うな」(0/21) が最も確度が高い。
export type FieldSizeGuide = {
  n: number;
  bucketLabel: string;
  recommend: string;
  note: string;
  tone: BadgeTone;
};

export function fieldSizeGuide(n: number | null | undefined): FieldSizeGuide | null {
  if (!n || n <= 0) return null;
  if (n >= 12)
    return {
      n,
      bucketLabel: "多頭数 (12頭+)",
      recommend: "馬連#1-2 ・ 組合せ系 (ワイド/馬単)",
      note: "多頭数は3連複BOX・本命系が壊滅 (BOX 的中0/21・本命系9%)。組合せ系が伸びる (馬連131%)",
      tone: "magenta",
    };
  if (n >= 10)
    return {
      n,
      bucketLabel: "中頭数 (10-11頭)",
      recommend: "3連複BOX (指数1-2-3-4) ・ 単勝#1",
      note: "3連複BOXの稼ぎどき (79%) + 単勝#1 (108%)。#1が抜けていれば的中集中",
      tone: "good",
    };
  if (n >= 8)
    return {
      n,
      bucketLabel: "中頭数 (8-9頭)",
      recommend: "馬連#1-2 ・ 組合せ系 (本命も可)",
      note: "組合せ系/馬連が優勢 (101%/90%)。本命系も水準 (56%)",
      tone: "info",
    };
  return {
    n,
    bucketLabel: "少頭数 (≤7頭)",
    recommend: "単勝#1 ・ 複勝#1 (本命が素直)",
    note: "少頭数は本命が来やすい (単勝180%/複勝130%・ただし標本2Rで極小)",
    tone: "warn",
  };
}

// ── 総合ガイド (#1抜け × 市場一致 × 頭数 → 1行の推奨券種 or 見送り) ────────────
// 出典: 市場非依存 73R の 2×2 (自信度 × 市場一致) 同時 ROI + 頭数ゲート。要点:
//   #1抜け × 不一致 (Claude自信×逆張り) = 最強セル (3連複BOX 126% / 単勝#1 98%)
//   拮抗 × 一致 = 組合せ系 (馬連 153-264%)
//   #1抜け × 一致 = 衝突 (本命系vs組合せ)。BOXは一致時18%で不振 → 単勝#1/馬連で折衷
//   拮抗 × 不一致 = 死にセル (全券種<55%) → 見送り
//   頭数ゲート: 12頭+ は BOX 的中0/21 なので 3連複BOX を出さず馬連/組合せへ降格
// いずれも n 小 (12〜22R/セル) の傾向であり確証ではない (★確証は 3連複BOX×不一致 のみ)。
export type CombinedGuide = {
  state: "sweet" | "combo" | "conflict" | "skip";
  stateLabel: string;
  headline: string; // 本線券種 or 見送り
  reason: string;
  signals: string; // 判定に使った3信号
  tone: BadgeTone;
};

export function combinedGuide(
  items: IdxItem[],
  nRunners: number | null | undefined,
): CombinedGuide | null {
  const style = betStyleGuide(items.map((i) => i.claude_index));
  const mkt = marketAgreementGuide(items);
  if (!style || !mkt) return null;
  const dom = style.kind === "dominant";
  const agree = mkt.agree;
  const n = nRunners && nRunners > 0 ? nRunners : null;
  const bigField = n != null && n >= 12;
  const signals = `${dom ? "#1抜け" : "上位拮抗"} Δ${style.gap12.toFixed(0)}pt · ${
    agree ? "市場一致" : "市場不一致"
  }${n ? ` · ${n}頭` : ""}`;

  if (dom && !agree) {
    // 最強セル (Claude自信 × 市場逆張り)。BOX は頭数ゲート (12頭+は的中0/21)。
    return bigField
      ? {
          state: "combo",
          stateLabel: "狙い目 (多頭数)",
          headline: "馬連#1-2 ・ 単勝#1",
          reason: "Claude自信×市場逆張りだが多頭数のため3連複BOXは回避 (12頭+ BOX 的中0/21)",
          signals,
          tone: "good",
        }
      : {
          state: "sweet",
          stateLabel: "狙い目",
          headline: "3連複BOX (1-2-3-4) ・ 単勝#1",
          reason: "Claude自信 (#1抜け) × 市場逆張り = 最も回収が伸びるセル (BOX126%/単勝98%)",
          signals,
          tone: "good",
        };
  }
  if (!dom && agree) {
    return {
      state: "combo",
      stateLabel: "組合せ",
      headline: "馬連#1-2 ・ ワイド#1-2",
      reason: "上位拮抗 × 市場一致 = 組合せ系が伸びる (馬連153-264%)",
      signals,
      tone: "info",
    };
  }
  if (dom && agree) {
    return {
      state: "conflict",
      stateLabel: "折衷 (ガイド衝突)",
      headline: "単勝#1 or 馬連#1-2 (3連複BOXは不可)",
      reason: "#1抜け(本命寄り)と市場一致(組合せ寄り)が衝突。BOXは一致時18%で不振 → 単勝#1/馬連で折衷",
      signals,
      tone: "warn",
    };
  }
  // !dom && !agree
  return {
    state: "skip",
    stateLabel: "見送り推奨",
    headline: "無理に賭けない",
    reason: "上位拮抗 × 市場逆張り = 全券種が不振の死にセル (全て<55%)",
    signals,
    tone: "bad",
  };
}
