import type { Metadata } from "next";
import Link from "next/link";
import { api } from "@/lib/api";
import {
  Badge,
  Card,
  Page,
  PageHeader,
  Stat,
  fmtPct,
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

  return (
    <Page>
      <PageHeader
        eyebrow="Calibration"
        title="確率較正"
        subtitle="計算 EV と実 EV のオフセット (tier ratio) + 勝負レースの仮想収支 (上位N頭3連単BOX) の paper 実績。サンプル 30+ で初めて判断材料になる。"
      />

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
