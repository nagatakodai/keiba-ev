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

// ダッシュボード仮想購入 (Claude 指数上位N頭3連単BOX + 戦略くらべ) の的中券種ラベル 1 件。
// EV束/3連単束 (実弾) の的中とは無関係 (ユーザ指示 2026-07-04)。payout は ¥100/脚換算。
export type HitBetLabel = { key: string; label: string; payout: number };

export type PredictionSummary = {
  race_id: string;
  saved_at: string;
  // "score"=Claude 指数出力時の暫定プレビュー / "bet"=締切直前の確定。旧 snapshot は欠落→"bet" 相当。
  stage?: "score" | "bet";
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
  // 入り、表示は 2 つの bundle (recommended_bundle_t 3連単束 実弾 + recommended_bundle EV束参考) に集約。
  // 旧 plan_*_count は backend が 0 を返すように、frontend からも参照しない。
  // 適性指数 top 3 (total 降順)。snapshot に horse_aptitude が無いと空配列。
  top_aptitude?: Array<{ number: number; name: string; total: number }>;
  has_evidence: boolean;
  // 補強根拠 (evidence) 方針バージョン: "v1"=3件上限 / "v2"=無制限 / null=Claude 指数なし。
  index_version?: string | null;
  has_result: boolean;
  // 仮想購入の的中券種ラベル。null=判定不能 (指数なし/結果未確定/オッズ欠落)、[]=的中なし。
  hit_strategies?: HitBetLabel[] | null;
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
  // scripts/backfill_bundle.py が後付けした paper 束 (実弾でない)。集計除外・UI はグレー表示。
  backfilled?: boolean;

  // claude -p による web 調査検証 (取消/不安材料を裏取りして cut)。未検証なら欠落。
  llm_review?: {
    validated: boolean;
    cuts?: string[];
    notes?: Record<string, string>;
    summary?: string;
    confidence?: string;
  };
};

// 3連単的中モード束 (3連単のみ・市場無視・Claude 指数フォーメーション・トリガミ防止あり)。
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
  // "score"=Claude 指数出力時の暫定プレビュー / "bet"=締切直前の確定。旧 snapshot は欠落→"bet" 相当。
  stage?: "score" | "bet";
  venue_name: string;
  race_class: string;
  schedule_index: number;
  race_number: number;
  odds_updated_at: number;
  close_at: number | null;
  start_at: number | null;
  // オッズ更新ボタン用: 経路 ("jra"/"keibago"/"oddspark"、netkeiba 経路は null) と
  // 再取得可否 (race_id から netkeiba rid を復元できるか)。
  odds_source?: string | null;
  can_refresh?: boolean | null;
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
  // 補強根拠 (evidence) 方針バージョン: "v1"=3件上限 / "v2"=無制限 / null=Claude 指数なし。
  index_version?: string | null;
  // Claude 強さ指数 (0-100) × 市場指数 を per-horse で併記 (差 = Claude − 市場、正 = Claude が強気)。
  // Claude 値降順 (無ければ市場指数降順)。support = 補強根拠件数 (検索で動かした裏付け)。
  index_compare?: Array<{
    number: number;
    name: string;
    claude_index: number | null;
    market_index: number | null;
    diff: number | null;
    // 仮指数 = 公式出走表だけの市場非依存な叩き台 (0-100)。prov_delta = Claude − 仮指数
    // (Claude が検索でどれだけ動かしたか)。無ければ null。
    provisional?: number | null;
    prov_delta?: number | null;
    support?: number | null;
    // 直前/軟情報フラグ (取消/馬体重増減/前走不利/厩舎勝負気配 等)。無ければ空配列。
    alerts?: string[] | null;
    // 補強根拠の詳細 (検索で見つけた裏付けを 1 件ずつ・上限なし)。無ければ空配列。
    evidence?: string[] | null;
    // パドック評価 (締切~5分前の直前情報)。rating=◎○△✕ / note=所見。無ければ null。
    paddock?: { rating: string; note: string } | null;
  }>;
  // 各馬の直前/軟情報フラグ (score ステージ由来)。記録/表示用 (確率には未使用)。
  llm_alerts?: Record<string, string[]> | null;
  // score→bet の単勝オッズ変化 (late-money momentum, paper 計測のみ・確率/束には未使用)。
  late_money?: {
    score_stage?: string;
    score_captured_at?: string;
    gap_min?: number;
    ratio?: Record<string, number>;   // 馬番 → bet時オッズ / score時オッズ (<1 = 直前に売れた)
    source_mix?: boolean;             // score と bet で取得元が異なる (微小変動はノイズ扱い)
  } | null;
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
  // 各馬の model 単勝確率 (馬番 → P)。出走頭数の推定 (複勝の頭数ルール) にも使う
  // (api/store.py と同じ優先順: win_probs_model → bet_tables.win → horse_aptitude)。
  win_probs_model?: Record<string, number> | null;
  // 出走頭数 (取消除く, 2026-06-11〜の snapshot に保存)。複勝の頭数ルール判定の権威値。
  n_runners?: number | null;
  // 馬連 (quinella) / ワイド (wide) / 馬単 (exacta) / 3連複 (trio) の EV table。
  // 各 bet type の top 30 行 (P×O 降順)。fetch されていない bet type はキー無し。
  bet_tables?: Record<string, BetEvRow[]>;
  // bet_tables の「適性ゲート → EV 足切り」picks (Plan G の bet type 版)。
  bet_tables_g?: Record<string, BetEvRow[]>;
  // 適性総合 top N 頭の馬番リスト (Plan G が依拠する集合)。
  aptitude_top_horses?: number[];
  // 新スキーマ (2026-05-29 後半): Plan A/B 自体は廃止。3連単 は bet_tables[trifecta] に入る。
  // 2 つの bundle を表示する (2026-06-06〜 3連単的中モード特化):
  //   recommended_bundle_t    : 3連単的中モード (**実弾投票束**, 固定)
  //   recommended_bundle      : EV束 (モデルのみの参考値、joint Kelly EV 最適、投票しない)
  recommended_bundle?: RecommendedBundle | null;
  // 3連単的中モード (全力フォーメーション): 3連単のみ・市場無視・EV/トリガミ無しの model 的中確率 top-K 束。
  // recommended_bundle (EV駆動) と完全分離。covered_prob = 理論的中率 (model 基準・過信禁物)。
  recommended_bundle_t?: TrifectaHitmaxBundle | null;
  trifecta_keys?: number[][];
  trifecta_params?: {
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
    // 結果ソース ("netkeiba-html" / "keibago" / "jra" / "auto" / 手動)。
    // netkeiba-html のとき final_odds は**払戻があった組のみ**の payout テーブル
    // (同着の的中判定フォールバックに使える — api/store.py _leg_hit と同規則)。
    source?: string;
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
    // 締切の何秒前に投票を発火するか (score 完了で予約 → この秒数で発火)。既定 150。
    bet_lead_sec?: number;
    interval_sec?: number;
    ev_max?: number | null;
    min_prob?: number | null;
    market_blend?: number | null;
    aptitude_top?: number | null;
    with_exacta?: boolean;
    with_trio?: boolean;
    // claude -p (各馬指数 score + 3連単買い目選定) を使わず確率モデルのみ。
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
    // セッション中のみ 3連単束の全 leg stake を N 倍 (100円単位丸め)。per-race 上限 / daily_cap は維持。
    bet_stake_multiplier?: number;
    // per-race 上限の専用倍率 (上限 = 基準¥10,000×N)。null/未設定なら掛金倍率に連動。
    bet_max_stake_multiplier?: number | null;
    // 支払方法: "opcoin" (OPコイン残, 既定) | "buylimit" (投票資金残, 会員入金)
    bet_payment_method?: "opcoin" | "buylimit";
    // JRA 即PAT 自動投票 (カート投入)。ON で JRA 投票 daemon (headful ブラウザ) を起動。
    bet_ipat?: boolean;
    // legacy (2026-06-06 以前): 投票束トグル (bet_plan_t) と専用倍率・旧予算キー (plan_t_bankroll)。
    // 現在は3連単的中モード固定で送信しない。旧 persist 済 config の prefill 互換のため型にだけ残す。
    bet_plan_t?: boolean;
    bet_plan_t_multiplier?: number;
    plan_t_bankroll?: number;
    // 3連単の1レース購入予算 (円)。束の合計購入額をこの予算内に収める (Claude選定・モデル共通)。
    trifecta_bankroll?: number;
    // 投票束 (2026-06-10 復活): "ev"=EV束 (recommended_bundle, 推奨既定) / "trifecta"=3連単束。
    // 旧 persist 済 config は欠落 (= 旧挙動 trifecta 扱い、resume 側で互換処理)。
    bet_bundle?: "ev" | "trifecta";
    // EV束の1レース予算 (円)。½Kelly なので実投入は通常この10-30%。
    ev_bankroll?: number;
    // score ステージ (Claude 指数) の検索並列化 (KEIBA_SCORE_PARALLEL)。既定 OFF。旧 config 欠落=OFF。
    score_parallel?: boolean;
    // score の1馬あたり検索クエリ数 (KEIBA_SCORE_QUERIES_PER_HORSE)。既定 6。旧 config 欠落=6。
    score_queries_per_horse?: number;
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
  // dispatch 段階 (score=指数取得 / bet=締切直前の束生成)。古い履歴には欠落。
  phase?: "score" | "bet";
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
  // EV束 (2026-06-10〜 実弾既定束) の的中。bet_type 横断 (3連単 + ワイド/馬連/etc)。
  bundle_hit?: boolean;
  bundle_hit_bet_types?: string[];
  bundle_participated?: boolean;
  bundle_stake?: number;
  bundle_payout?: number;              // 予想オッズ基準
  bundle_payout_final?: number;        // 最終オッズ基準 (result.final_odds × stake)
  // 3連単的中モード bundle (**実弾投票束**, 市場無視・Claude 指数フォーメーション) の的中。
  // 古い snapshot は recommended_bundle_t 欠落 → participated=false。
  trifecta_bundle_hit?: boolean;
  trifecta_bundle_hit_bet_types?: string[];
  trifecta_bundle_participated?: boolean;
  trifecta_bundle_stake?: number;
  trifecta_bundle_payout?: number;              // 予想オッズ基準
  trifecta_bundle_payout_final?: number;        // 最終オッズ基準
  // 3連単的中モードの計測対象か (saved_at >= trifecta_cutoff)。false は集計/チャート除外。
  trifecta_measured?: boolean;
  // EV束 (実弾既定束) の計測対象か (saved_at >= ev_cutoff = 修正版 EV束の稼働開始 2026-06-10)。
  ev_measured?: boolean;
  // 最終オッズ取得済 race か (frontend で「予想/最終」両表示の discriminator)
  has_final_odds?: boolean;
  // snapshot 保存時刻 (ISO8601 JST naive)。チャートでの時系列ソート / 表示用。
  saved_at?: string;
  // EV束が backfill (paper 後付け) で実弾でない race。集計から除外済・UI はグレーアウト表示。
  bundle_backfilled?: boolean;
};

// --- オッズタイムライン (GET /api/timeline/{race_id}) ---

export type TimelineRow = {
  stage: "score" | "bet" | "poll";
  captured_at: string;            // ISO8601 JST naive
  close_at: number;               // unix (0=不明)
  start_at: number;               // unix
  // win/place のみ (フルグリッドは depth 参照)。label = 馬番文字列。
  odds: { win?: Record<string, number>; place?: Record<string, number> };
  // 券種ごとの組合せ数 (どの券種がどの深さで取れていたか)
  depth?: Record<string, number>;
  source?: string;
};

export type TimelineResponse = {
  race_id: string;
  rows: TimelineRow[];
  result: { finish_order: number[]; final_odds: Record<string, number> } | null;
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
  // EV束の全期間参考集計 (β=0 事故時代込み。実弾系列には ev_bundle を使う)
  claude_bundle?: ClaudeBundleAggregate;
  // EV束 (**実弾既定束**, 2026-06-10〜) の集計。ev_cutoff 以降の race のみが分母。
  ev_bundle?: ClaudeBundleAggregate;
  // 3連単束 (recommended_bundle_t, KEIBA_BET_BUNDLE=trifecta 選択時の実弾束) の集計。
  // claude_bundle と同形。trifecta_cutoff 以降の race のみが分母に入る。
  trifecta_bundle?: ClaudeBundleAggregate;
  // 各系列の計測開始日 (ISO8601 JST naive)。注記表示用。
  trifecta_cutoff?: string;
  ev_cutoff?: string;
  races: CalibrationRaceItem[];
};

// api/store.py の EV_CUTOFF_ISO_JST と同値 (修正版 EV束が実弾既定束になった時点)。
// 詳細ページ等は calibrate API を引かずに snapshot の saved_at とこの定数で
// 「EV束計測対象 (実弾既定束 = EV束)」かを判定する。値を変える時は store と同時に。
export const EV_CUTOFF_ISO = "2026-06-10T18:21:00";
export const isEvMeasured = (savedAt?: string | null): boolean =>
  !!savedAt && savedAt >= EV_CUTOFF_ISO;

// --- 今日の勝負レース (GET /api/shobu/result, POST /api/shobu/scan) ---
// 判定は基準B (市場との順位乖離) 単独 (ユーザ指示 2026-06-28: 基準A=強弱は廃止)。

// 市場との順位乖離 (基準B): 市場ランク vs Claude ランクの食い違い。
export type ShobuEdgeHorse = {
  number: number | null;
  name: string;
  claude_index: number | null;
  market_index: number | null;
  claude_rank: number;       // Claude 指数での順位 (1=本命)
  market_rank: number;       // 市場での順位 (1=1番人気)
  rank_gap: number;          // market_rank − claude_rank (正 = Claude が上位評価=市場過小)
  diff: number;              // claude_index − market_index
  support: number | null;    // 補強根拠件数
  alerts: string[];
};
export type ShobuClaude = {
  available: boolean;
  edge_count: number;        // 乖離馬の数 (rank_gap≥1 かつ 指数差≥フロア)
  score: number;             // 0-100 市場乖離スコア (Claude本命の市場順位ギャップ主軸)
  // Claude 本命と、それが市場で何番人気か (market_rank=2 → 「市場2位なのにClaude1位」)
  top_pick: { number: number | null; name: string; market_rank: number } | null;
  top_rank_gap: number;      // Claude本命の市場順位 − 1 (1=市場2番人気を本命視)
  max_rank_gap: number;
  max_diff: number | null;
  edge_horses: ShobuEdgeHorse[];
  scored_at?: string | null;
};

export type ShobuRace = {
  netkeiba_race_id: string;
  race_id: string;           // 内部 race_id (/predictions/<id> への join key)
  venue: string;
  race_no: number;
  race_type: "jra" | "nar" | "banei";
  start_at: number;
  close_at: number;
  n_runners: number | null;
  // fresh=最新オッズ取得 / snapshot=既存スナップショット / none=データなし(未解析)
  data_source: "fresh" | "snapshot" | "none";
  has_snapshot: boolean;
  snapshot_stage?: "score" | "bet" | null;
  claude: ShobuClaude | null;
  recommended: boolean;          // 基準B (市場乖離) を満たす = 勝負レース
  matched: string[];             // "claude"
  shobu_score: number;           // ランキング用スコア (0-100) = 市場乖離スコア
  reasons: string[];
  // 2分毎の最新オッズ更新 (POST /api/shobu/refresh) で推奨レースに付く:
  // 勝負スコアの前回比 (score_delta = 今回−前回, 正=上昇) と時系列履歴。
  score_delta?: number | null;
  score_prev?: number | null;
  score_history?: Array<{ at: number; score: number }>;
  refreshed_at?: number | null;  // 最終 refresh の unix 秒
  // 仮想購入 (ダッシュボード計測) の的中券種ラベル。配信時に snapshot+result から付与。
  // null=判定不能 (結果未確定など)、[]=的中なし。
  hit_strategies?: HitBetLabel[] | null;
};

export type ShobuResult = {
  date: string;
  generated_at: string;
  // 最新オッズ更新 (refresh) の最終時刻 (ISO8601 JST)。未更新なら欠落。
  refreshed_at?: string;
  options: {
    date: string;
    race_type: "all" | "jra" | "nar" | "banei";
    edge_margin: number;
    edge_threshold: number;
    upcoming_only: boolean;
    claude_all: boolean;
    claude_eval: number;
  };
  summary: {
    total_discovered: number;
    evaluated: number;
    recommended: number;
    with_snapshot: number;
    with_claude: number;
    with_fresh_odds: number;
    by_type?: { jra: number; nar: number; banei: number };
  };
  races: ShobuRace[];
  // Claude 指数の一括生成進捗 (ユーザ指示 2026-06-22)。生成がある時、scan は生成前に暫定一覧
  // (基準A中心・generating=true) を先出しし、各レース生成完了ごとに live 更新、全完了で
  // generating=false の確定版 (基準B=市場乖離 反映済) に切替える。生成が無い scan は最初から false。
  generating?: boolean;
  gen_total?: number;   // 生成対象レース数
  gen_done?: number;    // 生成完了レース数 (基準B 確定済)
};

// 予測分析履歴の結果 (着順/払戻) 自動取得 (make api 稼働中に N 秒毎) の状態。
export type ResultsAutoStatus = {
  interval_sec: number;
  loop_running: boolean;        // 常駐ループが生きているか
  last_run_at: number | null;   // 直近実行の unix 秒
  next_run_at: number | null;   // 次回実行予定の unix 秒
  runs: number;
  last_summary: {
    enqueued?: number;          // この回 enqueue した発走済予測の数
    checked?: number;
    success?: number;           // 取得できた結果数
    failed?: number;
    not_due?: number;
    error?: string;
  } | null;
};

// 勝負レース専用の仮想収支 (GET /api/shobu/pnl)。Claude 指数上位N頭の3連単BOXを
// 買ったと仮定した paper P&L。per-race 明細は races_detail。
export type ShobuPnlRace = {
  race_id: string;
  date: string;
  venue: string;
  race_no: number | null;
  race_type: "jra" | "nar" | "banei" | null;
  shobu_score: number | null;
  matched: string[];
  n_runners: number | null;
  box: number;             // BOX に使った上位頭数 (7頭立て=4 等)
  top_horses: number[];    // Claude 指数上位N頭 (馬番)
  finish: number[];        // 実 1-2-3着
  n_points: number;        // 3連単 BOX 点数 (P(box,3))
  stake: number;
  hit: boolean;
  payout: number;
  trifecta_payout: number;
  saved_at?: string | null;
};
export type ShobuPnl = {
  point_cost: number;
  box_size: number;
  races: number;
  hits: number;
  hit_rate: number;
  hit_rate_ci_low?: number;
  hit_rate_ci_high?: number;
  stake: number;
  payout: number;
  roi: number;
  roi_ci_low?: number;
  roi_ci_high?: number;
  recommended_total: number;
  // 補強根拠バージョン ("v1"/"v2"/null=全体)。version 毎に分離した計測のとき set。
  version?: string | null;
  skipped_no_index: number;
  skipped_no_result: number;
  last_updated_at: string | null;
  sample_warning: boolean;
  races_detail: ShobuPnlRace[];
};

// Claude 指数 単純戦略くらべの仮想収支 (GET /api/shobu/strategies-pnl, indexed-strategies-pnl)。
// win1/place1/place2/place3/quinella12/wide12/wide13/exacta12/trifecta123/trio123/trio1234box/wide123box を比較 (2026-06-30)。
export type StrategyPnl = {
  key: string;                 // win1 / place1 / place2 / place3 / quinella12 / wide12 / wide13 / exacta12 / trifecta123 / trio123 / trio1234box / wide123box
  label: string;               // 表示名 (例「単勝 (指数1位)」)
  bet_type: string;            // win / place / quinella / wide / exacta / trifecta / trio
  races: number;               // 実際に1脚以上買ったレース数 (フィルタ後)
  races_hit: number;           // レース単位の的中数 (hit_rate の分子)
  bets: number;                // 脚数 (trio1234box=4/レース, wide123box=3/レース)。stake 算出用
  hits: number;                // 的中脚数 (脚単位)
  hit_rate: number;            // races_hit / races (**母数=レース数**)
  hit_rate_ci_low?: number;
  hit_rate_ci_high?: number;
  stake: number;
  payout: number;
  net: number;
  roi: number;
  roi_ci_low?: number;
  roi_ci_high?: number;
};
export type StrategyRaceDetail = {
  race_id: string;
  date: string;
  venue: string;
  race_no: number | null;
  race_type: "jra" | "nar" | "banei" | null;
  shobu_score: number | null;
  n_runners: number | null;
  place_cutoff: number;
  top1: number;
  top2: number;
  top3: number;
  finish: number[];
  per: Record<
    string,
    { stake: number; payout: number; bets: number; hits: number; hit: boolean }
  >;
  saved_at?: string | null;
};
export type StrategiesPnl = {
  point_cost: number;
  strategies: StrategyPnl[];
  races: number;
  recommended_total: number;
  // 補強根拠バージョン ("v1"/"v2"/null=全体)。version 毎に分離した計測のとき set。
  version?: string | null;
  skipped_no_index: number;
  skipped_no_result: number;
  skipped_no_odds: number;
  last_updated_at: string | null;
  sample_warning: boolean;
  races_detail: StrategyRaceDetail[];
};

// 競馬場 (venue) 毎の内訳 (GET /api/shobu/venue-breakdown)。BOX + 各戦略を venue で集計 (2026-06-30)。
export type VenueRoiBlock = {
  races: number;
  races_hit: number;
  hit_rate: number;
  stake: number;
  payout: number;
  net: number;
  roi: number;
};
export type VenueStrategy = VenueRoiBlock & { key: string; label: string };
export type VenueBreakdownItem = {
  venue: string;
  n_races: number;
  box: VenueRoiBlock;
  strategies: VenueStrategy[];
};
export type VenueBreakdown = {
  point_cost: number;
  version?: string | null;
  venues: VenueBreakdownItem[];
  last_updated_at: string | null;
};

// 市場一致シグナル (GET /api/shobu/market-agreement)。Claude#1==市場1番人気で券種ROIを分割し
// 差Δの bootstrap CI が 0 から離れる (=確証) まで結果取得ループが自動蓄積する (2026-06-30)。
export type MarketAgreementMetric = {
  key: string;
  label: string;
  agree_roi: number;
  disagree_roi: number;
  agree_legs: number;   // 1脚以上買ったレース数 (2026-07-04 レース単位合算化)
  disagree_legs: number;
  delta: number; // agree_roi − disagree_roi
  delta_ci_low: number;
  delta_ci_high: number;
  significant: boolean; // CI が 0 を跨がない
};
export type MarketAgreement = {
  races: number;
  agree_n: number;
  disagree_n: number;
  metrics: MarketAgreementMetric[];
  last_updated_at: string | null;
  sample_warning: boolean;
};
export type MarketAgreementResponse = {
  current: MarketAgreement;
  history: Array<{
    recorded_at: string;
    races: number;
    agree_n: number;
    disagree_n: number;
    metrics: Array<{ key: string; delta: number; significant: boolean }>;
  }>;
  appends: number;
};

export type ShobuScanRequest = {
  date?: string | null;
  race_type?: "all" | "jra" | "nar" | "banei";
  edge_margin?: number;
  edge_threshold?: number;
  upcoming_only?: boolean;
  claude_all?: boolean;
  claude_eval?: number;
  max_races?: number | null;
  claude_eval_parallel?: number;
  score_parallel?: boolean;
  score_queries_per_horse?: number;
  llm_max_concurrent?: number;
};

// --- Endpoints ---

export const api = {
  listPredictions: (limit = 100) =>
    jsonFetch<{ items: PredictionSummary[] }>(`/api/predictions?limit=${limit}`),
  getPrediction: (raceId: string) =>
    jsonFetch<PredictionDetail>(`/api/predictions/${encodeURIComponent(raceId)}`),
  calibrate: (pointCost = 100) =>
    jsonFetch<CalibrationReport>(`/api/calibrate?point_cost=${pointCost}`),
  // タイムライン未取得 race は 404 → 呼び元で握って「データなし」表示にする。
  getTimeline: (raceId: string) =>
    jsonFetch<TimelineResponse>(`/api/timeline/${encodeURIComponent(raceId)}`),

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
    // "score" = Claude 指数のみ (暫定 snapshot) / "bet" = 指数+市場で束まで生成 (既定)。
    phase?: "score" | "bet";
    // score タブの検索チューニング (phase=score 時のみ): 並列化 / 1馬あたりクエリ数 / 締切秒。
    score_parallel?: boolean;
    score_queries_per_horse?: number;
    score_timeout?: number;
  }) =>
    jsonFetch<JobInfo>(`/api/analyze`, { method: "POST", body: JSON.stringify(body) }),

  // 今日の勝負レース スキャンを起動 (Job)。完了後 getShobuResult で結果を取得。
  scanShobu: (body: ShobuScanRequest) =>
    jsonFetch<JobInfo & { date: string }>(`/api/shobu/scan`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // 勝負レース スキャンの最新結果。未スキャンは 404 → 呼び元で握って「未スキャン」表示。
  getShobuResult: (date?: string) =>
    jsonFetch<ShobuResult>(
      `/api/shobu/result${date ? `?date=${encodeURIComponent(date)}` : ""}`,
    ),
  // 推奨 (勝負レース) のみ最新オッズで再採点 (2分毎ポーリング用)。Claude は呼ばず単勝 1 fetch/レース。
  // 勝負スコアの履歴・前回比 (score_delta) 付きの更新済 ShobuResult を返す。未スキャンは 404。
  refreshShobu: (date?: string) =>
    jsonFetch<ShobuResult>(`/api/shobu/refresh`, {
      method: "POST",
      body: JSON.stringify({ date: date ?? null }),
    }),
  // 勝負レース専用の仮想収支 (Claude 指数上位N頭の3連単 BOX を買ったと仮定)。
  shobuPnl: (pointCost = 100) =>
    jsonFetch<ShobuPnl>("/api/shobu/pnl?point_cost=" + pointCost),
  // 全 Claude 指数レース (recommended に限らない・全馬指数+結果あり) の仮想収支。
  // version ("v1"/"v2") を渡すと補強根拠バージョン毎に分離。
  indexedPnl: (pointCost = 100, version?: string) =>
    jsonFetch<ShobuPnl>(
      `/api/shobu/indexed-pnl?point_cost=${pointCost}${
        version ? `&version=${encodeURIComponent(version)}` : ""
      }`,
    ),
  // Claude 指数 単純戦略くらべ (単勝/複勝/馬連/単複) の仮想収支 — 勝負レース(推奨)のみ。
  strategiesPnl: (pointCost = 100) =>
    jsonFetch<StrategiesPnl>("/api/shobu/strategies-pnl?point_cost=" + pointCost),
  // Claude 指数 単純戦略くらべ — shobu 評価レース全体 (過去分全て・recommended に限らない)。
  // version ("v1"/"v2") を渡すと補強根拠バージョン毎に分離。
  indexedStrategiesPnl: (pointCost = 100, version?: string) =>
    jsonFetch<StrategiesPnl>(
      `/api/shobu/indexed-strategies-pnl?point_cost=${pointCost}${
        version ? `&version=${encodeURIComponent(version)}` : ""
      }`,
    ),
  // 競馬場 (venue) 毎の内訳 仮想収支 (BOX + 戦略くらべ)。version で v2/v1/β 分離。
  venueBreakdown: (version?: string, pointCost = 100) =>
    jsonFetch<VenueBreakdown>(
      `/api/shobu/venue-breakdown?point_cost=${pointCost}${
        version ? `&version=${encodeURIComponent(version)}` : ""
      }`,
    ),
  // 市場一致シグナル (Claude#1==市場1番人気で券種ROI分割) の現在値 + 蓄積履歴。
  marketAgreement: () =>
    jsonFetch<MarketAgreementResponse>("/api/shobu/market-agreement"),
  // 予測分析履歴の結果 自動取得ループの状態 (make api 稼働中に 5 分毎)。
  getResultsAuto: () => jsonFetch<ResultsAutoStatus>(`/api/results/auto`),

  // 履歴のレースを今すぐ最新オッズで score 再評価 (per-route 即時 fetch)。Job を返す。
  refreshOdds: (raceId: string) =>
    jsonFetch<JobInfo>(`/api/predictions/${encodeURIComponent(raceId)}/refresh-odds`, {
      method: "POST",
    }),

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
    bet_max_stake_multiplier?: number | null;
    bet_payment_method?: "opcoin" | "buylimit";
    bet_ipat?: boolean;
    trifecta_bankroll?: number;
    bet_bundle?: "ev" | "trifecta";
    ev_bankroll?: number;
    score_parallel?: boolean;
    score_queries_per_horse?: number;
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
