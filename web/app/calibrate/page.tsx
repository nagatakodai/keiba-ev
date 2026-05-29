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
  const yb = cal.claude_bundle;       // 回収優先 (実弾で買う)
  const hb = cal.claude_bundle_hit;   // 的中優先 (おまけ計測)
  const roiPct = (b?: { roi: number } | null) =>
    b ? `${Math.round(b.roi * 100)}%` : "—";
  // 100% 超 → 黒 (default)、未満 → 赤 (損失) (dashboard と同じ閾値)
  const roiTone = (b?: { participated_races: number; roi: number } | null) =>
    !b || b.participated_races === 0 ? "default" : b.roi > 1 ? "default" : "bad";

  return (
    <Page>
      <PageHeader
        title="確率較正"
        subtitle="計算 EV と実 EV のオフセット (tier ratio) + 回収優先 / 的中優先 bundle の実績。サンプル 30+ で初めて判断材料になる。"
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="対象レース"
          value={cal.race_count}
          hint={
            <span className="flex items-center gap-1">
              <Badge tone={confidence.tone}>{confidence.label}</Badge>
              <span>· 最終更新 {lastUpdated}</span>
            </span>
          }
          tone={confidence.tone === "good" ? "good" : confidence.tone === "warn" ? "warn" : "bad"}
        />
        <Stat
          label="回収優先AI 的中率"
          value={!yb || yb.participated_races === 0 ? "—" : fmtPct(yb.hit_rate, 1)}
          hint={yb && yb.participated_races > 0 ? `${yb.hits} 的中 / ${yb.participated_races} 参加` : "—"}
          accentTone="good"
        />
        <Stat
          label="回収優先AI 回収率"
          value={roiPct(yb)}
          hint={
            yb && yb.participated_races > 0
              ? `賭金 ${fmtYen(yb.stake)} → 払戻 ${fmtYen(yb.payout)} · 見送り ${yb.skipped_races}`
              : "賭けたレースなし"
          }
          tone={roiTone(yb)}
          accentTone="info"
        />
        <Stat
          label="的中優先AI 回収率"
          value={roiPct(hb)}
          hint={
            hb && hb.participated_races > 0
              ? `${hb.hits} 的中 / ${hb.participated_races} 参加 (おまけ計測)`
              : "新スキーマ待ち"
          }
          tone={roiTone(hb)}
          accentTone="good"
        />
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
              // 見送り (Claude 総合オススメが空束) は plan_X_hit が偶然立っていても
              // 「賭けて当たった」ではないので的中扱いせず、row 自体をグレー bg に。
              const bundleSkipped = r.bundle_participated === false;
              const anyHit =
                !bundleSkipped &&
                (!!r.bundle_hit || !!r.bundle_hit_first_hit);
              const rowBg = bundleSkipped
                ? "bg-(--color-panel-2)"          // 見送り = グレー系
                : anyHit
                  ? "bg-(--color-good)/5"        // 的中 = 緑薄
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
                      <>
                        {/* 色は dashboard 規約に統一: 回収優先=青(info) / 的中優先=緑(good) */}
                        {r.bundle_hit && <Badge tone="info">回収優先</Badge>}
                        {r.bundle_hit_first_hit && <Badge tone="good">的中優先</Badge>}
                      </>
                    ) : (
                      <Badge tone="muted">不的中</Badge>
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
