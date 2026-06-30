// Claude 指数の補強根拠 (evidence) 方針バージョン (バックエンド src/llm.py:INDEX_VERSION と一致させる)。
//   v1 = 各馬の補強根拠 (evidence) を **3 件まで** に制限していた頃 (〜2026-06-27)
//   v2 = 補強根拠の **上限を撤廃** (無制限・あればあるだけ) した現行 (2026-06-28〜)
// ユーザ指示 (2026-06-30): 「補強根拠が3件だったのをv1 / 無制限を v2 として表示。左上タイトルの横にも」。
export const INDEX_VERSION = "v2";

// 計測表示の対象バージョン (新しい/現行が先頭)。β (市場由来) は対象が少ないため表示しない
// (ユーザ指示 2026-06-30)。β race は index_version_of で β に分類され v1/v2 から除外される。
export const INDEX_VERSIONS = ["v2", "v1"] as const;

export const INDEX_VERSION_DESC: Record<string, string> = {
  v1: "補強根拠 3件まで",
  v2: "補強根拠 無制限",
  // β = score プロンプトに単勝オッズ列があり Claude 指数が市場由来だった頃 (〜2026-06-21 19:04)。
  "β": "市場由来 (旧・実験)",
};

export function indexVersionTitle(v: string | null | undefined): string {
  if (!v) return "Claude 指数なし";
  return `Claude 指数 ${v} — ${INDEX_VERSION_DESC[v] ?? ""}`.trim();
}
