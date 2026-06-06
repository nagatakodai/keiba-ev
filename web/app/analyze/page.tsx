"use client";

import { useState } from "react";
import { Badge, Button, Card, Input, Page, PageHeader, Select } from "@/components/ui";
import { LogStream } from "@/components/LogStream";
import { api, type JobInfo } from "@/lib/api";

export default function AnalyzePage() {
  const [url, setUrl] = useState("");
  const [refresh, setRefresh] = useState(true);
  const [llmModel, setLlmModel] = useState("opus");
  const [noLlm, setNoLlm] = useState(false);
  const [evMax, setEvMax] = useState("");
  const [minProb, setMinProb] = useState("2.0");
  // 空 = backend BLEND_DEFAULT (=0.78) を使う。CLAUDE.md の production 設定。
  const [marketBlend, setMarketBlend] = useState("");
  const [aptitudeTop, setAptitudeTop] = useState("6");
  const [withExacta, setWithExacta] = useState(false);
  const [withTrio, setWithTrio] = useState(false);

  const [job, setJob] = useState<JobInfo | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const j = await api.analyze({
        url,
        refresh,
        no_llm: noLlm,
        llm_model: llmModel,
        ev_max: evMax === "" ? null : parseFloat(evMax),
        min_prob: minProb === "" ? null : parseFloat(minProb),
        market_blend: marketBlend === "" ? null : parseFloat(marketBlend),
        aptitude_top: aptitudeTop === "" ? null : parseInt(aptitudeTop, 10),
        with_exacta: withExacta,
        with_trio: withTrio,
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

  return (
    <Page>
      <PageHeader
        title="レース予測分析"
        subtitle="URL を渡して 3連単的中モード (実弾投票束) と EV束 (モデル参考) を生成。refresh で発走 5 分前まで待機し最新オッズで再分析。"
      />

      <Card title="リクエスト">
        <form onSubmit={submit} className="space-y-3">
          <Input
            label="URL"
            placeholder="https://race.netkeiba.com/race/shutuba.html?race_id=… (JRA) または https://nar.netkeiba.com/race/shutuba.html?race_id=… (地方)"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Select label="LLM モデル" value={llmModel} onChange={(e) => setLlmModel(e.target.value)}>
              <option value="opus">opus (default)</option>
              <option value="sonnet">sonnet</option>
              <option value="haiku">haiku</option>
            </Select>
            <Input
              label="期待値上限"
              placeholder="例: 3"
              value={evMax}
              onChange={(e) => setEvMax(e.target.value)}
              inputMode="decimal"
            />
            <Input
              label="最低当選率 (%)"
              placeholder="例: 2.0"
              value={minProb}
              onChange={(e) => setMinProb(e.target.value)}
              inputMode="decimal"
            />
            <Input
              label="市場確率ブレンド"
              placeholder="0.0–1.0 (デフォルト 0.4)"
              value={marketBlend}
              onChange={(e) => setMarketBlend(e.target.value)}
              inputMode="decimal"
            />
            <Input
              label="Plan G 適性 top N 頭"
              placeholder="6 (default)"
              value={aptitudeTop}
              onChange={(e) => setAptitudeTop(e.target.value)}
              inputMode="numeric"
            />
          </div>

          <div className="flex items-center gap-4 text-sm flex-wrap">
            <label className="inline-flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={refresh}
                onChange={(e) => setRefresh(e.target.checked)}
                className="accent-(--color-accent)"
              />
              <span>refresh (締切 5 分前まで待機して再取得)</span>
            </label>
            <label className="inline-flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={noLlm}
                onChange={(e) => setNoLlm(e.target.checked)}
                className="accent-(--color-accent)"
              />
              <span>LLM をスキップ</span>
            </label>
            <label className="inline-flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={withExacta}
                onChange={(e) => setWithExacta(e.target.checked)}
                className="accent-(--color-accent)"
              />
              <span>馬単も取得 (fetch +40s)</span>
            </label>
            <label className="inline-flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={withTrio}
                onChange={(e) => setWithTrio(e.target.checked)}
                className="accent-(--color-accent)"
              />
              <span>3 連複も取得 (fetch +40s)</span>
            </label>
          </div>

          {error && <div className="text-sm text-(--color-bad)">{error}</div>}

          <div className="flex items-center gap-2">
            <Button type="submit" disabled={submitting || !url}>
              {submitting ? "起動中..." : job ? "別ジョブで再実行" : "予測分析を開始"}
            </Button>
            {job && (
              <Button type="button" variant="ghost" onClick={cancel} disabled={job.status !== "running"}>
                中断
              </Button>
            )}
          </div>
        </form>
      </Card>

      {job && (
        <Card
          title={
            <span className="flex items-center gap-2">
              <span>ジョブログ</span>
              <Badge tone={statusTone(job.status)}>{job.status}</Badge>
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

function statusTone(s: JobInfo["status"]): "good" | "warn" | "bad" | "default" | "muted" {
  if (s === "running" || s === "pending") return "warn";
  if (s === "done") return "good";
  if (s === "failed") return "bad";
  if (s === "cancelled") return "muted";
  return "default";
}
