"use client";

import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  Input,
  Page,
  PageHeader,
  Select,
  Stat,
  fmtTime,
  fmtTs,
  planAccentClass,
  raceTimingRowBg,
  raceTimingStatus,
} from "@/components/ui";
import Link from "next/link";
import { LogStream } from "@/components/LogStream";
import { PendingRecorder } from "@/components/PendingRecorder";
import { useWatchStatus } from "@/components/WatchStatusContext";
import { isEvMeasured,
  api,
  type PredictionDetail,
  type WatchAutoHistoryItem,
} from "@/lib/api";

function fmtPicks(keys: number[][] | undefined): string {
  if (!keys || keys.length === 0) return "—";
  return keys.map((k) => k.join("-")).join("  ");
}

export default function WatchAutoPage() {
  const { status, refresh: refreshStatus } = useWatchStatus();
  const [history, setHistory] = useState<WatchAutoHistoryItem[]>([]);
  // race_id → PredictionDetail | null (fetch failed). undefined = まだ取得していない。
  const [picks, setPicks] = useState<Record<string, PredictionDetail | null>>({});
  // クライアント側の現在時刻 (締切バッジ計算用)。SSR との一致を保つため初期は null。
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 設定/制御パネルはデフォルト閉。ユーザがプルダウンを開いたときだけ展開。
  const [showSettings, setShowSettings] = useState(false);

  // 2段パイプライン: SCORE 帯 (締切 score_window〜+tol 分前) で Claude 考察→各馬指数を
  // キャッシュ + 投票を予約 → 締切 bet_lead_sec 秒前に自動発火 (最新オッズ→束→投票)。
  const [scoreWindow, setScoreWindow] = useState("5");
  const [scoreTolerance, setScoreTolerance] = useState("2");
  // 既定 150s (2026-06-11 実測ベース: bet dispatch 31-95s + daemon poll + カート投入 20-40s。
  // 旧 60s は締切に間に合わず不成立=賭け逃しが出ていた)。
  const [betLeadSec, setBetLeadSec] = useState("150");
  // 空 = backend 既定 (ev.LLM_BLEND_DEFAULT=0.5)。Claude 指数 vs モデルの合成重み。
  const [llmBlend, setLlmBlend] = useState("");
  const [intervalSec, setIntervalSec] = useState("60");
  const [evMax, setEvMax] = useState("");
  const [minProb, setMinProb] = useState("2.0");
  // 空 = backend BLEND_DEFAULT (=0.78) を使う。CLAUDE.md の production 設定。
  // 過去 "0.9" を default にしていたが CLI / make watch-auto と挙動が乖離していた。
  const [marketBlend, setMarketBlend] = useState("");
  const [aptitudeTop, setAptitudeTop] = useState("6");
  const [activeHours, setActiveHours] = useState("09:00-23:45");
  const [withExacta, setWithExacta] = useState(false);
  const [withTrio, setWithTrio] = useState(false);
  // claude -p (各馬指数 score + 3連単買い目選定) を使わず確率モデルのみで分析。
  const [noLlm, setNoLlm] = useState(false);
  // オッズパーク自動投票 (カート投入)。ON で投票 daemon (headful ブラウザ) が起動し、人がログイン。
  const [betOddspark, setBetOddspark] = useState(false);
  // JRA 即PAT 自動投票 (カート投入)。ON で JRA 投票 daemon (headful ブラウザ) が起動 (土日 JRA 用)。
  const [betIpat, setBetIpat] = useState(false);
  // 投票束は 3連単的中モード (recommended_bundle_t) 固定 (2026-06-06)。旧トグルは廃止。
  // 自動ログイン: ON で env 認証 (ODDSPARK_ID/PASSWORD/PIN) で自動ログイン。OFF は人が手でログイン。
  const [betAutoLogin, setBetAutoLogin] = useState(false);
  // **自動購入 (実弾)** モード: ON で #gotobuy → 確認画面 → 確定 まで自動 (人の介入なし)。
  // bet_oddspark が ON でないと意味が無い。daily_cap (円) で日次上限ガード。
  const [betAutoPurchase, setBetAutoPurchase] = useState(false);
  const [betDailyCap, setBetDailyCap] = useState("10000");
  // セッション中のみ 3連単束の全 leg stake を倍率倍に (小数倍可・100円単位切り捨て)。1.0=既定。
  const [betStakeMultiplier, setBetStakeMultiplier] = useState("1");
  // per-race 上限の専用倍率 (上限 = 基準¥10,000×N)。空 (既定) なら掛金倍率に連動。
  const [betMaxStakeMultiplier, setBetMaxStakeMultiplier] = useState("");
  // 3連単の1レース購入予算 (円)。束の合計購入額をこの予算内に収める (Claude選定・モデル共通)。
  // 初期値は計測モード ¥2,000 (2026-06-10 実測: 3連単束は全系列 ROI 14-83% — rolling ROI>100%
  // が出るまで実弾は最小限に。bundle_calibration_report.py で定点観測)。
  const [trifectaBankroll, setTrifectaBankroll] = useState("2000");
  // 3連単束モード: recovery=回収(穴狙い, 市場1番人気はClaude指数>90でない限り1着に置かない, 既定) /
  // hit=旧 全力的中。env KEIBA_TRIFECTA_MODE で全 dispatch subprocess に伝播。
  const [trifectaMode, setTrifectaMode] = useState<"recovery" | "hit">("recovery");
  // 投票束 (2026-06-10 レビュー後の推奨既定 = EV束): "ev"=EV束 — 全脚がドリフトシェード込み
  // P×O≥1.02 + px_o≤2.0 + ½Kelly + トリガミ防止を通過した時のみ買う (大半のレースは見送り =
  // 正しい挙動)。"trifecta"=3連単束 (Claude 指数・市場無視, 実測 -EV のため計測モード推奨)。
  const [betBundle, setBetBundle] = useState<"ev" | "trifecta">("ev");
  // EV束の1レース予算 (円)。½Kelly なので実投入は通常この10-30%。+EV 未実証のため計測モード初期値。
  const [evBankroll, setEvBankroll] = useState("5000");
  // 支払方法: opcoin (OPコイン残, 既定) または buylimit (投票資金残)
  const [betPaymentMethod, setBetPaymentMethod] = useState<"opcoin" | "buylimit">("opcoin");

  // 前回使った設定 (backend に persist 済 status.config) を form の default に流し込む。
  // status は 5s 間隔で polling されるので、ユーザ編集を上書きしないよう初回 config 到着時に
  // 1 度だけ適用する。config が null (停止中・API 未応答) のときは上の hardcode default のまま。
  // React 公式の「レンダー中に前回値から state 調整」パターン (条件 guard で無限ループ防止)。
  // 初回 config 到着時に 1 度だけ form を前回値で埋める。
  const [prefilled, setPrefilled] = useState(false);
  if (!prefilled && status?.config) {
    const c = status.config;
    setPrefilled(true);
    if (c.score_window != null) setScoreWindow(String(c.score_window));
    if (c.score_tolerance != null) setScoreTolerance(String(c.score_tolerance));
    if (c.bet_lead_sec != null) setBetLeadSec(String(c.bet_lead_sec));
    setLlmBlend(c.llm_blend != null ? String(c.llm_blend) : "");
    if (c.interval_sec != null) setIntervalSec(String(c.interval_sec));
    setEvMax(c.ev_max != null ? String(c.ev_max) : "");
    setMinProb(c.min_prob != null ? String(c.min_prob) : "");
    setMarketBlend(c.market_blend != null ? String(c.market_blend) : "");
    if (c.aptitude_top != null) setAptitudeTop(String(c.aptitude_top));
    if (c.active_hours != null) setActiveHours(String(c.active_hours));
    if (c.with_exacta != null) setWithExacta(!!c.with_exacta);
    if (c.with_trio != null) setWithTrio(!!c.with_trio);
    if (c.no_llm != null) setNoLlm(!!c.no_llm);
    if (c.bet_oddspark != null) setBetOddspark(!!c.bet_oddspark);
    if (c.bet_ipat != null) setBetIpat(!!c.bet_ipat);
    if (c.bet_auto_login != null) setBetAutoLogin(!!c.bet_auto_login);
    if (c.bet_auto_purchase != null) setBetAutoPurchase(!!c.bet_auto_purchase);
    if (c.bet_daily_cap != null) setBetDailyCap(String(c.bet_daily_cap));
    // 旧 config 互換: 旧トグル (bet_plan_t) 時代は 3連単専用倍率 (bet_plan_t_multiplier) が実効値
    // だったので、そちらを優先して掛金倍率に流し込む (束は今や常に3連単束)。
    if (c.bet_plan_t && c.bet_plan_t_multiplier != null)
      setBetStakeMultiplier(String(c.bet_plan_t_multiplier));
    else if (c.bet_stake_multiplier != null) setBetStakeMultiplier(String(c.bet_stake_multiplier));
    setBetMaxStakeMultiplier(
      c.bet_max_stake_multiplier != null ? String(c.bet_max_stake_multiplier) : "");
    // 旧 config 互換: 旧キー plan_t_bankroll で persist された予算も読む。
    if (c.trifecta_bankroll != null) setTrifectaBankroll(String(c.trifecta_bankroll));
    else if (c.plan_t_bankroll != null) setTrifectaBankroll(String(c.plan_t_bankroll));
    if (c.trifecta_mode === "recovery" || c.trifecta_mode === "hit")
      setTrifectaMode(c.trifecta_mode);
    // 旧 config (bet_bundle キー無し) は旧挙動 = 3連単束として表示 (黙って EV束に変えない)。
    if (c.bet_bundle === "ev" || c.bet_bundle === "trifecta") setBetBundle(c.bet_bundle);
    else if (c.trifecta_bankroll != null || c.trifecta_mode != null) setBetBundle("trifecta");
    if (c.ev_bankroll != null) setEvBankroll(String(c.ev_bankroll));
    if (c.bet_payment_method === "buylimit" || c.bet_payment_method === "opcoin")
      setBetPaymentMethod(c.bet_payment_method);
  }

  const refreshHistory = async () => {
    try {
      const h = await api.watchHistory(200);
      setHistory(h.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const h = await api.watchHistory(200);
        if (!cancelled) setHistory(h.items);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    tick();
    const t = setInterval(tick, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.startWatch({
        score_window: Number.isFinite(parseFloat(scoreWindow)) ? parseFloat(scoreWindow) : 5,
        score_tolerance: Number.isFinite(parseFloat(scoreTolerance)) ? parseFloat(scoreTolerance) : 2,
        bet_lead_sec: parseInt(betLeadSec) || 60,
        llm_blend: llmBlend === "" ? null : parseFloat(llmBlend),
        interval_sec: parseInt(intervalSec) || 60,
        ev_max: evMax === "" ? null : parseFloat(evMax),
        min_prob: minProb === "" ? null : parseFloat(minProb),
        market_blend: marketBlend === "" ? null : parseFloat(marketBlend),
        aptitude_top: aptitudeTop === "" ? null : parseInt(aptitudeTop, 10),
        active_hours: activeHours.trim() || "09:00-23:45",
        with_exacta: withExacta,
        with_trio: withTrio,
        no_llm: noLlm,
        bet_oddspark: betOddspark,
        bet_ipat: betIpat,
        bet_auto_login: betAutoLogin,
        bet_auto_purchase: betAutoPurchase,
        // 0 を許容 (cap=0 で無効化を意図的に表現できる)。NaN/負値だけ既定に戻す。
        bet_daily_cap: (() => {
          const v = parseInt(betDailyCap, 10);
          return Number.isFinite(v) && v >= 0 ? v : 50000;
        })(),
        // 0 を許容 (cap=0 で無効化、multiplier は backend Pydantic で gt=0 拒否される)。
        // `|| default` だと 0 が既定で上書きされ意図と異なる挙動になるため NaN 判定で分岐。
        bet_stake_multiplier: (() => {
          const v = parseFloat(betStakeMultiplier);
          return Number.isFinite(v) && v > 0 ? v : 1.0;
        })(),
        // per-race 上限倍率。空欄 = null = 掛金倍率に連動 (既定)。NaN/0以下も null に倒す。
        bet_max_stake_multiplier: (() => {
          if (betMaxStakeMultiplier.trim() === "") return null;
          const v = parseFloat(betMaxStakeMultiplier);
          return Number.isFinite(v) && v > 0 ? v : null;
        })(),
        // 3連単の1レース購入予算 (円)。backend は ge=100 拒否なので NaN/100未満は既定 10000 に。
        trifecta_bankroll: (() => {
          const v = parseInt(trifectaBankroll, 10);
          return Number.isFinite(v) && v >= 100 ? v : 10000;
        })(),
        trifecta_mode: trifectaMode,
        bet_bundle: betBundle,
        // EV束の1レース予算 (円)。backend は ge=100 拒否なので NaN/100未満は既定 5000 に。
        ev_bankroll: (() => {
          const v = parseInt(evBankroll, 10);
          return Number.isFinite(v) && v >= 100 ? v : 5000;
        })(),
        bet_payment_method: betPaymentMethod,
      });
      await Promise.all([refreshStatus(), refreshHistory()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.stopWatch();
      await Promise.all([refreshStatus(), refreshHistory()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const running = !!status?.running;
  const job = status?.job;

  // 締切バッジを 5 秒間隔で再評価。マウント後に初期化することで SSR との不一致を避ける。
  useEffect(() => {
    setNowMs(Date.now());
    const t = setInterval(() => setNowMs(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);

  // 表示する最新 3 件分の予測スナップショットを差分取得 (Plan A/B/C の買い目を表示するため)。
  // 既に取得済みの race_id はスキップ。失敗 (404 / 401) は null として記録し再試行しない。
  // history は新しい順で返ってくる前提。同じ race_id が複数あれば最新の 1 件だけ残す。
  const seen = new Set<string>();
  const deduped: WatchAutoHistoryItem[] = [];
  for (const h of history) {
    if (seen.has(h.race_id)) continue;
    seen.add(h.race_id);
    deduped.push(h);
  }
  const recentHistory = deduped.slice(0, 3);
  useEffect(() => {
    let cancelled = false;
    for (const h of recentHistory) {
      const id = h.race_id;
      if (id in picks) continue;
      api
        .getPrediction(id)
        .then((p) => {
          if (!cancelled) setPicks((prev) => ({ ...prev, [id]: p }));
        })
        .catch(() => {
          if (!cancelled) setPicks((prev) => ({ ...prev, [id]: null }));
        });
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recentHistory.map((h) => h.race_id).join("|")]);

  return (
    <Page>
          <PageHeader
            title="自動予測分析・投票"
            subtitle="公式ソースの当日開催一覧を polling し、発走間際のレースを自動で予測分析する。"
          />

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="状態"
          value={running ? "稼働中" : "停止"}
          tone={running ? "good" : "default"}
        />
        <Stat
          label="考察→投票 (締切まで)"
          value={
            status?.config?.score_window != null
              ? `考察 ${status.config.score_window}〜${(status.config.score_window ?? 0) + (status.config.score_tolerance ?? 0)}分前 → 投票 締切${status.config.bet_lead_sec ?? 60}秒前`
              : "—"
          }
        />
        <Stat
          label="polling 間隔"
          value={status?.config?.interval_sec ? `${status.config.interval_sec}s` : "—"}
        />
        <Stat label="自動予測分析 件数" value={history.length} />
      </div>

      <Card
        title={
          <button
            type="button"
            onClick={() => setShowSettings((v) => !v)}
            className="flex items-center gap-1.5 -my-1 text-left"
            aria-expanded={showSettings}
          >
            <span
              className={`inline-block transition-transform text-(--color-muted) text-xs ${
                showSettings ? "rotate-90" : ""
              }`}
            >
              ▶
            </span>
            <span>設定 / 制御</span>
          </button>
        }
        right={
          <div className="flex items-center gap-2">
            {running ? (
              <Button variant="danger" size="lg" disabled={busy} onClick={stop}>
                <span aria-hidden>■</span>
                {busy ? "停止中..." : "停止"}
              </Button>
            ) : (
              <Button size="lg" disabled={busy} onClick={start}>
                <span aria-hidden>▶</span>
                {busy ? "起動中..." : "開始"}
              </Button>
            )}
          </div>
        }
      >
        {showSettings ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
              <Input
                label="BET LEAD (締切の何秒前に投票発火 / 既定 60=締切1分前)"
                type="number"
                step="10"
                min="0"
                value={betLeadSec}
                onChange={(e) => setBetLeadSec(e.target.value)}
                disabled={running}
              />
              <Input
                label="SCORE WINDOW (締切までの目標分 / Claude 考察→指数。BET より手前に)"
                type="number"
                step="0.5"
                min="0"
                value={scoreWindow}
                onChange={(e) => setScoreWindow(e.target.value)}
                disabled={running}
              />
              <Input
                label="SCORE TOLERANCE (+分 / 締切 score_window〜+tol 分前で考察)"
                type="number"
                step="0.5"
                min="0"
                value={scoreTolerance}
                onChange={(e) => setScoreTolerance(e.target.value)}
                disabled={running}
              />
              <Input
                label="LLM 合成重み (空=既定0.5 / 0=モデルのみ 1=指数のみ)"
                placeholder="0.0–1.0"
                value={llmBlend}
                onChange={(e) => setLlmBlend(e.target.value)}
                disabled={running}
              />
              <Input
                label="INTERVAL_SEC (polling 間隔)"
                value={intervalSec}
                onChange={(e) => setIntervalSec(e.target.value)}
                disabled={running}
              />
              <Input
                label="期待値上限"
                placeholder="例: 3"
                value={evMax}
                onChange={(e) => setEvMax(e.target.value)}
                disabled={running}
              />
              <Input
                label="最低当選率 (%)"
                placeholder="例: 2.0"
                value={minProb}
                onChange={(e) => setMinProb(e.target.value)}
                disabled={running}
              />
              <Input
                label="市場確率ブレンド"
                placeholder="0.0–1.0"
                value={marketBlend}
                onChange={(e) => setMarketBlend(e.target.value)}
                disabled={running}
              />
              <Input
                label="Plan G 適性 top N 頭"
                placeholder="6 (default)"
                value={aptitudeTop}
                onChange={(e) => setAptitudeTop(e.target.value)}
                disabled={running}
              />
              <Select
                label="投票束"
                value={betBundle}
                onChange={(e) => setBetBundle(e.target.value as "ev" | "trifecta")}
                disabled={running}
                hint={
                  betBundle === "ev"
                    ? "全脚がシェード込み P×O≥1.02 + ½Kelly + トリガミ防止を通過した時のみ買う。大半のレースは見送り (正常)"
                    : "Claude 指数フォーメーション・市場無視。実測 ROI 14-83% (-EV) のため計測モード予算を推奨"
                }
              >
                <option value="ev">EV束 (推奨・修正後)</option>
                <option value="trifecta">3連単束 (Claude 指数)</option>
              </Select>
              {betBundle === "ev" && (
                <Input
                  label="EV束 1レース予算 (¥)"
                  placeholder="5000 (計測モード推奨)"
                  value={evBankroll}
                  onChange={(e) => setEvBankroll(e.target.value)}
                  disabled={running}
                />
              )}
              {betBundle === "trifecta" && (
                <Input
                  label="3連単 1レース購入予算 (¥)"
                  placeholder="2000 (計測モード推奨)"
                  value={trifectaBankroll}
                  onChange={(e) => setTrifectaBankroll(e.target.value)}
                  disabled={running}
                />
              )}
              {betBundle === "trifecta" && (
                <Select
                  label="3連単束モード"
                  value={trifectaMode}
                  onChange={(e) => setTrifectaMode(e.target.value as "recovery" | "hit")}
                  disabled={running}
                  hint={
                    trifectaMode === "recovery"
                      ? "市場1番人気は単勝1.5倍未満の鉄板か Claude 指数 > 90 でない限り1着に置かない"
                      : "旧 全力的中: 1着除外なし (Claude 指数上位をそのまま1着候補に)"
                  }
                >
                  <option value="recovery">回収 (穴狙い) — 既定</option>
                  <option value="hit">的中 (旧 全力的中)</option>
                </Select>
              )}
              <Input
                label="稼働時間帯 (JST HH:MM-HH:MM)"
                placeholder="09:00-23:45"
                value={activeHours}
                onChange={(e) => setActiveHours(e.target.value)}
                disabled={running}
              />
            </div>
            <div className="mt-3 flex items-center gap-4 text-sm flex-wrap">
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={withExacta}
                  onChange={(e) => setWithExacta(e.target.checked)}
                  disabled={running}
                  className="accent-(--color-accent)"
                />
                <span>馬単も取得 (jiku iter / fetch +40s)</span>
              </label>
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={withTrio}
                  onChange={(e) => setWithTrio(e.target.checked)}
                  disabled={running}
                  className="accent-(--color-accent)"
                />
                <span>3 連複も取得 (jiku iter / fetch +40s)</span>
              </label>
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={noLlm}
                  onChange={(e) => setNoLlm(e.target.checked)}
                  disabled={running}
                  className="accent-(--color-accent)"
                />
                <span>LLM を使わない (確率モデルのみ / claude -p 省略)</span>
              </label>
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={betOddspark}
                  onChange={(e) => setBetOddspark(e.target.checked)}
                  disabled={running}
                  className="accent-(--color-accent)"
                />
                <span>オッズパーク自動投票 (カート投入・要ログイン)</span>
              </label>
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={betIpat}
                  onChange={(e) => setBetIpat(e.target.checked)}
                  disabled={running}
                  className="accent-(--color-accent)"
                />
                <span>JRA 即PAT 自動投票 (カート投入・要ログイン / 土日 JRA 開催日)</span>
              </label>
              {(betOddspark || betIpat) && (
                <p className="basis-full text-xs text-(--color-muted)">
                  {betBundle === "ev" ? (
                    <>
                      投票束は <b>EV束 (recommended_bundle)</b>。シェード込み P×O≥1.02 を通過した
                      脚のみ投票し、大半のレースは見送り。
                      <b className="text-(--color-warn)">Claude 指数ゲートは適用されません</b>
                      (指数なしのレースでも +EV があれば投票されます)。
                    </>
                  ) : (
                    <>
                      投票束は <b>3連単束 (市場無視・既定 回収モード)</b>。
                      <b>Claude 指数が無いレースは自動 skip</b> します (rank_source≠claude は投票しない)。
                    </>
                  )}
                </p>
              )}
            </div>
            {(betOddspark || betIpat) && (
              <>
                <p className="mt-2 text-xs text-(--color-muted)">
                  ⚠ ON で開始すると <b>headful ブラウザが開きます</b>。発走前の束を
                  カート/購入予定リストに投入し続けます。ブラウザを表示するには <code>make api</code> を
                  DISPLAY のある端末 (WSLg 等) で起動しておくこと。
                </p>
                <div className="mt-3">
                  <label className="inline-flex items-center gap-2 cursor-pointer text-sm">
                    <input
                      type="checkbox"
                      checked={betAutoLogin}
                      onChange={(e) => setBetAutoLogin(e.target.checked)}
                      disabled={running}
                      className="accent-(--color-accent)"
                    />
                    <span>自動ログイン — env 認証で自動ログイン (OFF は人が手でログイン)</span>
                  </label>
                  {betAutoLogin && (
                    <p className="mt-1 text-xs text-(--color-muted)">
                      <code>make api</code> を起動する端末の env に
                      {betOddspark && (
                        <>
                          {" "}<code>ODDSPARK_ID</code> / <code>ODDSPARK_PASSWORD</code>
                          {" "}(+ 必要なら <code>ODDSPARK_PIN</code>)
                        </>
                      )}
                      {betOddspark && betIpat && "、"}
                      {betIpat && (
                        <>
                          {" "}<code>IPAT_INETID</code> / <code>IPAT_SUBSCRIBER</code> /{" "}
                          <code>IPAT_PARS</code> / <code>IPAT_PIN</code>
                        </>
                      )}
                      {" "}を設定しておくこと。
                      未設定だと daemon が起動直後に失敗します (ライブログ参照)。認証情報はコミット禁止。
                    </p>
                  )}
                </div>
                <div className="mt-3 flex items-end gap-4 flex-wrap">
                  <label className="inline-flex items-center gap-2 cursor-pointer text-sm">
                    <input
                      type="checkbox"
                      checked={betAutoPurchase}
                      onChange={(e) => setBetAutoPurchase(e.target.checked)}
                      disabled={running}
                      className="accent-(--color-bad)"
                    />
                    <span><b className="text-(--color-bad)">⚠ 自動購入 (実弾)</b> — #gotobuy まで自動でクリック (人の介入なし)</span>
                  </label>
                  <Input
                    label="日次上限 (円)"
                    value={betDailyCap}
                    onChange={(e) => setBetDailyCap(e.target.value)}
                    disabled={running || !betAutoPurchase}
                    className="w-32"
                  />
                  <Input
                    label="掛金倍率 — 3連単束 (×N)"
                    value={betStakeMultiplier}
                    onChange={(e) => setBetStakeMultiplier(e.target.value)}
                    disabled={running}
                    className="w-24"
                  />
                  <div className="flex items-end gap-2">
                    <Input
                      label="per-race 上限倍率 (×N)"
                      placeholder="掛金倍率に連動"
                      value={betMaxStakeMultiplier}
                      onChange={(e) => setBetMaxStakeMultiplier(e.target.value)}
                      disabled={running}
                      className="w-32"
                    />
                    <span className="text-xs text-(--color-muted) pb-1.5">
                      1レース上限 = 基準¥10,000×N。空欄なら掛金倍率に連動。
                    </span>
                  </div>
                  {betOddspark && (
                    <div className="flex flex-col gap-1">
                      <span className="text-[10px] text-(--color-muted) font-bold tracking-wider uppercase">
                        支払方法 (オッズパーク)
                      </span>
                      <div className="flex gap-3 text-sm">
                        <label className="inline-flex items-center gap-1 cursor-pointer">
                          <input
                            type="radio"
                            name="bet_payment_method"
                            checked={betPaymentMethod === "opcoin"}
                            onChange={() => setBetPaymentMethod("opcoin")}
                            disabled={running}
                          />
                          OPコイン
                        </label>
                        <label className="inline-flex items-center gap-1 cursor-pointer">
                          <input
                            type="radio"
                            name="bet_payment_method"
                            checked={betPaymentMethod === "buylimit"}
                            onChange={() => setBetPaymentMethod("buylimit")}
                            disabled={running}
                          />
                          投票資金
                        </label>
                      </div>
                    </div>
                  )}
                </div>
                {betAutoPurchase && (
                  <p className="mt-2 text-xs text-(--color-bad)">
                    🚨 <b>実弾モード</b>: 確定ボタンを自動でクリックし、実際に賭けます。per-race
                    ¥{(() => {
                      const cap = parseFloat(betMaxStakeMultiplier);
                      const stk = parseFloat(betStakeMultiplier);
                      const mult = Number.isFinite(cap) && cap > 0
                        ? cap
                        : Math.max(1, Number.isFinite(stk) && stk > 0 ? stk : 1);
                      return (Math.round((10000 * mult) / 100) * 100).toLocaleString();
                    })()} +
                    日次 ¥{(parseInt(betDailyCap, 10) || 50000).toLocaleString()} を上限としますが、
                    各サービスの利用規約および誤発注の責任は使用者にあります。
                    確認画面の最終ボタン DOM が未検証の間は <code>AUTO_PURCHASE_VERIFIED=False</code> により
                    src 側で fail-safe (実弾は撃たれない)。実機で 1 回検証後に flag を True に。
                  </p>
                )}
              </>
            )}
            {error && <div className="mt-3 text-sm text-(--color-bad)">{error}</div>}
            {running && status?.config?.bet_oddspark && (
              <p className="mt-2 text-xs">
                投票ブラウザ (オッズパーク):{" "}
                {status?.bet_running
                  ? <span className="text-(--color-accent)">稼働中 — ブラウザでログイン後、束がカートに積まれます (確定は人)</span>
                  : <span className="text-(--color-bad)">未起動 — DISPLAY 不在等で daemon が落ちた可能性 (ライブログ参照)</span>}
              </p>
            )}
            {running && status?.config?.bet_ipat && (
              <p className="mt-2 text-xs">
                投票ブラウザ (JRA 即PAT):{" "}
                {status?.ipat_bet_running
                  ? <span className="text-(--color-accent)">稼働中 — ブラウザでログイン後、JRA の束がカートに積まれます (確定は人)</span>
                  : <span className="text-(--color-bad)">未起動 — DISPLAY 不在等で daemon が落ちた可能性 (ライブログ参照)</span>}
              </p>
            )}
            <p className="mt-3 text-xs text-(--color-muted)">
              稼働中はパラメータ変更不可。停止 → 設定変更 → 開始の順で適用。
            </p>
          </>
        ) : (
          <p className="text-xs text-(--color-muted)">
            {running
              ? `稼働中: 考察${status?.config?.score_window ?? 5}分前→投票締切${status?.config?.bet_lead_sec ?? 60}秒前 / ${status?.config?.interval_sec}s / ${status?.config?.active_hours ?? "—"} / 束=${status?.config?.bet_bundle === "ev" ? "EV束" : `3連単/${status?.config?.trifecta_mode === "hit" ? "的中" : "回収(穴狙い)"}`}${status?.config?.bet_oddspark ? (status?.bet_running ? " / 投票ブラウザ稼働中" : " / 投票ブラウザ未起動") : ""}${status?.config?.bet_ipat ? (status?.ipat_bet_running ? " / IPAT稼働中" : " / IPAT未起動") : ""}`
              : "停止中。タイトルをクリックして設定を展開。"}
            {error && <span className="ml-2 text-(--color-bad)">{error}</span>}
          </p>
        )}
      </Card>

      <Card
        title={
          <span className="flex items-center gap-2">
            <span>ライブログ</span>
            {job && <Badge tone={running ? "warn" : "muted"}>{job.status}</Badge>}
          </span>
        }
      >
        {job ? (
          <LogStream key={job.id} url={`/api/watch-auto/stream`} height="h-[50vh]" emptyHint="(ログ待機中...)" />
        ) : (
          <p className="text-sm text-(--color-muted)">未起動。開始ボタンで 自動予測分析・投票 を立ち上げます。</p>
        )}
      </Card>

      {(status?.bet_job || status?.ipat_bet_job) && (
        <Card
          title={
            <span className="flex items-center gap-2">
              <span>投票ブラウザ daemon ログ</span>
              <span className="text-xs text-(--color-muted)">
                (ブラウザ起動 / ログイン待ち / X server エラー等)
              </span>
            </span>
          }
        >
          {status?.bet_job && (
            <div className="mb-4">
              <div className="mb-1 text-xs flex items-center gap-2">
                <span>オッズパーク</span>
                <Badge tone={status.bet_running ? "warn" : "muted"}>{status.bet_job.status}</Badge>
              </div>
              <LogStream
                key={status.bet_job.id}
                url={`/api/jobs/${status.bet_job.id}/stream`}
                height="h-[30vh]"
                emptyHint="(ログ待機中...)"
              />
            </div>
          )}
          {status?.ipat_bet_job && (
            <div>
              <div className="mb-1 text-xs flex items-center gap-2">
                <span>JRA 即PAT</span>
                <Badge tone={status.ipat_bet_running ? "warn" : "muted"}>{status.ipat_bet_job.status}</Badge>
              </div>
              <LogStream
                key={status.ipat_bet_job.id}
                url={`/api/jobs/${status.ipat_bet_job.id}/stream`}
                height="h-[30vh]"
                emptyHint="(ログ待機中...)"
              />
            </div>
          )}
        </Card>
      )}

      <Card
        title="未取得結果 (手動 record)"
      >
        <p className="text-xs text-(--color-muted) mb-3">
          結果取得が <code className="mono">max_attempts</code> 回失敗 (status=failed) したレースは、
          競走除外・降着・大幅な審議などで結果ページが想定構造で取れていない可能性があります。
          馬番を入力して手動で記録してください。
        </p>
        <PendingRecorder />
      </Card>

      <Card
        title="自動予測分析の履歴 (最新3件)"
        right={
          <Link
            href="/predictions"
            className="text-xs text-(--color-accent) hover:underline"
          >
            もっと見る →
          </Link>
        }
      >
        {recentHistory.length === 0 ? (
          <p className="text-sm text-(--color-muted)">まだ履歴がありません。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabnum table-zebra">
              <thead className="text-left text-(--color-muted) text-xs">
                <tr className="border-b border-(--color-line)">
                  <th className="py-2 pr-3">予測分析時刻</th>
                  <th className="py-2 pr-3">会場</th>
                  <th className="py-2 pr-3">R</th>
                  <th className="py-2 pr-3">締切</th>
                  <th className="py-2 pr-3">発走</th>
                  <th className="py-2 pr-3">状態</th>
                  <th className="py-2 pr-3">投票束</th>
                  <th className="py-2 pr-3">詳細</th>
                </tr>
              </thead>
              <tbody>
                {recentHistory.map((h) => {
                  const p = picks[h.race_id];
                  const pickStatus = p === undefined ? "loading" : p === null ? "missing" : "ok";
                  const timing = nowMs !== null
                    ? raceTimingStatus(h.close_at, h.start_at, !!p?.result, nowMs)
                    : null;
                  const rowBg = timing ? raceTimingRowBg(timing.tone) : "";
                  return (
                  <tr key={`${h.race_id}-${h.started_at}`} className={`border-b border-(--color-line)/60 ${rowBg}`}>
                    <td className="py-1.5 pr-3">
                      {fmtTs(h.started_at)}
                    </td>
                    <td className="py-1.5 pr-3">{h.venue}</td>
                    <td className="py-1.5 pr-3">
                      <Link
                        href={`/predictions/${h.race_id}`}
                        className="text-(--color-accent) hover:underline"
                      >
                        {h.race_no}R
                      </Link>
                    </td>
                    <td className="py-1.5 pr-3">
                      {fmtTime(h.close_at)}
                    </td>
                    <td className="py-1.5 pr-3">
                      {fmtTime(h.start_at)}
                    </td>
                    <td className="py-1.5 pr-3">
                      {timing && <Badge tone={timing.tone}>{timing.label}</Badge>}
                    </td>
                    <td className="py-1.5 pr-3 mono text-xs font-semibold text-(--color-good)">
                      {pickStatus === "loading" ? (
                        <span className="text-(--color-muted)">…</span>
                      ) : isEvMeasured(p?.saved_at) ? (
                        // EV束レジーム (2026-06-10〜): 実弾は EV束 (券種混在)。脚を 種別:組 で列挙。
                        (p?.recommended_bundle?.legs?.length ?? 0) > 0
                          ? p!.recommended_bundle!.legs!
                              .map((l) => `${l.bet_type}:${l.key.join("-")}`)
                              .join(" / ")
                          : "見送り"
                      ) : (
                        fmtPicks(p?.recommended_bundle_t?.legs?.map((l) => l.key) ?? [])
                      )}
                    </td>
                    <td className="py-1.5 pr-3">
                      <Link
                        href={`/predictions/${h.race_id}?url=${encodeURIComponent(h.url)}`}
                        className="text-(--color-accent) hover:underline"
                      >
                        詳細 →
                      </Link>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </Page>
  );
}
