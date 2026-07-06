// Claude 指数の「自信度」(1位の抜け具合) から **買い方タイプ** を判定する参考ガイド。
//
// 出典: scripts/strategy_by_confidence.py の分析 (市場非依存 ~72R)。所見は方向性が一貫:
//   #1 が抜けている (1-2位差が大) ほど **本命系** (単勝#1 / 複勝#1 / 3連複・BOX) の回収率が上がり、
//   上位が拮抗する (差が小) ほど **組合せ系** (馬連 / 馬単 / ワイド) の回収率が上がる傾向。
// ただし標本が小さく **統計的に有意ではない** (Δ の 95%CI が 0 を跨ぐものが大半)。あくまで参考。

import type { BetEvRow, MarketAgreement, SignalRule, SignalRules } from "@/lib/api";
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

// ── プレレジ済シグナルルール (per-race 発火判定, 2026-07-05) ─────────────────
// api/store.py SIGNAL_RULES と同じ条件判定 (consensus / style / venue / 頭数 / 死にセル) を
// このレースに適用し、発火中のルールを返す。「確証★」ルールだけが行動根拠になり、蓄積中は
// 参考表示。破綻ルールは出さない。条件定数は backend (_MARKET_INDEX_T /
// _FAVORITE_RATIO_THRESHOLD) のミラー — 変える時は両方変える。
const MARKET_INDEX_T = 1.5;
const FAVORITE_RATIO_THRESHOLD = 2.0;
// JRA 10 場 (それ以外の venue_name は NAR/ばんえい)。
const JRA_VENUES = new Set([
  "札幌",
  "函館",
  "福島",
  "新潟",
  "東京",
  "中山",
  "中京",
  "京都",
  "阪神",
  "小倉",
]);

export type FiredSignalRule = {
  rule: SignalRule;
  prospectiveRoi: number;
  prospectiveRaces: number;
  status: SignalRule["status"];
};

// backend `_race_features` (api/store.py) のミラー: Claude 指数上位3頭のギャップ + 市場の
// 荒れ具合 + FL バイアス (単勝/複勝オッズ比, betTables から) の発走前特徴量。同点タイブレークは
// (-指数, 馬番昇順)。計算不能な特徴量は null (= その特徴量を要求するルールは発火しない)。
// 変える時は backend と両方変える。
export function raceSignalFeatures(
  items: IdxItem[],
  betTables?: Record<string, BetEvRow[]> | null,
): Record<string, number | null> {
  const withClaude = items
    .filter((i): i is IdxItem & { claude_index: number } => typeof i.claude_index === "number")
    .slice()
    .sort((a, b) => b.claude_index - a.claude_index || a.number - b.number);
  const withMarket = items
    .filter((i): i is IdxItem & { market_index: number } => typeof i.market_index === "number")
    .slice()
    .sort((a, b) => b.market_index - a.market_index || a.number - b.number);
  const gap12 = withClaude.length >= 2 ? withClaude[0].claude_index - withClaude[1].claude_index : null;
  const gap23 = withClaude.length >= 3 ? withClaude[1].claude_index - withClaude[2].claude_index : null;
  const gap34 = withClaude.length >= 4 ? withClaude[2].claude_index - withClaude[3].claude_index : null;
  // Claude 指数 1/2/3位の生値 (backend _race_features の idx1/2/3 ミラー, 2026-07-06)
  const idx1 = withClaude.length >= 1 ? withClaude[0].claude_index : null;
  const idx2 = withClaude.length >= 2 ? withClaude[1].claude_index : null;
  const idx3 = withClaude.length >= 3 ? withClaude[2].claude_index : null;
  const marketRank = new Map(withMarket.map((i, k) => [i.number, k + 1]));
  const top3 = withClaude.slice(0, 3);
  let top3RankGap: number | null = null;
  let top3IdxDiff: number | null = null;
  if (top3.length === 3 && top3.every((h) => marketRank.has(h.number))) {
    top3RankGap = top3.reduce((s, h, k) => s + (marketRank.get(h.number)! - (k + 1)), 0);
    const byNum = new Map(withMarket.map((i) => [i.number, i.market_index]));
    top3IdxDiff = top3.reduce((s, h) => s + (h.claude_index - byNum.get(h.number)!), 0) / 3;
  }
  const favOdds =
    withMarket.length >= 1 && withMarket[0].market_index > 0
      ? Math.pow(100 / withMarket[0].market_index, MARKET_INDEX_T)
      : null;
  const probs = withMarket
    .filter((i) => i.market_index > 0)
    .map((i) => Math.pow(i.market_index / 100, MARKET_INDEX_T));
  const total = probs.reduce((a, b) => a + b, 0);
  const top3Conc = probs.length >= 3 && total > 0 ? (probs[0] + probs[1] + probs[2]) / total : null;
  // FL バイアス: Claude 上位3頭の 単勝/複勝 オッズ比 (bet_tables の実オッズ, 2026-07-06)。
  const pw: Record<string, number | null> = { pw_top1: null, pw_top2: null, pw_top3: null };
  if (betTables) {
    const oddsMap = (rows?: BetEvRow[]) => {
      const m = new Map<number, number>();
      for (const r of rows ?? []) if (r.key.length === 1 && r.odds > 0) m.set(r.key[0], r.odds);
      return m;
    };
    const winO = oddsMap(betTables.win);
    const plcO = oddsMap(betTables.place);
    withClaude.slice(0, 3).forEach((h, i) => {
      const w = winO.get(h.number);
      const p = plcO.get(h.number);
      if (w && p) pw[`pw_top${i + 1}`] = w / p;
    });
  }
  return {
    gap12,
    gap23,
    gap34,
    idx1,
    idx2,
    idx3,
    top3_rank_gap: top3RankGap,
    top3_idx_diff: top3IdxDiff,
    fav_odds: favOdds,
    top3_conc: top3Conc,
    ...pw,
  };
}
export type RaceSignalGuide = {
  fired: FiredSignalRule[]; // このレースで条件成立中のプレレジルール (破綻除く)
  deadCell: boolean; // 拮抗型 × 市場不一致 = 見送りゾーン
  agree: boolean;
  competitive: boolean; // 拮抗型か (市場 top2 implied 勝率比 < 2.0)
  jra: boolean | null; // venue_name から判定 (不明は null)
};

/** このレースに発火するプレレジ済シグナルルールを判定する。指数不足なら null。 */
export function raceSignalRuleGuide(
  items: IdxItem[],
  nRunners: number | null | undefined,
  venueName: string | null | undefined,
  signalRules?: SignalRules | null,
  betTables?: Record<string, BetEvRow[]> | null,
): RaceSignalGuide | null {
  const withClaude = items.filter(
    (i): i is IdxItem & { claude_index: number } => typeof i.claude_index === "number",
  );
  const withMarket = items.filter(
    (i): i is IdxItem & { market_index: number } => typeof i.market_index === "number",
  );
  if (withClaude.length < 1 || withMarket.length < 2) return null;
  // 同点は馬番昇順の明示タイブレーク (backend _tagged_eval_races / market_ranked と同一規約。
  // 行順 reduce だと index_compare の並び=Claude 指数降順に依存し consensus が非決定になる)。
  const claudeTop = withClaude.reduce((a, b) =>
    b.claude_index > a.claude_index ||
    (b.claude_index === a.claude_index && b.number < a.number)
      ? b
      : a,
  );
  const marketTop = withMarket.reduce((a, b) =>
    b.market_index > a.market_index ||
    (b.market_index === a.market_index && b.number < a.number)
      ? b
      : a,
  );
  const agree = claudeTop.number === marketTop.number;
  const ms = withMarket.map((i) => i.market_index).sort((a, b) => b - a);
  const p1 = Math.pow(ms[0] / 100, MARKET_INDEX_T);
  const p2 = Math.pow(ms[1] / 100, MARKET_INDEX_T);
  const competitive = !(p2 > 0 && p1 / p2 >= FAVORITE_RATIO_THRESHOLD);
  const jra = venueName ? JRA_VENUES.has(venueName) : null;
  const deadCell = competitive && !agree;
  const features = raceSignalFeatures(items, betTables);

  const fired: FiredSignalRule[] = [];
  for (const rule of signalRules?.rules ?? []) {
    if (rule.status === "broken") continue; // 棄却済ルールは提示しない
    if (rule.consensus != null && agree !== rule.consensus) continue;
    if (rule.style != null && competitive !== rule.style) continue;
    if (rule.venue != null && (jra == null || jra !== rule.venue)) continue;
    if (rule.min_runners != null && (!nRunners || nRunners < rule.min_runners)) continue;
    if (rule.max_runners != null && (!nRunners || nRunners > rule.max_runners)) continue;
    if (rule.skip_dead_cell && deadCell) continue; // 死にセルでは規律ルール自体が見送り
    // 数値特徴量条件 (上位3頭ギャップ/荒れ具合)。backend `_rule_matches` と同じ: 計算不能は不発火。
    let featOk = true;
    for (const [name, cond] of Object.entries(rule.features ?? {})) {
      const v = features[name];
      if (v == null || (cond.min != null && v < cond.min) || (cond.max != null && v > cond.max)) {
        featOk = false;
        break;
      }
    }
    if (!featOk) continue;
    fired.push({
      rule,
      prospectiveRoi: rule.prospective.roi,
      prospectiveRaces: rule.prospective.races,
      status: rule.status,
    });
  }
  // 確証★ → 有望 → 蓄積中 の順 (行動根拠になるものを先頭に)。
  const rank: Record<string, number> = { confirmed: 0, promising: 1, accumulating: 2 };
  fired.sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9));
  return { fired, deadCell, agree, competitive, jra };
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
