// keiba-ev FastAPI クライアント。
//
// 経路:
//  - Client (browser): 同 origin (`""`) で `/api/*` を叩く →
//    Next の Route Handler (app/api/[...path]/route.ts) が
//    server-side で X-API-Key を付与して Cloud Run に転送する。
//  - Server (RSC / Server Action / Route Handler): API_BASE を直接叩き、
//    その場で X-API-Key を付ける。Route Handler を経由しない (余計なホップを避ける)。
//
// 共有キー (API_SHARED_KEY) は絶対にクライアントバンドルに含めない。
// NEXT_PUBLIC_ プレフィックスを付けず、サーバ側でのみ process.env から読む。

const IS_SERVER = typeof window === "undefined";

// keirin ev-api は 8787。本 keiba-ev は完全にずらして 9788 をデフォルトにする
// (env 未設定で keirin に流れる事故を防ぐ — 2026-05-21 まで起きていた)。
export const API_BASE = IS_SERVER
  ? process.env.API_BASE ?? "http://localhost:9788"
  : "";

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = {
    "content-type": "application/json",
    ...((init?.headers ?? {}) as Record<string, string>),
  };
  if (IS_SERVER) {
    const key = process.env.API_SHARED_KEY;
    if (key) headers["x-api-key"] = key;
  }
  const res = await fetch(url, { ...init, headers, cache: "no-store" });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} for ${path}: ${text}`);
  }
  return (await res.json()) as T;
}

// --- Types ---

export type PredictionSummary = {
  race_id: string;
  saved_at: string;
  venue_name: string;
  race_class: string;
  schedule_index: number;
  race_number: number;
  odds_updated_at: number;
  // 締切 (betting close) / 発走 (race start) の unix 秒。古い snapshot は null。
  close_at: number | null;
  start_at: number | null;
  row_count: number;
  plan_a_count: number;
  plan_b_count: number;
  plan_c_count: number;
  // Plan G (適性ゲート → EV 足切り, Phase 20) の point 数。API が exposing する
  // ようになったのは 2026-05-24 以降、それ以前の snapshot は欠落することがある。
  plan_g_count?: number;
  // 当て枠 Plan H1 / H2 (backend 2026-05-20 以降。古いスナップショットには欠落する)
  plan_h1_count?: number;
  plan_h2_count?: number;
  // 最終買い目 Plan F = A/B/C/G/H1/H2 の union (backend 2026-05-21 以降)
  plan_f_count?: number;
  // 適性指数 top 3 (total 降順)。snapshot に horse_aptitude が無いと空配列。
  top_aptitude?: Array<{ number: number; name: string; total: number }>;
  has_evidence: boolean;
  has_result: boolean;
};

export type PredictionRow = {
  key: [number, number, number];
  odds: number;
  popularity: number;
  prob: number;
  px_o: number;
  tier: "honsen" | "chuana" | "oana" | "minus" | string;
};

// 市場乖離 (単勝オッズと複勝オッズの implied prob 比率) per-horse。
// interpretation で「3着型」「1着型」「標準」「極端」「不明」を判定。
export type MarketSignal = {
  number: number;
  name: string;
  win_odds: number;
  place_odds_min: number;
  win_implied: number;
  place_implied: number;
  place_to_win_ratio: number;
  interpretation: "3着型" | "1着型" | "標準" | "極端" | "不明" | string;
};

// 持ち時計 (venue × distance × surface で過去走った own_time_sec の最速値)。
export type HorseBestTime = {
  number: number;
  name: string;
  best_time_sec: number;
  runs: number;
};

// 各馬の適性指数 (0-100 / 同レース内相対) + 因子内訳 + 主要根拠。
// total は重み付け平均。snapshot は total 降順で配列化されている。
export type HorseAptitude = {
  number: number;
  name: string;
  total: number;
  ability: number;
  distance_fit: number;
  last3f: number;
  surface_fit: number;       // 同 surface/venue 経験 (コース適性)
  going_fit?: number;        // 同馬場状態 (良/稍/重/不) での好走率
  condition: number;
  jockey_fit: number;
  pace_fit: number;
  graded_record: number;
  graded_text: string;
  reasons: string[];
};

// 馬連 / ワイド / 馬単 / 3連複 の EV row (3連単とは別の bet type)。
// key 長は bet_type による (2 = 馬連/ワイド/馬単, 3 = 3連複)。
export type BetEvRow = {
  key: number[];
  odds: number;
  popularity: number;
  prob: number;
  px_o: number;
  tier: string;
};

export type PredictionDetail = {
  race_id: string;
  saved_at: string;
  venue_name: string;
  race_class: string;
  schedule_index: number;
  race_number: number;
  odds_updated_at: number;
  close_at: number | null;
  start_at: number | null;
  rows: PredictionRow[];
  horse_aptitude?: HorseAptitude[];
  // 市場乖離 (単勝 vs 複勝 implied prob 比率)。fetch されていなければ空配列。
  market_signals?: MarketSignal[];
  // 持ち時計 (同 venue × 同距離 × 同 surface での best own_time_sec)。速い順。
  horse_best_times?: HorseBestTime[];
  // どの確率モデルが使われたか (lgbm = LightGBM 学習済 / linear-fallback = 線形 softmax)
  model_info?: {
    available?: boolean;
    n_features?: number;
    trained_at?: string | null;
    engine: "lgbm" | "linear-fallback" | "unknown" | string;
  };
  // 馬連 (quinella) / ワイド (wide) / 馬単 (exacta) / 3連複 (trio) の EV table。
  // 各 bet type の top 30 行 (P×O 降順)。fetch されていない bet type はキー無し。
  bet_tables?: Record<string, BetEvRow[]>;
  // bet_tables の「適性ゲート → EV 足切り」picks (Plan G の bet type 版)。
  bet_tables_g?: Record<string, BetEvRow[]>;
  // 適性総合 top N 頭の馬番リスト (Plan G が依拠する集合)。
  aptitude_top_horses?: number[];
  plan_a_keys: number[][];
  plan_b_keys: number[][];
  plan_c_keys: number[][];
  // Plan G: 適性 top N 頭の集合 → P×O≥1.02 で足切り (3連単)
  plan_g_keys?: number[][];
  plan_h1_keys?: number[][];
  plan_h2_keys?: number[][];
  // 最終買い目 Plan F: A/B/C/G/H1/H2 を union dedup・EV 降順
  plan_f_keys?: number[][];
  evidence?: { evidence_by_key?: Record<string, { count: number; reasons?: string[] }>; cuts?: string[]; final_plan?: unknown };
  evidence_rows?: PredictionRow[];
  evidence_plan_a_keys?: number[][];
  evidence_plan_b_keys?: number[][];
  evidence_plan_c_keys?: number[][];
  evidence_plan_g_keys?: number[][];
  evidence_plan_h1_keys?: number[][];
  evidence_plan_h2_keys?: number[][];
  evidence_plan_f_keys?: number[][];
  result?: { finish_order: number[]; trifecta_payout?: number; note?: string };
};

export type JobInfo = {
  id: string;
  label: string;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  return_code: number | null;
  started_at: number | null;
  finished_at: number | null;
  line_count: number;
};

export type WatchAutoStatus = {
  running: boolean;
  config: {
    window?: number;
    tolerance?: number;
    interval_sec?: number;
    ev_max?: number | null;
    min_prob?: number | null;
    market_blend?: number | null;
    aptitude_top?: number | null;
    with_exacta?: boolean;
    with_trio?: boolean;
    // race detection を行う JST 時間帯 (HH:MM-HH:MM)。
    // backend (api/main.py:WatchAutoStartRequest) の default は "09:00-23:45"。
    active_hours?: string;
  };
  job: JobInfo | null;
};

export type PendingItem = {
  race_id: string;
  url?: string;
  status: "pending" | "success" | "failed";
  attempts: number;
  max_attempts: number;
  retry_interval_sec: number;
  due_at: number;
  next_attempt_at: number;
  scheduled_at: number;
  seconds_until_next: number;
  last_error?: string;
};

export type PendingSummary = {
  total: number;
  pending: number;
  success: number;
  failed: number;
};

export type RecordResponse = {
  saved: boolean;
  race_id: string;
  finish_order: number[];
  trifecta_payout: number;
  matched: boolean;
};

export type WatchAutoHistoryItem = {
  started_at: number;
  finished_at: number;
  // 内部 race_id ("cup_id-schedule_index-race_number") — predictions / results
  // と join するキー。/predictions/{race_id} へのリンクはこちらを使う。
  race_id: string;
  // netkeiba 内部 race_id (YYYYMMDDPP00RR)。デバッグ・トレース用途のみ。prediction との join 不可。
  netkeiba_race_id?: string;
  /** @deprecated 旧フィールド名。新しい API では `netkeiba_race_id` を返す。 */
  winticket_race_id?: string;
  url: string;
  venue: string;
  race_no: number;
  close_at: number;
  // 発走 unix 秒。古い履歴には欠落する可能性あり。
  start_at: number | null;
  rc: number;
};

export type CalibrationTier = {
  tier: string;
  rows: number;
  prob_sum: number;
  hits: number;
  ratio: number;
};

export type CalibrationPlan = {
  // "Plan A" | "Plan B" | "Plan C" | "Plan H1" | "Plan H2" (順序固定で 5 種返ってくる前提)
  plan: string;
  // 全 calibration 対象レース数 (race_count と同じ)。買い目 0 点のレースも分母に入る。
  races: number;
  // この Plan が買い目を出した (key_list 長 ≥ 1) レース数。hit_rate の分母として推奨。
  participated_races: number;
  hits: number;
  // hits / participated_races (旧 backend は hits / races だったが新 backend で変更済)
  hit_rate: number;
  // Wilson 95% 信頼区間 (二項分布)。sample が増えるほど狭くなる。
  hit_rate_ci_low?: number;
  hit_rate_ci_high?: number;
  total_points: number;
  // リクエスト送信時の point_cost (default 100)。共通単価。
  point_cost: number;
  // 各 Plan の想定枠 (¥)。A/B/C=8000、H1/H2=2000。フロントで per-point cost を出すならこれを使う。
  assumed_budget_slot: number;
  stake: number;
  payout: number;
  roi: number;
  // ROI bootstrap 95% 信頼区間 (n_iter=1000, seed=42 固定なのでリロードしても揺れない)。
  roi_ci_low?: number;
  roi_ci_high?: number;
};

export type CalibrationRaceItem = {
  race_id: string;
  venue: string;
  finish: number[];
  winning_tier: string | null;
  payout: number;
  plan_a_hit: boolean;
  plan_b_hit: boolean;
  plan_c_hit: boolean;
  // Plan G (適性ゲート→EV足切り) の的中フラグ (backend 2026-05-22 以降)
  plan_g_hit?: boolean;
  // 当て枠の的中フラグ (backend 2026-05-20 以降)。古いレースには欠落する可能性。
  plan_h1_hit?: boolean;
  plan_h2_hit?: boolean;
  // 最終買い目 Plan F の的中フラグ (backend 2026-05-21 以降)
  plan_f_hit?: boolean;
};

export type CalibrationReport = {
  race_count: number;
  point_cost: number;
  // 集計に含めた results の recorded_at 最大値 (ISO8601)。データ無しなら null。
  last_updated_at: string | null;
  // race_count < 30 のとき true。フロントで「参考値」バッジを出す目印。
  sample_warning?: boolean;
  tiers: CalibrationTier[];
  plans: CalibrationPlan[];
  races: CalibrationRaceItem[];
};

// --- Endpoints ---

export const api = {
  listPredictions: (limit = 100) =>
    jsonFetch<{ items: PredictionSummary[] }>(`/api/predictions?limit=${limit}`),
  getPrediction: (raceId: string) =>
    jsonFetch<PredictionDetail>(`/api/predictions/${encodeURIComponent(raceId)}`),
  calibrate: (pointCost = 100) =>
    jsonFetch<CalibrationReport>(`/api/calibrate?point_cost=${pointCost}`),

  analyze: (body: {
    url: string;
    refresh?: boolean;
    no_llm?: boolean;
    llm_model?: string;
    ev_max?: number | null;
    min_prob?: number | null;
    market_blend?: number | null;
    aptitude_top?: number | null;
    with_exacta?: boolean;
    with_trio?: boolean;
  }) =>
    jsonFetch<JobInfo>(`/api/analyze`, { method: "POST", body: JSON.stringify(body) }),

  getJob: (id: string) =>
    jsonFetch<JobInfo & { lines: Array<{ seq: number; ts: number; stream: string; text: string }> }>(
      `/api/jobs/${id}`,
    ),
  cancelJob: (id: string) => jsonFetch<JobInfo>(`/api/jobs/${id}/cancel`, { method: "POST" }),

  startWatch: (body: {
    window?: number;
    tolerance?: number;
    interval_sec?: number;
    ev_max?: number | null;
    min_prob?: number | null;
    market_blend?: number | null;
    aptitude_top?: number | null;
    with_exacta?: boolean;
    with_trio?: boolean;
    active_hours?: string;
  }) =>
    jsonFetch<{ running: boolean; config: WatchAutoStatus["config"]; job: JobInfo }>(
      `/api/watch-auto/start`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  stopWatch: () =>
    jsonFetch<{ running: boolean; config: WatchAutoStatus["config"] }>(
      `/api/watch-auto/stop`,
      { method: "POST" },
    ),
  watchStatus: () => jsonFetch<WatchAutoStatus>(`/api/watch-auto/status`),
  watchHistory: (limit = 200) =>
    jsonFetch<{ items: WatchAutoHistoryItem[] }>(`/api/watch-auto/history?limit=${limit}`),

  listPending: () =>
    jsonFetch<{ items: PendingItem[]; summary: PendingSummary }>(`/api/pending`),
  // pending エントリを手動削除。failed の old WinTicket-id を 24h auto-prune を待たずに掃除する用途。
  deletePending: (raceId: string) =>
    jsonFetch<{ removed: number; race_id: string; total: number }>(
      `/api/pending/${encodeURIComponent(raceId)}`,
      { method: "DELETE" },
    ),
  recordResult: (body: {
    race_id: string;
    finish_order: number[];
    trifecta_payout?: number;
    note?: string;
  }) =>
    jsonFetch<RecordResponse>(`/api/record`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
