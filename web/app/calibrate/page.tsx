import type { Metadata } from "next";
import Link from "next/link";
import { TrendingUp } from "lucide-react";
import { api } from "@/lib/api";
import { CHART_COLORS, TrendChart } from "@/components/charts";
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
  const shobuPnl = await api.shobuPnl(pointCost).catch(() => null);

  if (!cal) {
    return (
      <Page>
        <PageHeader eyebrow="Calibration" title="確率較正" />
        <Card>
          <p className="text-sm text-(--color-bad)">FastAPI に到達できませんでした。</p>
        </Card>
      </Page>
    );
  }

  const confidence = calibrationConfidence(cal.race_count);
  const lastUpdated = fmtRelativeFromNow(cal.last_updated_at);
  // 実弾既定束: watch-auto の bet_bundle 設定に追従 (dashboard と同ロジック, 2026-06-10〜)。
  // tb = 実弾既定束 / yb = もう一方 (参考)。EV 側は β=0 事故時代込みの claude_bundle ではなく
  // EV_CUTOFF 以降のみの ev_bundle を使う。
  const watch = await api.watchStatus().catch(() => null);
  const activeBundleKind: "ev" | "trifecta" =
    watch?.config?.bet_bundle ?? (watch?.running ? "trifecta" : "ev");
  const activeBundleLabel = activeBundleKind === "ev" ? "EV束" : "3連単束";
  const tb = activeBundleKind === "ev" ? cal.ev_bundle : cal.trifecta_bundle;
  const yb = activeBundleKind === "ev" ? cal.trifecta_bundle : cal.ev_bundle;
  const ybLabel = activeBundleKind === "ev" ? "3連単束" : "EV束";
  // 回収率は **最終オッズ基準** (roi_final)。最終が無い旧 result は roi に fallback。
  const roiPct = (b?: { roi: number; roi_final?: number } | null) =>
    b ? `${Math.round((b.roi_final ?? b.roi) * 100)}%` : "—";
  // 回収率: 100% 以上 → emerald (利益)、100% 未満 → rose (損失)
  const roiTone = (b?: { participated_races: number; roi: number; roi_final?: number } | null) =>
    !b || b.participated_races === 0 ? "default" : (b.roi_final ?? b.roi) >= 1 ? "good" : "bad";
  // 的中率: 30% 未満 → 赤、30% 以上 → 標準
  const hitTone = (b?: { participated_races: number; hit_rate: number } | null) =>
    !b || b.participated_races === 0 ? "default" : b.hit_rate < 0.3 ? "bad" : "default";

  // Wilson / bootstrap CI を「± 半幅」の控えめ suffix にする (pt 表記)。
  const ciHalf = (lo?: number, hi?: number): string | null =>
    lo != null && hi != null ? (((hi - lo) / 2) * 100).toFixed(1) : null;
  const hitCi = tb ? ciHalf(tb.hit_rate_ci_low, tb.hit_rate_ci_high) : null;
  const roiCi = tb
    ? tb.roi_final_ci_low != null && tb.roi_final_ci_high != null
      ? ciHalf(tb.roi_final_ci_low, tb.roi_final_ci_high)
      : ciHalf(tb.roi_ci_low, tb.roi_ci_high)
    : null;

  // 累積回収率トレンド (実弾系列のみ: 計測対象 + 参加 + backfill 除外)。
  // 最終オッズ基準 (無ければ予想オッズに fallback = 集計と同じ)。
  const trendSrc = cal.races
    .filter((r) =>
      activeBundleKind === "ev"
        ? !!r.ev_measured && r.bundle_participated === true && !r.bundle_backfilled
        : !!r.trifecta_measured && r.trifecta_bundle_participated === true,
    )
    .filter((r) => !!r.saved_at)
    .sort((a, b) => (a.saved_at ?? "").localeCompare(b.saved_at ?? ""));
  const trend: { x: string; roi: number | null }[] = [];
  let cumStake = 0;
  let cumPayout = 0;
  for (const r of trendSrc) {
    const stake =
      (activeBundleKind === "ev" ? r.bundle_stake : r.trifecta_bundle_stake) ?? 0;
    const payout =
      activeBundleKind === "ev"
        ? r.bundle_payout_final ?? r.bundle_payout ?? 0
        : r.trifecta_bundle_payout_final ?? r.trifecta_bundle_payout ?? 0;
    cumStake += stake;
    cumPayout += payout;
    trend.push({
      x: (r.saved_at ?? "").slice(5, 10),
      roi: cumStake > 0 ? Math.round((cumPayout / cumStake) * 1000) / 10 : null,
    });
  }
  const lastRoi = trend.length > 0 ? trend[trend.length - 1].roi : null;
  const trendColor =
    lastRoi != null && lastRoi >= 100 ? CHART_COLORS.positive : CHART_COLORS.negative;

  return (
    <Page>
      <PageHeader
        eyebrow="Calibration"
        title="確率較正"
        subtitle="計算 EV と実 EV のオフセット (tier ratio) + 実弾投票束 (watch-auto の束設定に追従) の実績。サンプル 30+ で初めて判断材料になる。"
      />

      <div>
        <div className="text-[10px] font-bold uppercase tracking-widest text-(--color-muted) mb-2">
          実弾系列 KPI — {activeBundleLabel}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
          <Stat
            label="総合レース数"
            value={<span className="tnum">{cal.race_count}</span>}
            hint={
              tb
                ? `参加 ${tb.participated_races} / 見送り ${tb.skipped_races}`
                : "—"
            }
            accentTone="muted"
          />
          <Stat
            label="的中率"
            value={
              !tb || tb.participated_races === 0 ? (
                "—"
              ) : (
                <span className="tnum">
                  {fmtPct(tb.hit_rate, 1)}
                  {hitCi && (
                    <span className="text-sm font-medium text-(--color-muted) ml-1">±{hitCi}</span>
                  )}
                </span>
              )
            }
            hint={tb && tb.participated_races > 0 ? `${tb.hits} 的中 / ${tb.participated_races} 参加` : "—"}
            tone={hitTone(tb)}
            accentTone="magenta"
          />
          <Stat
            label="回収率"
            value={
              !tb || tb.participated_races === 0 ? (
                roiPct(tb)
              ) : (
                <span className="tnum">
                  {roiPct(tb)}
                  {roiCi && (
                    <span className="text-sm font-medium text-(--color-muted) ml-1">±{roiCi}</span>
                  )}
                </span>
              )
            }
            hint={
              tb && tb.participated_races > 0
                ? `賭金 ${fmtYen(tb.stake)} → 払戻(最終) ${fmtYen(tb.payout_final ?? tb.payout)} · 見送り ${tb.skipped_races}`
                : "賭けたレースなし"
            }
            tone={roiTone(tb)}
            accentTone="magenta"
          />
          <Stat
            label={`${ybLabel} (参考)`}
            value={
              !yb || yb.participated_races === 0 ? (
                "—"
              ) : (
                <span className="tnum">{roiPct(yb)}</span>
              )
            }
            hint={
              yb && yb.participated_races > 0
                ? `的中率 ${fmtPct(yb.hit_rate, 1)} · ${yb.hits} 的中 / ${yb.participated_races} 参加`
                : "計測なし"
            }
            tone={roiTone(yb)}
            accentTone="muted"
          />
        </div>
        <div className="text-[10px] text-(--color-muted) text-right mt-1 px-1">
          ※ 全て {activeBundleLabel} (実弾投票束) 基準
          {yb && yb.participated_races > 0 && (
            <span className="ml-1">
              ／ {ybLabel} (参考): 的中率 {fmtPct(yb.hit_rate, 1)} · 回収率 {roiPct(yb)}
            </span>
          )}
          ／ 集計対象 {cal.race_count} レース ／
          <span className="ml-1">
            <Badge tone={confidence.tone}>{confidence.label}</Badge>
          </span>
          <span className="ml-1">最終更新 {lastUpdated}</span>
        </div>
      </div>

      {trend.length >= 2 && (
        <Card
          title={
            <span className="flex items-center gap-1.5">
              <TrendingUp className="w-4 h-4 text-(--color-accent)" />
              <span>{activeBundleLabel} 累積回収率の推移</span>
            </span>
          }
          right={
            <span className="text-[10px] text-(--color-muted)">
              最終オッズ基準 · backfill 除外 · 基準線 = 100%
            </span>
          }
        >
          <TrendChart
            data={trend}
            series={[{ key: "roi", label: "累積回収率 (%)", color: trendColor }]}
            xKey="x"
            height={200}
            referenceY={100}
          />
        </Card>
      )}

      <Card
        title="勝負レース 仮想収支 (上位N頭の3連単BOX)"
        right={
          <span className="text-[10px] text-(--color-muted)">
            Claude指数 上位N頭の3連単BOX。実際の1-2-3着が全て上位N頭に収まれば的中。8頭以上=5頭(60点)/7頭=4頭(24点)/少頭数は最低3頭を場外に残す。
          </span>
        }
      >
        {!shobuPnl || shobuPnl.recommended_total === 0 ? (
          <p className="text-sm text-(--color-muted)">
            勝負レースのデータがありません (今日の勝負レースをスキャンしてください)。
          </p>
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Stat
                label="対象レース"
                value={<span className="tnum">{shobuPnl.races}</span>}
                hint={`勝負レース ${shobuPnl.recommended_total} / 結果待ち ${shobuPnl.skipped_no_result} / 指数なし ${shobuPnl.skipped_no_index}`}
                accentTone="muted"
              />
              <Stat
                label="的中率"
                value={shobuPnl.races === 0 ? "—" : fmtPct(shobuPnl.hit_rate, 1)}
                hint={
                  shobuPnl.races
                    ? `${shobuPnl.hits} 的中 / ${shobuPnl.races} レース`
                    : "—"
                }
                accentTone="magenta"
              />
              <Stat
                label="回収率"
                value={
                  shobuPnl.races === 0 ? "—" : `${Math.round(shobuPnl.roi * 100)}%`
                }
                hint={
                  shobuPnl.races
                    ? `賭金 ${fmtYen(shobuPnl.stake)} → 払戻 ${fmtYen(shobuPnl.payout)}`
                    : "—"
                }
                tone={
                  shobuPnl.races === 0 ? "default" : shobuPnl.roi >= 1 ? "good" : "bad"
                }
                accentTone="magenta"
              />
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-1.5 mt-3">
              {shobuPnl.races_detail.map((r) => (
                <Link
                  key={r.race_id}
                  href={`/predictions/${r.race_id}`}
                  className={`flex items-center gap-2 text-sm tnum border border-(--color-line) rounded-lg px-2.5 py-1.5 transition-colors hover:border-(--color-accent)/40 ${
                    r.hit ? "bg-emerald-500/10" : ""
                  }`}
                >
                  <span className="font-bold text-xs whitespace-nowrap">
                    {r.venue}
                    {r.race_no}R
                  </span>
                  <span className="text-xs text-(--color-muted) whitespace-nowrap">
                    {r.box}頭BOX
                  </span>
                  <span className="mono text-xs">上位 {r.top_horses.join(",")}</span>
                  <span className="mono text-xs whitespace-nowrap">
                    着 {r.finish.join("-")}
                  </span>
                  <span className="flex-1 text-right mono text-xs">
                    {r.hit ? fmtYen(r.payout) : ""}
                  </span>
                  <Badge tone={r.hit ? "magenta" : "muted"}>
                    {r.hit ? "的中" : "不的中"}
                  </Badge>
                </Link>
              ))}
            </div>
            <p className="text-[10px] text-(--color-muted) mt-2">
              ※ 長期回収を保証する指標ではなく「勝負レース判定 + 上位N頭BOX」の paper
              検証。上部の実弾投票束KPIとは別物。
            </p>
          </>
        )}
      </Card>

      <Card title="Tier 別 (ratio = 実hit / 予測P合計)">
        {cal.tiers.length === 0 ? (
          <p className="text-sm text-(--color-muted)">データなし。`make record` で結果を登録してください。</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabnum table-zebra">
              <thead className="text-left text-(--color-muted)">
                <tr className="border-b border-(--color-line) text-[10px] uppercase tracking-wider">
                  <th className="py-2 pr-3 font-bold">Tier</th>
                  <th className="py-2 pr-3 text-right font-bold">予測 rows</th>
                  <th className="py-2 pr-3 text-right font-bold">予測 P 合計</th>
                  <th className="py-2 pr-3 text-right font-bold">実 hit</th>
                  <th className="py-2 pr-3 text-right font-bold">ratio</th>
                  <th className="py-2 pr-3 font-bold">解釈</th>
                </tr>
              </thead>
              <tbody>
                {cal.tiers.map((t) => (
                  <tr key={t.tier} className="border-b border-(--color-line)/60">
                    <td className="py-1.5 pr-3">
                      <Badge tone={tierTone(t.tier)}>{tierLabel(t.tier)}</Badge>
                    </td>
                    <td className="py-1.5 pr-3 text-right tnum">{t.rows}</td>
                    <td className="py-1.5 pr-3 text-right tnum">{t.prob_sum.toFixed(3)}</td>
                    <td className="py-1.5 pr-3 text-right tnum">{t.hits}</td>
                    <td
                      className={`py-1.5 pr-3 text-right tnum font-semibold ${
                        t.prob_sum > 0
                          ? t.ratio >= 1
                            ? "text-emerald-300"
                            : "text-rose-300"
                          : "text-(--color-muted)"
                      }`}
                    >
                      {t.prob_sum > 0 ? `${t.ratio.toFixed(2)}×` : "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-(--color-muted)">{interpret(t.hits, t.ratio)}</td>
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
              // 「的中」ラベルは**実弾投票束**で判定: EV束計測対象 (ev_measured, 2026-06-10〜)
              // は EV束 (bundle_*)、それ以前は 3連単束 (無ければ旧実弾だった EV束に fallback)。
              // 見送り (束が空) は理論値が立っていても的中扱いせず、row 自体をグレー bg に。
              const useEv = !!r.ev_measured;
              const useTrifecta = !useEv && !!r.trifecta_bundle_participated;
              const bundleSkipped = useEv
                ? r.bundle_participated === false
                : !useTrifecta && r.bundle_participated === false;
              const anyHit =
                !bundleSkipped &&
                !!(useEv ? r.bundle_hit : useTrifecta ? r.trifecta_bundle_hit : r.bundle_hit);
              // backfill (paper 後付け) = 実弾でない race。集計から除外済なので
              // グレーアウト + バッジで「系列に入っていない」ことを明示する。
              const backfilled = !!r.bundle_backfilled;
              const rowBg = bundleSkipped
                ? "bg-slate-500/15"               // 見送り = グレー系
                : anyHit
                  ? "bg-emerald-500/10"           // 的中 = emerald tint
                  : "";                            // 不的中 = 通常
              return (
                <Link
                  key={r.race_id}
                  href={`/predictions/${r.race_id}`}
                  className={`flex items-center gap-2 text-sm tnum border border-(--color-line) rounded-lg px-2.5 py-1.5 transition-colors hover:border-(--color-accent)/40 ${rowBg} ${
                    backfilled ? "opacity-50" : ""
                  }`}
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
                    {backfilled && <Badge tone="muted">backfill</Badge>}
                    {bundleSkipped ? (
                      <Badge tone="muted">見送り</Badge>
                    ) : anyHit ? (
                      <Badge tone="magenta">
                        {useEv ? "EV束 的中" : useTrifecta ? "3連単束 的中" : "的中 (旧EV束)"}
                      </Badge>
                    ) : (
                      <Badge tone="muted">不的中</Badge>
                    )}
                    {useEv && r.trifecta_bundle_hit && (
                      <Badge tone="muted">3連単束(参考)</Badge>
                    )}
                    {!useEv && useTrifecta && r.bundle_hit && (
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
