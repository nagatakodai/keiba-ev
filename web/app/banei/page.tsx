// ダッシュボード（ばんえい）: 帯広ばんえいのみの計測 (研究系カードは地方ページ側に集約)。
// ユーザ指示 2026-07-05「ばんえいは地方と中央と同じように別ダッシュボードに分離」。
// ばんえいは別競技 (確率モデルも ev.segment_of_rd で分離済) なので地方平地と混ぜない。
// 本体は components/DashboardView.tsx を共有。
import { DashboardView } from "@/components/DashboardView";

export const dynamic = "force-dynamic";

export default function BaneiDashboardPage() {
  return <DashboardView venue="banei" />;
}
