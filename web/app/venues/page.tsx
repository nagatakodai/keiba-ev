import { MapPin, ServerOff } from "lucide-react";
import {
  api,
  type VenueBreakdown,
  type VenueBreakdownItem,
  type VenueRoiBlock,
} from "@/lib/api";
import { Card, Page, PageHeader, fmtPct } from "@/components/ui";
import { AutoRefresh } from "@/components/AutoRefresh";
import { VersionHeading } from "@/components/VersionHeading";

export const dynamic = "force-dynamic";

export const metadata = { title: "競馬場別" };

function fmtRoiPct(roi: number): string {
  return `${Math.round(roi * 100)}%`;
}
function fmtSignedYen(n: number): string {
  return `${n < 0 ? "-" : "+"}¥${Math.abs(n).toLocaleString()}`;
}
function roiClass(b: VenueRoiBlock): string {
  if (b.races === 0) return "";
  return b.roi >= 1 ? "text-emerald-300" : "text-rose-300";
}

// 1 競馬場のカード: BOX 収支を大きく、各戦略を小さな表で内訳表示。
function VenueCard({ item }: { item: VenueBreakdownItem }) {
  const box = item.box;
  return (
    <Card>
      <div className="space-y-3">
        {/* ヘッダ: 競馬場名 + レース数 + BOX 収支 */}
        <div className="flex items-baseline justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <MapPin className="w-4 h-4 text-sky-300" />
            <span className="text-base font-black tracking-tight">{item.venue}</span>
            <span className="text-[11px] text-(--color-muted) tnum">{item.n_races} R</span>
          </div>
          <div className={`text-lg font-black tnum ${roiClass(box)}`}>
            {box.races ? fmtSignedYen(box.net) : "—"}
          </div>
        </div>

        {/* BOX 収支 KPI 行 (上位N頭3連単BOX) */}
        <div className="grid grid-cols-3 gap-2 text-center rounded-lg bg-(--color-surface-2) py-2">
          <div>
            <div className="text-[9px] font-bold uppercase tracking-widest text-(--color-muted)">
              BOX回収率
            </div>
            <div className={`text-sm font-black tnum ${roiClass(box)}`}>
              {box.races ? fmtRoiPct(box.roi) : "—"}
            </div>
          </div>
          <div>
            <div className="text-[9px] font-bold uppercase tracking-widest text-(--color-muted)">
              BOX的中率
            </div>
            <div className="text-sm font-black tnum">
              {box.races ? fmtPct(box.hit_rate, 0) : "—"}
            </div>
          </div>
          <div>
            <div className="text-[9px] font-bold uppercase tracking-widest text-(--color-muted)">
              的中/対象
            </div>
            <div className="text-sm font-black tnum">
              {box.races_hit}/{box.races}
            </div>
          </div>
        </div>

        {/* 戦略別内訳 */}
        {item.strategies.length > 0 && (
          <table className="w-full text-[11px] table-zebra">
            <thead>
              <tr className="text-left text-[9px] uppercase tracking-wider text-(--color-muted) border-b border-(--color-line)">
                <th className="px-1.5 py-1 font-bold">戦略</th>
                <th className="px-1.5 py-1 font-bold text-right">回収率</th>
                <th className="px-1.5 py-1 font-bold text-right">的中</th>
                <th className="px-1.5 py-1 font-bold text-right">収支</th>
              </tr>
            </thead>
            <tbody>
              {item.strategies.map((s) => (
                <tr key={s.key} className="border-b border-(--color-line-soft)">
                  <td className="px-1.5 py-1 font-medium whitespace-nowrap">{s.label}</td>
                  <td
                    className={`px-1.5 py-1 text-right tnum font-bold ${roiClass(s)}`}
                  >
                    {s.races ? fmtRoiPct(s.roi) : "—"}
                  </td>
                  <td className="px-1.5 py-1 text-right tnum text-(--color-muted)">
                    {s.races_hit}/{s.races}
                  </td>
                  <td
                    className={`px-1.5 py-1 text-right tnum ${
                      s.races === 0 ? "" : s.net < 0 ? "text-rose-300" : "text-emerald-300"
                    }`}
                  >
                    {s.races ? fmtSignedYen(s.net) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Card>
  );
}

// 1 バージョン分の競馬場別セクション (venue カードのグリッド)。
function VenueVersionSection({
  version,
  data,
}: {
  version: "v1" | "v2" | "β";
  data: VenueBreakdown | null;
}) {
  return (
    <>
      <VersionHeading version={version} />
      {data && data.venues.length > 0 ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {data.venues.map((v) => (
            <VenueCard key={v.venue} item={v} />
          ))}
        </div>
      ) : (
        <Card>
          <div className="text-xs text-(--color-muted)">
            このバージョンの結果確定レースはまだありません。
          </div>
        </Card>
      )}
    </>
  );
}

function ApiDownCard() {
  return (
    <Card tone="alert">
      <div className="flex items-center gap-3">
        <ServerOff className="w-6 h-6 text-amber-300 shrink-0" />
        <div className="text-xs text-(--color-muted)">
          API (port 9788) に到達できません。<code className="mono text-emerald-300">make api</code>{" "}
          を起動してください。15 秒おきに自動再接続します。
        </div>
      </div>
    </Card>
  );
}

export default async function VenuesPage() {
  // β (市場由来・〜2026-06-21) は対象が少ないため表示しない (ユーザ指示で撤去)。
  const [v2, v1] = await Promise.all([
    api.venueBreakdown("v2").catch(() => null),
    api.venueBreakdown("v1").catch(() => null),
  ]);

  return (
    <Page>
      <AutoRefresh seconds={30} />
      <PageHeader
        eyebrow="Venue Breakdown"
        title="競馬場別の内訳"
        subtitle="shobu 評価レースの仮想収支 (上位N頭3連単BOX + 各戦略) を競馬場 (venue) 毎に内訳表示。Claude 指数バージョン毎 (v2=現行 / v1=旧) に分離。30 秒おきに自動更新。"
      />
      {!v2 && !v1 ? (
        <ApiDownCard />
      ) : (
        <>
          <VenueVersionSection version="v2" data={v2} />
          <VenueVersionSection version="v1" data={v1} />
        </>
      )}
    </Page>
  );
}
