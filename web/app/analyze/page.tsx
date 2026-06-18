"use client";

import { useState } from "react";
import {
  AlertTriangle,
  FlaskConical,
  Link2,
  Loader2,
  OctagonX,
  Play,
  SquareTerminal,
} from "lucide-react";
import { Badge, Button, Card, Input, Page, PageHeader, Select } from "@/components/ui";
import { LogStream } from "@/components/LogStream";
import { api, type JobInfo } from "@/lib/api";

export default function AnalyzePage() {
  const [url, setUrl] = useState("");
  const [refresh, setRefresh] = useState(true);
  const [llmModel, setLlmModel] = useState("opus");
  // score 検索チューニング (このタブ専用・per-job env で analyze に渡る)。
  const [scoreParallel, setScoreParallel] = useState(false);
  const [scoreQueries, setScoreQueries] = useState("6"); // 1馬あたり検索クエリ数 (上限/回数, 並列時のみ)
  const [scoreTimeout, setScoreTimeout] = useState("900"); // score 締切=タイムアウト秒

  const [job, setJob] = useState<JobInfo | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      // このタブは score (Claude 指数生成) のみ。束選定・実弾投票は行わない。
      const j = await api.analyze({
        url,
        refresh,
        llm_model: llmModel,
        phase: "score",
        score_parallel: scoreParallel,
        // 検索クエリ数/馬 (上限・回数)。範囲外/NaN は既定 6。
        score_queries_per_horse: (() => {
          const v = parseInt(scoreQueries, 10);
          return Number.isFinite(v) && v >= 2 && v <= 12 ? v : 6;
        })(),
        // score 締切 (秒)。範囲外/NaN は既定 900。
        score_timeout: (() => {
          const v = parseInt(scoreTimeout, 10);
          return Number.isFinite(v) && v >= 60 && v <= 1800 ? v : 900;
        })(),
      });
      setJob(j);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      const j = await api.cancelJob(job.id);
      setJob(j);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const jobActive = job != null && (job.status === "running" || job.status === "pending");

  return (
    <Page>
      <PageHeader
        eyebrow="Score"
        title="レース予測分析"
        subtitle="URL を渡して各馬の Claude 強さ指数 (0-100) のみを生成し、暫定スナップショットとして履歴に保存します。束選定・実弾投票は行いません (それは自動予測分析・投票が締切直前に実施)。refresh で発走 5 分前まで待機し最新オッズで再取得。"
      />

      <Card
        title={
          <span className="flex items-center gap-1.5">
            <FlaskConical className="w-4 h-4 text-(--color-accent)" />
            <span>リクエスト (Score のみ)</span>
          </span>
        }
      >
        <form onSubmit={submit} className="space-y-5">
          {/* ---- 対象レース -------------------------------------------------- */}
          <div className="space-y-2">
            <div className="text-[10px] font-bold uppercase tracking-widest text-(--color-muted)">
              対象レース
            </div>
            <label className="block">
              <span className="block text-xs text-(--color-muted) font-medium mb-1">URL</span>
              <div className="relative">
                <Link2 className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-(--color-muted)" />
                <input
                  required
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://race.netkeiba.com/race/shutuba.html?race_id=… (JRA) または https://nar.netkeiba.com/race/shutuba.html?race_id=… (地方)"
                  className="w-full mono text-sm bg-(--color-surface-2) border border-(--color-line) rounded-lg pl-9 pr-3 py-2.5 placeholder:text-(--color-muted)/60 transition-colors focus:outline-none focus:border-(--color-accent) focus:ring-2 focus:ring-(--color-ring)/40"
                />
              </div>
              <span className="block text-xs text-(--color-muted) mt-1">
                netkeiba の出馬表 URL (race.netkeiba.com = JRA / nar.netkeiba.com = 地方)
              </span>
            </label>
          </div>

          {/* ---- パラメータ -------------------------------------------------- */}
          <div className="space-y-2">
            <div className="text-[10px] font-bold uppercase tracking-widest text-(--color-muted)">
              パラメータ
            </div>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
              <Select
                label="LLM モデル"
                hint="score 指数生成に使う claude -p モデル"
                value={llmModel}
                onChange={(e) => setLlmModel(e.target.value)}
              >
                <option value="opus">opus (default)</option>
                <option value="sonnet">sonnet</option>
                <option value="haiku">haiku</option>
              </Select>
              <Input
                label="検索クエリ数/馬 (上限・回数)"
                type="number"
                min="2"
                max="12"
                step="1"
                hint="並列実行 ON のときのみ有効 (既定6)"
                value={scoreQueries}
                onChange={(e) => setScoreQueries(e.target.value)}
                disabled={!scoreParallel}
                className="tnum"
              />
              <Input
                label="締切 (秒)"
                type="number"
                min="60"
                max="1800"
                step="30"
                hint="score をこの秒数で打ち切り (既定900・並列の60%が検索)"
                value={scoreTimeout}
                onChange={(e) => setScoreTimeout(e.target.value)}
                className="tnum"
              />
            </div>
          </div>

          {/* ---- オプション -------------------------------------------------- */}
          <div className="space-y-2">
            <div className="text-[10px] font-bold uppercase tracking-widest text-(--color-muted)">
              オプション
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <FlagChip
                checked={refresh}
                onChange={setRefresh}
                label="refresh (締切 5 分前まで待機して再取得)"
              />
              <FlagChip
                checked={scoreParallel}
                onChange={setScoreParallel}
                label="検索を並列実行 (検索回数を増やすなら推奨)"
              />
            </div>
          </div>

          {error && (
            <div className="flex items-start gap-2 text-sm text-rose-300 bg-rose-500/10 border border-rose-500/40 rounded-lg px-3 py-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span className="break-all">{error}</span>
            </div>
          )}

          <div className="flex items-center gap-2 pt-1 border-t border-(--color-line-soft)">
            <Button type="submit" size="lg" disabled={submitting || !url}>
              {submitting ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Play className="w-4 h-4" />
              )}
              {submitting ? "起動中..." : job ? "別ジョブで再実行" : "Score (Claude 指数) を生成"}
            </Button>
            {job && (
              <Button
                type="button"
                variant="ghost"
                onClick={cancel}
                disabled={job.status !== "running"}
              >
                <OctagonX className="w-4 h-4" />
                中断
              </Button>
            )}
            <span className="text-xs text-(--color-muted) ml-auto hidden sm:block">
              ジョブはバックグラウンドで実行され、ログが下に流れます
            </span>
          </div>
        </form>
      </Card>

      {job && (
        <Card
          tone={jobActive ? "active" : "default"}
          title={
            <span className="flex items-center gap-2">
              <SquareTerminal className="w-4 h-4 text-(--color-info)" />
              <span>ジョブログ</span>
              <Badge tone={statusTone(job.status)}>{job.status}</Badge>
              {jobActive && <Loader2 className="w-3.5 h-3.5 animate-spin text-sky-300" />}
              <span className="text-xs text-(--color-muted) mono">{job.id.slice(0, 8)}</span>
            </span>
          }
          right={<span className="text-xs text-(--color-muted) mono">{job.label}</span>}
        >
          <LogStream key={job.id} url={`/api/jobs/${job.id}/stream`} />
        </Card>
      )}
    </Page>
  );
}

// チェックボックスを「選択中が一目で分かる」チップ型のウェルにする。
// has-[:checked] で選択時は emerald tint (dark 用 alpha)。
function FlagChip({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="inline-flex items-center gap-2 cursor-pointer select-none text-xs bg-(--color-surface-2) border border-(--color-line) rounded-lg px-3 py-2 transition-colors hover:border-white/25 has-[:checked]:bg-emerald-500/10 has-[:checked]:border-emerald-500/40 has-[:checked]:text-emerald-200">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-(--color-accent)"
      />
      <span>{label}</span>
    </label>
  );
}

function statusTone(s: JobInfo["status"]): "good" | "warn" | "bad" | "default" | "muted" {
  if (s === "running" || s === "pending") return "warn";
  if (s === "done") return "good";
  if (s === "failed") return "bad";
  if (s === "cancelled") return "muted";
  return "default";
}
