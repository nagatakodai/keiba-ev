import type { Metadata } from "next";
import Link from "next/link";
import { api } from "@/lib/api";

export const metadata: Metadata = { title: "確率較正" };
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
  parsePlanLabel,
  planAccentClass,
  planTone,
  tierLabel,
  tierTone,
} from "@/components/ui";

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
        <PageHeader title="キャリブレーション" />
        <Card>
          <p className="text-sm text-(--color-bad)">FastAPI に到達できませんでした。</p>
        </Card>
      </Page>
    );
  }

  const confidence = calibrationConfidence(cal.race_count);
  const totalStake = cal.plans.reduce((s, p) => s + p.stake, 0);
  const totalPayout = cal.plans.reduce((s, p) => s + p.payout, 0);
  const overallRoi = totalStake > 0 ? totalPayout / totalStake : 0;
  const lastUpdated = fmtRelativeFromNow(cal.last_updated_at);

  return (
    <Page>
      <PageHeader
        title="キャリブレーション"
        subtitle="計算 EV と実 EV のオフセット、Plan 別 ROI。サンプル 30+ で初めて係数判断材料になる。"
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
        <Stat label="1点あたり賭金" value={fmtYen(cal.point_cost)} />
        <Stat
          label="累計賭金 (全 Plan 合算)"
          value={fmtYen(totalStake)}
        />
        <Stat
          label="累計払戻 (全 Plan 合算)"
          value={fmtYen(totalPayout)}
          hint={`通算回収率 ${Math.round(overallRoi * 100)}%`}
          tone={overallRoi >= 1 ? "good" : overallRoi >= 0.85 ? "warn" : "bad"}
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

      <Card title={`Plan 別 回収率 (1点 ${fmtYen(cal.point_cost)})`}>
        {cal.plans.length === 0 ? (
          <p className="text-sm text-(--color-muted)">データなし。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabnum table-zebra">
              <thead className="text-left text-(--color-muted) text-xs">
                <tr className="border-b border-(--color-line)">
                  <th className="py-2 pr-3">Plan</th>
                  <th className="py-2 pr-3 text-right">参加</th>
                  <th className="py-2 pr-3 text-right">races</th>
                  <th className="py-2 pr-3 text-right">hits</th>
                  <th className="py-2 pr-3 text-right">hit 率</th>
                  <th className="py-2 pr-3 text-right">総点数</th>
                  <th className="py-2 pr-3 text-right">枠 (¥)</th>
                  <th className="py-2 pr-3 text-right">賭金</th>
                  <th className="py-2 pr-3 text-right">払戻</th>
                  <th className="py-2 pr-3 text-right">回収率</th>
                </tr>
              </thead>
              <tbody>
                {cal.plans.map((p) => {
                  const letter = parsePlanLabel(p.plan);
                  const notParticipated = p.participated_races === 0;
                  return (
                    <tr
                      key={p.plan}
                      className={`border-b border-(--color-line)/60 ${
                        notParticipated ? "opacity-60" : ""
                      }`}
                    >
                      <td className="py-1.5 pr-3">
                        <span className={`font-bold ${letter ? planAccentClass(letter) : ""}`}>
                          {p.plan}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3 text-right">{p.participated_races}</td>
                      <td className="py-1.5 pr-3 text-right text-(--color-muted)">{p.races}</td>
                      <td className="py-1.5 pr-3 text-right">{p.hits}</td>
                      <td className="py-1.5 pr-3 text-right">
                        {notParticipated ? (
                          "—"
                        ) : (
                          <span>
                            {fmtPct(p.hit_rate, 1)}
                            {p.hit_rate_ci_low !== undefined &&
                              p.hit_rate_ci_high !== undefined && (
                                <span className="text-[10px] text-(--color-muted) ml-1">
                                  [{fmtPct(p.hit_rate_ci_low, 0)}–
                                  {fmtPct(p.hit_rate_ci_high, 0)}]
                                </span>
                              )}
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-3 text-right">{p.total_points}</td>
                      <td className="py-1.5 pr-3 text-right">{fmtYen(p.assumed_budget_slot)}</td>
                      <td className="py-1.5 pr-3 text-right">{fmtYen(p.stake)}</td>
                      <td className="py-1.5 pr-3 text-right">{fmtYen(p.payout)}</td>
                      <td className="py-1.5 pr-3 text-right">
                        {notParticipated ? (
                          <Badge tone="muted">未参加</Badge>
                        ) : (
                          <div className="flex flex-col items-end gap-0.5">
                            <Badge tone={p.roi >= 1 ? "good" : p.roi >= 0.85 ? "warn" : "bad"}>
                              {Math.round(p.roi * 100)}%
                            </Badge>
                            {p.roi_ci_low !== undefined &&
                              p.roi_ci_high !== undefined && (
                                <span className="text-[10px] text-(--color-muted) tabnum">
                                  [{Math.round(p.roi_ci_low * 100)}–
                                  {Math.round(p.roi_ci_high * 100)}%]
                                </span>
                              )}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-[11px] text-(--color-muted) mt-2">
          「参加」= この Plan が買い目を出したレース数 (hit 率の分母)。「races」は calibration 対象全レース数。
        </p>
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
                        {r.bundle_hit && <Badge tone="good">回収</Badge>}
                        {r.bundle_hit_first_hit && <Badge tone="info">的中</Badge>}
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
