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
  // 新スキーマ (2026-05-29 後半): Plan A/B も廃止。3連単 は他券種と並ぶ bet_tables[trifecta] に
  // 入り、表示は 2 つの bundle (recommended_bundle 回収優先 + recommended_bundle_hit 的中優先) に集約。
  // 旧 plan_*_count は backend が 0 を返すように、frontend からも参照しない。
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

// joint Kelly「まとめ買い」最適束の 1 脚。
export type BundleLeg = {
  bet_type: string;     // win/place/quinella/wide/exacta/trio/trifecta
  key: number[];
  odds: number;
  prob: number;
  px_o: number;
  tier: string;
  kelly: number;        // full-Kelly fraction f* (0-1)
  fraction: number;     // ¥丸め後の実効配分 (stake/bankroll)
  stake: number;        // ¥ (stake_unit 単位)
  payout_if_hit: number; // この脚が的中したときの払戻 (odds × stake)。≥ total_stake ならトリガミ無し
};

// 「買わなかった脚」(取り消し線で表示)。BundleLeg と同形 + reason。
//   reason="torigami" = トリガミ防止で除去 / "budget" = 予算を割れず配分0 (stake<min_stake)。
export type DroppedLeg = BundleLeg & { reason?: "torigami" | "budget" | string };

// 全 bet type 横断の joint (同時) Kelly 最適まとめ買い束。
// レースの完全な top-3 結果分布上で束全体の E[log(資金)] を最大化した配分。
export type RecommendedBundle = {
  objective: string;            // "joint_kelly"
  bankroll: number;             // ¥10,000
  kelly_fraction: number;       // 1.0 = full Kelly
  pxo_floor: number;
  legs: BundleLeg[];
  total_stake: number;          // ¥
  total_fraction: number;       // Σ fraction (資金に対する束全体の比率)
  bundle_hit_prob: number;      // P(1 脚以上当たる)
  expected_return: number;      // gross multiplier (モデル期待値, 楽観バイアス込み)
  expected_log_growth: number;  // E[log W]
  n_candidates: number;
  n_outcomes: number;
  // トリガミ防止: 全脚のうち最小の (payout_if_hit / total_stake)。≥ torigami_margin なら
  // 実オッズが ~(1−1/margin) 下振れしても収支マイナスにならない。dropped_torigami は除去脚数。
  min_payout_ratio?: number;
  dropped_torigami?: number;
  // 買わなかった脚 (= トリガミ防止 or 予算で除外)。frontend で取り消し線表示。古い snapshot は欠落。
  dropped_legs?: DroppedLeg[];
  torigami_margin?: number;     // 払戻/投資 の下限 (1.10 = 9% 下振れ緩衝)。古い snapshot は欠落

  // claude -p による web 調査検証 (取消/不安材料を裏取りして cut)。未検証なら欠落。
  llm_review?: {
    validated: boolean;
    cuts?: string[];
    notes?: Record<string, string>;
    summary?: string;
    confidence?: string;
  };
};

// Plan T「全力的中モード」束 (3連単のみ・市場無視・Claude 指数フォーメーション・トリガミ防止あり)。
// legs は RecommendedBundle と同形 (BundleLegsTable 流用可、bet_type は全て "trifecta")。
export type TrifectaHitmaxBundle = {
  objective: string;              // "trifecta_hitmax"
  bankroll: number;
  legs: BundleLeg[];
  total_stake: number;
  total_fraction: number;
  bundle_hit_prob: number;
  covered_prob: number;           // = bundle_hit_prob。理論的中率 (model 基準・過信禁物)
  expected_return: number;        // gross multiplier (-EV 想定の参考値)
  n_points: number;               // 実点数 (フォーメーション展開数 − トリガミ除去)
  n_formation: number;            // フォーメーション展開の総 triple 数 (除去前)
  n_candidates: number;           // odds が取れて買えた triple 数
  // Claude 指数フォーメーション構造
  rank_source?: string | null;    // "claude" | "model" (Claude 指数 or model fallback)
  // "claude" = 締切直前に Claude が3連単買い目を選定 (build_trifecta_from_keys)。
  // 無し/その他 = 機械フォーメーション (build_trifecta_hitmax)。
  selection_source?: string | null;
  llm_select?: { summary?: string; confidence?: string; n_keys?: number } | null;
  formation?: string | null;      // "1×4×7" (1着×2着×3着 の頭数)
  head_horses?: number[];         // 1着候補 (絞る)
  mid_horses?: number[];          // 2着候補 (中くらい)
  tail_horses?: number[];         // 3着候補 (広げる)
  head_n?: number;                // 1着列頭数 (1 or 2、指数の開きで可変)
  // トリガミ防止 (build_bundle と共通)
  min_payout_ratio?: number;      // 最小 払戻/投資 (≥ torigami_margin なら当たれば必ず投資総額以上)
  dropped_torigami?: number;      // トリガミで除去した脚数
  // 買わなかった脚 (= トリガミ防止 or 予算で除外)。frontend で取り消し線表示。古い snapshot は欠落。
  dropped_legs?: DroppedLeg[];
  torigami_margin?: number;
  odds_summary?: {
    min_payout: number;
    median_payout: number;
    max_payout: number;
    weighted_avg_odds: number;    // Σ p_i·O_i / Σ p_i (配当の伸び)
  } | null;
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
  // Claude 各馬指数 (score ステージ由来 0-100)。score 未実施/フォールバック時は null。
  llm_win_index?: Record<string, number> | null;
  // 市場指数 (単勝オッズ de-vig → Claude 指数と同じ対数勝率スケール 0-100)。
  market_win_index?: Record<string, number> | null;
  // Claude 指数の取得時刻 (score ステージ)。
  llm_scored_at?: string | null;
  // Claude 指数が無い (score 未完/未実施 = モデルのみ) フォールバックか。
  llm_fallback?: boolean;
  // Claude 強さ指数 (0-100) × 市場指数 を per-horse で併記 (差 = Claude − 市場、正 = Claude が強気)。
  // Claude 値降順 (無ければ市場指数降順)。support = 補強根拠件数 (検索で動かした裏付け)。
  index_compare?: Array<{
    number: number;
    name: string;
    claude_index: number | null;
    market_index: number | null;
    diff: number | null;
    support?: number | null;
    // 直前/軟情報フラグ (取消/馬体重増減/前走不利/厩舎勝負気配 等)。無ければ空配列。
    alerts?: string[] | null;
  }>;
  // 各馬の直前/軟情報フラグ (score ステージ由来)。記録/表示用 (確率には未使用)。
  llm_alerts?: Record<string, string[]> | null;
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
  // 新スキーマ (2026-05-29 後半): Plan A/B 自体は廃止。3連単 は bet_tables[trifecta] に入る。
  // 2 つの bundle のみ表示する:
  //   recommended_bundle      : 回収優先 (実弾で買う、joint Kelly EV 最適)
  //   recommended_bundle_hit  : 的中優先 (おまけ計測、prob 降順 pool で Kelly)
  recommended_bundle?: RecommendedBundle | null;
  // Plan T「全力的中モード」: 3連単のみ・市場無視・EV/トリガミ無しの model 的中確率 top-K 束。
  // recommended_bundle (EV駆動) と完全分離。covered_prob = 理論的中率 (model 基準・過信禁物)。
  recommended_bundle_t?: TrifectaHitmaxBundle | null;
  plan_t_keys?: number[][];
  plan_t_params?: {
    cover?: number; min_points?: number; max_points?: number;
    fixed_k?: number | null; stake_mode?: string; min_odds?: number | null; bankroll?: number;
  } | null;
  // 的中優先 EV table (per bet type, prob 降順, px_o>=1.0 で足切り)。
  evidence?: { evidence_by_key?: Record<string, { count: number; reasons?: string[] }>; cuts?: string[]; final_plan?: unknown };
  evidence_rows?: PredictionRow[];
  result?: {
    finish_order: number[];
    trifecta_payout?: number;
    note?: string;
    // 最終確定オッズ: `"<bet_type>:<key-with-->"` → odds。result fetch 時に保存。
    final_odds?: Record<string, number>;
  };
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
  // オッズパーク投票 daemon (headful ブラウザ) が稼働中か。
  bet_running?: boolean;
  // JRA 即PAT 投票 daemon (headful ブラウザ) が稼働中か。
  ipat_bet_running?: boolean;
  // 投票発火デーモン (bet_scheduler, 締切1分前に精密発火) が稼働中か。
  scheduler_running?: boolean;
  config: {
    // 2段パイプライン: BET 帯 (締切 window〜+tolerance 分前) で投票、SCORE 帯
    // (締切 score_window〜+score_tolerance 分前) で Claude 考察→各馬指数キャッシュ。
    window?: number;
    tolerance?: number;
    score_window?: number;
    score_tolerance?: number;
    // Claude 指数と model fundamental の合成重み (0=モデルのみ, 1=指数のみ, null=既定0.5)。
    llm_blend?: number | null;
    // 締切の何秒前に投票を発火するか (score 完了で予約 → この秒数で発火)。既定 60。
    bet_lead_sec?: number;
    interval_sec?: number;
    ev_max?: number | null;
    min_prob?: number | null;
    market_blend?: number | null;
    aptitude_top?: number | null;
    with_exacta?: boolean;
    with_trio?: boolean;
    // claude -p (各馬指数 score + Plan T 3連単選定) を使わず確率モデルのみ。
    no_llm?: boolean;
    // race detection を行う JST 時間帯 (HH:MM-HH:MM)。
    // backend (api/main.py:WatchAutoStartRequest) の default は "09:00-23:45"。
    active_hours?: string;
    // オッズパーク自動投票 (カート投入)。ON で投票 daemon (headful ブラウザ) を起動。
    bet_oddspark?: boolean;
    // 自動ログイン: ON で env 認証 (ODDSPARK_ID/PASSWORD/PIN) で自動ログイン。OFF は人が手で。
    bet_auto_login?: boolean;
    // 自動購入 (実弾): ON で #gotobuy まで自動。daily_cap で日次上限ガード。
    bet_auto_purchase?: boolean;
    bet_daily_cap?: number;   // 円
    // セッション中のみ Plan T 束の全 leg stake を N 倍 (100円単位丸め)。per-race 上限 / daily_cap は維持。
    bet_stake_multiplier?: number;
    // 支払方法: "opcoin" (OPコイン残, 既定) | "buylimit" (投票資金残, 会員入金)
    bet_payment_method?: "opcoin" | "buylimit";
    // JRA 即PAT 自動投票 (カート投入)。ON で JRA 投票 daemon (headful ブラウザ) を起動。
    bet_ipat?: boolean;
    // legacy (2026-06-06 以前): 投票束 Plan T トグルと専用倍率。現在は Plan T 固定で送信しない。
    // 旧 persist 済 config の prefill 互換のため型にだけ残す。
    bet_plan_t?: boolean;
    bet_plan_t_multiplier?: number;
    // Plan T の1レース購入予算 (円)。束の合計購入額をこの予算内に収める (Claude選定・モデル共通)。
    plan_t_bankroll?: number;
  };
  job: JobInfo | null;
  bet_job?: JobInfo | null;
  ipat_bet_job?: JobInfo | null;
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
  // 新スキーマ: "Plan A" (3連単 回収優先) | "Plan B" (3連単 的中優先) の 2 種のみ
  plan: string;
  races: number;
  // この Plan が買い目を出した (key_list 長 ≥ 1) レース数
  participated_races: number;
  hits: number;
  hit_rate: number;
  hit_rate_ci_low?: number;
  hit_rate_ci_high?: number;
  total_points: number;
  point_cost: number;
  // 想定枠 (¥)。新スキーマでは Plan A/B とも ¥10,000
  assumed_budget_slot: number;
  stake: number;
  payout: number;
  roi: number;
  // ROI bootstrap 95% 信頼区間 (n_iter=1000, seed=42 固定)
  roi_ci_low?: number;
  roi_ci_high?: number;
};

// Claude 選定 (recommended_bundle) の集計。**見送り (legs 空) は分母に含まない**。
export type ClaudeBundleAggregate = {
  races: number;                  // 全 join レース (見送り含む)
  participated_races: number;     // 賭けたレース (見送り除外)
  skipped_races: number;          // 見送りレース数
  hits: number;
  hit_rate: number;               // hits / participated_races
  hit_rate_ci_low?: number;
  hit_rate_ci_high?: number;
  stake: number;                  // Σ bundle stake (¥)
  // 予想オッズ (snapshot 時点) 基準の ROI
  payout: number;
  roi: number;
  roi_ci_low?: number;
  roi_ci_high?: number;
  // **最終オッズ** 基準の ROI (result fetch 時に保存される final_odds × stake)。
  // result に final_odds が無いレースは予想オッズに fallback (= payout と一致)。
  payout_final?: number;
  roi_final?: number;
  roi_final_ci_low?: number;
  roi_final_ci_high?: number;
};

export type CalibrationRaceItem = {
  race_id: string;
  venue: string;
  finish: number[];
  winning_tier: string | null;
  payout: number;
  // 回収優先 bundle (実弾で買う Claude 選定束) の的中。bet_type 横断 (3連単 + ワイド/馬連/etc)。
  bundle_hit?: boolean;
  bundle_hit_bet_types?: string[];
  bundle_participated?: boolean;
  bundle_stake?: number;
  bundle_payout?: number;              // 予想オッズ基準
  bundle_payout_final?: number;        // 最終オッズ基準 (result.final_odds × stake)
  // Plan T「3連単的中モード」bundle (市場無視・Claude 指数フォーメーション) の的中。
  // 古い snapshot は recommended_bundle_t 欠落 → participated=false。
  plan_t_hit?: boolean;
  plan_t_hit_bet_types?: string[];
  plan_t_participated?: boolean;
  plan_t_stake?: number;
  plan_t_payout?: number;              // 予想オッズ基準
  plan_t_payout_final?: number;        // 最終オッズ基準
  // 最終オッズ取得済 race か (frontend で「予想/最終」両表示の discriminator)
  has_final_odds?: boolean;
  // snapshot 保存時刻 (ISO8601 JST naive)。チャートでの時系列ソート / 表示用。
  saved_at?: string;
};

export type CalibrationReport = {
  race_count: number;
  point_cost: number;
  // 集計に含めた results の recorded_at 最大値 (ISO8601)。データ無しなら null。
  last_updated_at: string | null;
  // race_count < 30 のとき true。フロントで「参考値」バッジを出す目印。
  sample_warning?: boolean;
  tiers: CalibrationTier[];
  // 新スキーマでは plans は常に空配列 (旧 Plan A/B/C/F/G/H1/H2 廃止)。frontend は無視で OK。
  plans: CalibrationPlan[];
  // Claude 選定 回収優先 bundle (recommended_bundle) の集計
  claude_bundle?: ClaudeBundleAggregate;
  // Plan T「3連単的中モード」bundle (recommended_bundle_t) の集計。
  // 市場無視・的中優先の計測指標。claude_bundle と同形。古い snapshot は 0 集計。
  plan_t_bundle?: ClaudeBundleAggregate;
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
    score_window?: number;
    score_tolerance?: number;
    llm_blend?: number | null;
    bet_lead_sec?: number;
    interval_sec?: number;
    ev_max?: number | null;
    min_prob?: number | null;
    market_blend?: number | null;
    aptitude_top?: number | null;
    with_exacta?: boolean;
    with_trio?: boolean;
    no_llm?: boolean;
    active_hours?: string;
    bet_oddspark?: boolean;
    bet_auto_login?: boolean;
    bet_auto_purchase?: boolean;
    bet_daily_cap?: number;
    bet_stake_multiplier?: number;
    bet_payment_method?: "opcoin" | "buylimit";
    bet_ipat?: boolean;
    plan_t_bankroll?: number;
  }) =>
    jsonFetch<{ running: boolean; bet_running?: boolean; ipat_bet_running?: boolean; config: WatchAutoStatus["config"]; job: JobInfo }>(
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
