import type { Metadata } from "next";
import Link from "next/link";
import { api } from "@/lib/api";
import {
  Badge,
  Card,
  Page,
  PageHeader,
  Stat,
  calibrationConfidence,
  fmtPct,
  fmtRelativeFromNow,
  fmtYen,
  tierLabel,
  tierTone,
} from "@/components/ui";

export const metadata: Metadata = { title: "確率較正" };

export const dynamic = "force-dynamic";

export default async function CalibratePage({
  searchParams,
}: {
  searchParams: Promise<{ point_cost?: string }>;
}) {
  const sp = await searchParams;
  const pointCost = sp.point_cost ? parseInt(sp.point_cost) : 100;
  const cal = await api.calibrate(pointCost).catch(() => null);

  if (!cal) {
    return (
      <Page>
        <PageHeader title="確率較正" />
        <Card>
          <p className="text-sm text-(--color-bad)">FastAPI に到達できませんでした。</p>
        </Card>
      </Page>
    );
  }

  const confidence = calibrationConfidence(cal.race_count);
  const lastUpdated = fmtRelativeFromNow(cal.last_updated_at);
  const tb = cal.trifecta_bundle;       // 3連単的中モード (実弾投票束, 2026-06-06〜固定)
  const yb = cal.claude_bundle;       // EV束 (モデル参考・投票しない)
  // 回収率は **最終オッズ基準** (roi_final)。最終が無い旧 result は roi に fallback。
  const roiPct = (b?: { roi: number; roi_final?: number } | null) =>
    b ? `${Math.round((b.roi_final ?? b.roi) * 100)}%` : "—";
  // 回収率: 100% 未満 → 赤文字 (損失)、100% 以上 → 黒 (default)
  const roiTone = (b?: { participated_races: number; roi: number; roi_final?: number } | null) =>
    !b || b.participated_races === 0 ? "default" : (b.roi_final ?? b.roi) >= 1 ? "default" : "bad";
  // 的中率: 30% 未満 → 赤文字、30% 以上 → 黒 (default)
  const hitTone = (b?: { participated_races: number; hit_rate: number } | null) =>
    !b || b.participated_races === 0 ? "default" : b.hit_rate < 0.3 ? "bad" : "default";

  return (
    <Page>
      <PageHeader
        title="確率較正"
        subtitle="計算 EV と実 EV のオフセット (tier ratio) + 3連単束 (実弾) / EV束 (参考) bundle の実績。サンプル 30+ で初めて判断材料になる。"
      />

      <div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <Stat
            label="総合レース数"
            value={cal.race_count}
            hint={
              tb
                ? `参加 ${tb.participated_races} / 見送り ${tb.skipped_races}`
                : "—"
            }
            accentTone="muted"
          />
          <Stat
            label="的中率"
            value={!tb || tb.participated_races === 0 ? "—" : fmtPct(tb.hit_rate, 1)}
            hint={tb && tb.participated_races > 0 ? `${tb.hits} 的中 / ${tb.participated_races} 参加` : "—"}
            tone={hitTone(tb)}
            accentTone="magenta"
          />
          <Stat
            label="回収率"
            value={roiPct(tb)}
            hint={
              tb && tb.participated_races > 0
                ? `賭金 ${fmtYen(tb.stake)} → 払戻(最終) ${fmtYen(tb.payout_final ?? tb.payout)} · 見送り ${tb.skipped_races}`
                : "賭けたレースなし"
            }
            tone={roiTone(tb)}
            accentTone="magenta"
          />
        </div>
        <div className="text-[10px] text-(--color-muted) text-right mt-1 px-1">
          ※ 全て 3連単的中モード (実弾投票束) 基準
          {yb && yb.participated_races > 0 && (
            <span className="ml-1">
              ／ EV束 (参考): 的中率 {fmtPct(yb.hit_rate, 1)} · 回収率 {roiPct(yb)}
            </span>
          )}
          ／ 集計対象 {cal.race_count} レース ／
          <span className="ml-1">
            <Badge tone={confidence.tone}>{confidence.label}</Badge>
          </span>
          <span className="ml-1">最終更新 {lastUpdated}</span>
        </div>
      </div>

      <Card title="Tier 別 (ratio = 実hit / 予測P合計)">
        {cal.tiers.length === 0 ? (
          <p className="text-sm text-(--color-muted)">データなし。`make record` で結果を登録してください。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabnum table-zebra">
              <thead className="text-left text-(--color-muted) text-xs">
                <tr className="border-b border-(--color-line)">
                  <th className="py-2 pr-3">Tier</th>
                  <th className="py-2 pr-3 text-right">予測 rows</th>
                  <th className="py-2 pr-3 text-right">予測 P 合計</th>
                  <th className="py-2 pr-3 text-right">実 hit</th>
                  <th className="py-2 pr-3 text-right">ratio</th>
                  <th className="py-2 pr-3">解釈</th>
                </tr>
              </thead>
              <tbody>
                {cal.tiers.map((t) => (
                  <tr key={t.tier} className="border-b border-(--color-line)/60">
                    <td className="py-1.5 pr-3">
                      <Badge tone={tierTone(t.tier)}>{tierLabel(t.tier)}</Badge>
                    </td>
                    <td className="py-1.5 pr-3 text-right">{t.rows}</td>
                    <td className="py-1.5 pr-3 text-right">{t.prob_sum.toFixed(3)}</td>
                    <td className="py-1.5 pr-3 text-right">{t.hits}</td>
                    <td className="py-1.5 pr-3 text-right">
                      {t.prob_sum > 0 ? `${t.ratio.toFixed(2)}×` : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-xs">{interpret(t.hits, t.ratio)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="レース毎の 的中/不的中">
        {cal.races.length === 0 ? (
          <p className="text-sm text-(--color-muted)">データなし。</p>
        ) : (
          // **2 列レイアウト** (2026-05-29 ユーザ指示): 1 race = 1 row card、grid で 2 列。
          // 旧 table 形式は info 密度が低いため。
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-1.5">
            {cal.races.map((r) => {
              // 「的中」ラベルは **3連単束 (実弾投票束) のみ**で判定 (2026-06-06 特化)。
              // 3連単束が無い旧 snapshot は旧実弾だった EV束 (bundle_hit) に fallback。
              // 見送り (束が空) は理論値が立っていても的中扱いせず、row 自体をグレー bg に。
              const useTrifecta = !!r.trifecta_bundle_participated;
              const bundleSkipped = !useTrifecta && r.bundle_participated === false;
              const anyHit = !bundleSkipped && !!(useTrifecta ? r.trifecta_bundle_hit : r.bundle_hit);
              const rowBg = bundleSkipped
                ? "bg-(--color-panel-2)"          // 見送り = グレー系
                : anyHit
                  ? "bg-(--color-good)/5"        // 的中 (3連単束) = 緑薄
                  : "";                           // 不的中 = 通常
              return (
                <Link
                  key={r.race_id}
                  href={`/predictions/${r.race_id}`}
                  className={`flex items-center gap-2 text-sm tabnum border border-(--color-line) px-2.5 py-1.5 hover:bg-(--color-panel-2) ${rowBg}`}
                >
                  <span className="whitespace-nowrap font-bold text-xs w-12 truncate">{r.venue}</span>
                  <span className="mono whitespace-nowrap text-xs w-16 text-(--color-muted)">{r.finish.join("-")}</span>
                  {r.winning_tier ? (
                    <Badge tone={tierTone(r.winning_tier)}>{tierLabel(r.winning_tier)}</Badge>
                  ) : (
                    <span className="w-10 text-(--color-muted) text-xs">—</span>
                  )}
                  <span className="whitespace-nowrap text-xs text-right flex-1 mono">
                    {r.payout ? fmtYen(r.payout) : ""}
                  </span>
                  <div className="flex gap-1 flex-wrap items-center shrink-0">
                    {bundleSkipped ? (
                      <Badge tone="muted">見送り</Badge>
                    ) : anyHit ? (
                      <Badge tone="magenta">{useTrifecta ? "3連単束 的中" : "的中 (旧EV束)"}</Badge>
                    ) : (
                      <Badge tone="muted">不的中</Badge>
                    )}
                    {useTrifecta && r.bundle_hit && (
                      <Badge tone="muted">EV束(参考)</Badge>
                    )}
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </Card>

      <p className="text-xs text-(--color-muted)">
        ratio &lt; 0.7 → モデルが過大予測 (係数下げ検討) ／ ratio &gt; 1.3 → 過小予測 (機会が滞留)。1.0 付近で整合。
      </p>
    </Page>
  );
}

function interpret(hits: number, ratio: number): string {
  if (hits < 3) return "サンプル不足";
  if (ratio < 0.7) return "過大予測 (削減候補)";
  if (ratio < 0.85) return "やや過大";
  if (ratio < 1.15) return "ほぼ整合";
  if (ratio < 1.3) return "やや過小";
  return "過小 (機会)";
}
