// Claude 指数の方針バージョン (バックエンド src/llm.py:INDEX_VERSION と一致させる)。
//   v1 = 各馬の補強根拠 (evidence) を **3 件まで** に制限していた頃 (〜2026-06-27)
//   v2 = 補強根拠の **上限を撤廃** (無制限・あればあるだけ) (2026-06-28〜)
//   v3 = **仮指数アンカー方式** — 市場非依存の仮指数を anchor に Claude が±調整 (2026-07-01 15:13〜・現行)
// ユーザ指示 (2026-06-30): 「方針バージョン毎に計測を分離して表示。左上タイトルの横にも」。
export const INDEX_VERSION = "v3";

// 計測表示の対象バージョン (新しい/現行が先頭)。β (市場由来) は対象が少ないため表示しない
// (ユーザ指示 2026-06-30)。β race は index_version_of で β に分類され v1/v2/v3 から除外される。
export const INDEX_VERSIONS = ["v3", "v2", "v1"] as const;

export const INDEX_VERSION_DESC: Record<string, string> = {
  v1: "補強根拠 3件まで",
  v2: "補強根拠 無制限",
  v3: "仮指数アンカー±調整",
  // β = score プロンプトに単勝オッズ列があり Claude 指数が市場由来だった頃 (〜2026-06-21 19:04)。
  "β": "市場由来 (旧・実験)",
};

export function indexVersionTitle(v: string | null | undefined): string {
  if (!v) return "Claude 指数なし";
  return `Claude 指数 ${v} — ${INDEX_VERSION_DESC[v] ?? ""}`.trim();
}
