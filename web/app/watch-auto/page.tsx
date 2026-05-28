"use client";

import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  Input,
  Page,
  PageHeader,
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
import {
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

  const [windowMin, setWindowMin] = useState("5");
  const [tolerance, setTolerance] = useState("4");
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
  // オッズパーク自動投票 (カート投入)。ON で投票 daemon (headful ブラウザ) が起動し、人がログイン。
  const [betOddspark, setBetOddspark] = useState(false);

  // 前回使った設定 (backend に persist 済 status.config) を form の default に流し込む。
  // status は 5s 間隔で polling されるので、ユーザ編集を上書きしないよう初回 config 到着時に
  // 1 度だけ適用する。config が null (停止中・API 未応答) のときは上の hardcode default のまま。
  // React 公式の「レンダー中に前回値から state 調整」パターン (条件 guard で無限ループ防止)。
  // 初回 config 到着時に 1 度だけ form を前回値で埋める。
  const [prefilled, setPrefilled] = useState(false);
  if (!prefilled && status?.config) {
    const c = status.config;
    setPrefilled(true);
    if (c.window != null) setWindowMin(String(c.window));
    if (c.tolerance != null) setTolerance(String(c.tolerance));
    if (c.interval_sec != null) setIntervalSec(String(c.interval_sec));
    setEvMax(c.ev_max != null ? String(c.ev_max) : "");
    setMinProb(c.min_prob != null ? String(c.min_prob) : "");
    setMarketBlend(c.market_blend != null ? String(c.market_blend) : "");
    if (c.aptitude_top != null) setAptitudeTop(String(c.aptitude_top));
    if (c.active_hours != null) setActiveHours(String(c.active_hours));
    if (c.with_exacta != null) setWithExacta(!!c.with_exacta);
    if (c.with_trio != null) setWithTrio(!!c.with_trio);
    if (c.bet_oddspark != null) setBetOddspark(!!c.bet_oddspark);
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
        window: parseInt(windowMin) || 5,
        tolerance: parseInt(tolerance) || 4,
        interval_sec: parseInt(intervalSec) || 60,
        ev_max: evMax === "" ? null : parseFloat(evMax),
        min_prob: minProb === "" ? null : parseFloat(minProb),
        market_blend: marketBlend === "" ? null : parseFloat(marketBlend),
        aptitude_top: aptitudeTop === "" ? null : parseInt(aptitudeTop, 10),
        active_hours: activeHours.trim() || "09:00-23:45",
        with_exacta: withExacta,
        with_trio: withTrio,
        bet_oddspark: betOddspark,
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
            title="watch-auto"
            subtitle="netkeiba の開催一覧を polling し、発走間際のレースを自動で解析する。"
          />

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="状態"
          value={running ? "稼働中" : "停止"}
          tone={running ? "good" : "default"}
        />
        <Stat
          label="窓 (発走まで)"
          value={
            status?.config?.window != null
              ? `${status.config.window}〜${status.config.window + (status.config.tolerance ?? 0)} 分前`
              : "—"
          }
        />
        <Stat
          label="polling 間隔"
          value={status?.config?.interval_sec ? `${status.config.interval_sec}s` : "—"}
        />
        <Stat label="自動解析 件数" value={history.length} />
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
                label="WINDOW (締切までの目標分)"
                value={windowMin}
                onChange={(e) => setWindowMin(e.target.value)}
                disabled={running}
              />
              <Input
                label="TOLERANCE (+分のみ / 締切 window〜window+tol 分前 = 発走 +2分の lead)"
                value={tolerance}
                onChange={(e) => setTolerance(e.target.value)}
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
                  checked={betOddspark}
                  onChange={(e) => setBetOddspark(e.target.checked)}
                  disabled={running}
                  className="accent-(--color-accent)"
                />
                <span>オッズパーク自動投票 (カート投入・要ログイン)</span>
              </label>
            </div>
            {betOddspark && (
              <p className="mt-2 text-xs text-(--color-muted)">
                ⚠ ON で開始すると <b>headful ブラウザが開きます</b>(人がログイン)。発走前 NAR の束を
                カートに投入し続けますが、<b>購入確定は常に人が目視で押します</b>(自動では押しません)。
                ブラウザを表示するには <code>make api</code> を DISPLAY のある端末 (WSLg 等) で起動しておくこと。
              </p>
            )}
            {error && <div className="mt-3 text-sm text-(--color-bad)">{error}</div>}
            {running && status?.config?.bet_oddspark && (
              <p className="mt-2 text-xs">
                投票ブラウザ:{" "}
                {status?.bet_running
                  ? <span className="text-(--color-accent)">稼働中 — ブラウザでログイン後、束がカートに積まれます (確定は人)</span>
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
              ? `稼働中: 発走${status?.config?.window}〜${(status?.config?.window ?? 0) + (status?.config?.tolerance ?? 0)}分前 / ${status?.config?.interval_sec}s / ${status?.config?.active_hours ?? "—"}${status?.config?.bet_oddspark ? (status?.bet_running ? " / 投票ブラウザ稼働中" : " / 投票ブラウザ未起動") : ""}`
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
          <p className="text-sm text-(--color-muted)">未起動。開始ボタンで watch-auto を立ち上げます。</p>
        )}
      </Card>

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
        title="自動解析の履歴 (最新3件)"
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
                  <th className="py-2 pr-3">解析時刻</th>
                  <th className="py-2 pr-3">会場</th>
                  <th className="py-2 pr-3">R</th>
                  <th className="py-2 pr-3">締切</th>
                  <th className="py-2 pr-3">発走</th>
                  <th className="py-2 pr-3">状態</th>
                  <th className={`py-2 pr-3 ${planAccentClass("A")}`}>Plan A 買い目</th>
                  <th className={`py-2 pr-3 ${planAccentClass("B")}`}>Plan B</th>
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
                    <td className={`py-1.5 pr-3 mono text-xs font-semibold ${planAccentClass("A")}`}>
                      {pickStatus === "loading" ? (
                        <span className="text-(--color-muted)">…</span>
                      ) : (
                        fmtPicks(p?.plan_a_keys)
                      )}
                    </td>
                    <td className={`py-1.5 pr-3 mono text-xs ${planAccentClass("B")}`}>
                      {pickStatus === "loading" ? "…" : fmtPicks(p?.plan_b_keys)}
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
