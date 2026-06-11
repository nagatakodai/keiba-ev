"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CircleCheck, CircleX, Clock, RefreshCw, Trash2 } from "lucide-react";
import { api, type PendingItem, type PendingSummary } from "@/lib/api";
import { Badge, Button, fmtTs } from "@/components/ui";

// 新フォーマット ("2026052075-1-10") なら予測詳細に飛ばす。旧 winticket_race_id
// ("107520260520" 等の 12 桁数字) は飛ばし先が無いのでテキストのまま。
function looksLikeInternalRaceId(s: string): boolean {
  return /^\d+-\d+-\d+$/.test(s);
}

// 状態の色: success = emerald / failed = rose系 (bad) / pending = amber (warn)。
function statusTone(s: PendingItem["status"]): "good" | "warn" | "bad" | "muted" {
  if (s === "success") return "good";
  if (s === "failed") return "bad";
  if (s === "pending") return "warn";
  return "muted";
}

function StatusBadge({ s }: { s: PendingItem["status"] }) {
  const Icon = s === "success" ? CircleCheck : s === "failed" ? CircleX : Clock;
  return (
    <Badge tone={statusTone(s)}>
      <span className="inline-flex items-center gap-1">
        <Icon size={11} aria-hidden />
        {s}
      </span>
    </Badge>
  );
}

// 行全体にも状態色のごく薄い tint を敷く (zebra より優先される)。
function statusRowBg(s: PendingItem["status"]): string {
  if (s === "success") return "bg-emerald-500/[0.06]";
  if (s === "failed") return "bg-rose-500/[0.06]";
  if (s === "pending") return "bg-amber-500/[0.05]";
  return "";
}

function fmtCountdown(s: number): string {
  if (s <= 0) return "再試行中…";
  if (s < 60) return `${s}秒後`;
  const m = Math.ceil(s / 60);
  return `${m}分後`;
}

function RecorderRow({
  item,
  onDone,
  onDelete,
}: {
  item: PendingItem;
  onDone: () => void;
  onDelete: (raceId: string) => Promise<void>;
}) {
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [c, setC] = useState("");
  const [payout, setPayout] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setErr(null);
    setOk(null);
    try {
      const finish = [a, b, c].map((s) => parseInt(s, 10));
      if (finish.some((n) => !Number.isInteger(n) || n < 1 || n > 18)) {
        throw new Error("馬番は 1〜18 を 3 つ入力してください");
      }
      if (new Set(finish).size !== 3) {
        throw new Error("3 着順は重複なしで指定してください");
      }
      const res = await api.recordResult({
        race_id: item.race_id,
        finish_order: finish,
        trifecta_payout: payout ? parseInt(payout, 10) : 0,
        note: note || undefined,
      });
      setOk(
        res.matched
          ? "記録 + 予測と突合済み"
          : "記録しました (予測分析履歴なし)",
      );
      setTimeout(onDone, 600);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`pending queue から ${item.race_id} を除外します。よろしいですか?`)) return;
    setDeleting(true);
    try {
      await onDelete(item.race_id);
    } finally {
      setDeleting(false);
    }
  };

  const inputCls =
    "w-14 text-center bg-(--color-surface-2) border border-(--color-line) rounded-lg px-1 py-1 text-sm placeholder:text-(--color-muted)/70 transition-colors focus:outline-none focus:border-(--color-accent) focus:ring-2 focus:ring-(--color-ring)/40 tnum";
  const wideInputCls =
    "bg-(--color-surface-2) border border-(--color-line) rounded-lg px-2 py-1 text-sm placeholder:text-(--color-muted)/70 transition-colors focus:outline-none focus:border-(--color-accent) focus:ring-2 focus:ring-(--color-ring)/40";

  const internalId = looksLikeInternalRaceId(item.race_id);

  return (
    <tr className={`border-b border-(--color-line)/60 align-top ${statusRowBg(item.status)}`}>
      <td className="py-2 pr-3 mono text-xs">
        {internalId ? (
          <Link
            href={`/predictions/${item.race_id}`}
            className="text-(--color-accent) hover:underline"
          >
            {item.race_id}
          </Link>
        ) : (
          item.race_id
        )}
      </td>
      <td className="py-2 pr-3">
        <StatusBadge s={item.status} />
      </td>
      <td className="py-2 pr-3 text-xs tnum">
        {item.attempts}/{item.max_attempts}
      </td>
      <td className="py-2 pr-3 text-xs tnum">
        {item.status === "pending" ? (
          <span className="inline-flex items-center gap-1 text-amber-300">
            <Clock size={11} aria-hidden />
            {fmtCountdown(item.seconds_until_next)}
          </span>
        ) : item.status === "failed" ? (
          <span className="text-(--color-muted)">— (再試行終了)</span>
        ) : (
          <span className="text-(--color-muted)">{fmtTs(item.due_at)}</span>
        )}
      </td>
      <td
        className="py-2 pr-3 text-xs text-(--color-muted) max-w-[28ch]"
        title={item.last_error || ""}
      >
        {item.last_error ? (
          <span className="break-words whitespace-normal leading-snug">
            {item.last_error}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="py-2 pr-3">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-1">
            <input
              className={inputCls}
              placeholder="1着"
              value={a}
              onChange={(e) => setA(e.target.value)}
              inputMode="numeric"
              maxLength={2}
            />
            <span className="text-(--color-muted)">-</span>
            <input
              className={inputCls}
              placeholder="2着"
              value={b}
              onChange={(e) => setB(e.target.value)}
              inputMode="numeric"
              maxLength={2}
            />
            <span className="text-(--color-muted)">-</span>
            <input
              className={inputCls}
              placeholder="3着"
              value={c}
              onChange={(e) => setC(e.target.value)}
              inputMode="numeric"
              maxLength={2}
            />
          </div>
          <input
            className={`w-24 ${wideInputCls} tnum`}
            placeholder="払戻¥"
            value={payout}
            onChange={(e) => setPayout(e.target.value)}
            inputMode="numeric"
          />
          <input
            className={`w-32 ${wideInputCls}`}
            placeholder="note (任意)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <Button size="sm" onClick={submit} disabled={busy}>
            {busy ? "記録中..." : "記録"}
          </Button>
          {item.status === "failed" && (
            <Button
              size="sm"
              variant="ghost"
              onClick={handleDelete}
              disabled={deleting}
              title="pending queue から除外 (calibration からは元々除外済)"
            >
              {deleting ? (
                "..."
              ) : (
                <>
                  <Trash2 size={12} aria-hidden />
                  除外
                </>
              )}
            </Button>
          )}
        </div>
        {err && (
          <div className="text-xs text-(--color-bad) mt-1 inline-flex items-center gap-1">
            <CircleX size={11} aria-hidden />
            {err}
          </div>
        )}
        {ok && (
          <div className="text-xs text-(--color-good) mt-1 inline-flex items-center gap-1">
            <CircleCheck size={11} aria-hidden />
            {ok}
          </div>
        )}
      </td>
    </tr>
  );
}

export function PendingRecorder() {
  const [items, setItems] = useState<PendingItem[] | null>(null);
  const [summary, setSummary] = useState<PendingSummary | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  const refresh = async () => {
    try {
      const res = await api.listPending();
      setItems(res.items);
      setSummary(res.summary);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  const handleDelete = async (raceId: string) => {
    try {
      await api.deletePending(raceId);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleBulkDelete = async () => {
    if (!items) return;
    const failed = items.filter((i) => i.status === "failed");
    if (failed.length === 0) return;
    if (!confirm(`failed エントリ ${failed.length} 件を pending queue から一括除外します。よろしいですか?`)) return;
    setBulkBusy(true);
    try {
      // 並列だと backend が race condition を起こす可能性があるので順次。
      for (const it of failed) {
        await api.deletePending(it.race_id).catch(() => {});
      }
      await refresh();
    } finally {
      setBulkBusy(false);
    }
  };

  if (error) {
    return (
      <p className="text-sm text-(--color-bad)">
        pending 取得失敗: {error}
      </p>
    );
  }

  if (items === null) {
    return <p className="text-sm text-(--color-muted)">読み込み中...</p>;
  }

  const failed = items.filter((i) => i.status === "failed");
  const displayed = showAll ? items : failed;

  return (
    <div>
      <div className="flex items-center gap-2 mb-3 text-xs flex-wrap">
        {summary && (
          <>
            <Badge tone="bad">
              <span className="inline-flex items-center gap-1 tnum">
                <CircleX size={11} aria-hidden />
                失敗 {summary.failed}
              </span>
            </Badge>
            <Badge tone="warn">
              <span className="inline-flex items-center gap-1 tnum">
                <Clock size={11} aria-hidden />
                取得待ち {summary.pending}
              </span>
            </Badge>
            <Badge tone="good">
              <span className="inline-flex items-center gap-1 tnum">
                <CircleCheck size={11} aria-hidden />
                完了 {summary.success}
              </span>
            </Badge>
            <span className="text-(--color-muted) tnum">/ 全 {summary.total}</span>
          </>
        )}
        <button
          className="text-(--color-accent) hover:underline ml-2"
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? "失敗のみ表示" : "すべて表示"}
        </button>
        {failed.length > 0 && (
          <button
            className="inline-flex items-center gap-1 text-(--color-bad) hover:underline disabled:opacity-50"
            onClick={handleBulkDelete}
            disabled={bulkBusy}
          >
            <Trash2 size={11} aria-hidden />
            {bulkBusy ? "除外中..." : `failed をまとめて除外 (${failed.length})`}
          </button>
        )}
        <button
          className="inline-flex items-center gap-1 text-(--color-accent) hover:underline ml-auto"
          onClick={refresh}
        >
          <RefreshCw size={11} aria-hidden />
          再読込
        </button>
      </div>
      {displayed.length === 0 ? (
        <p className="text-sm text-(--color-muted)">
          {showAll ? "pending なし。" : "失敗 pending なし。"}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm table-zebra">
            <thead className="text-left text-(--color-muted) text-[11px] uppercase tracking-wider">
              <tr className="border-b border-(--color-line)">
                <th className="py-2 pr-3 font-bold">race_id</th>
                <th className="py-2 pr-3 font-bold">状態</th>
                <th className="py-2 pr-3 font-bold">試行</th>
                <th className="py-2 pr-3 font-bold">次の試行</th>
                <th className="py-2 pr-3 font-bold">last_error</th>
                <th className="py-2 pr-3 font-bold">手動 record / 除外</th>
              </tr>
            </thead>
            <tbody>
              {displayed.map((it) => (
                <RecorderRow
                  key={it.race_id}
                  item={it}
                  onDone={refresh}
                  onDelete={handleDelete}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
