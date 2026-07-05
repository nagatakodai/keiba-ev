// ダッシュボード（地方）: NAR (ばんえい含む) の計測 + 研究系カード (プレレジ台帳/市場一致
// マトリクス)。JRA は /jra (ダッシュボード（中央）) に分離 (ユーザ指示 2026-07-05)。
// 本体は components/DashboardView.tsx (venue プロップ付き共有 server component)。
import { DashboardView } from "@/components/DashboardView";

export const dynamic = "force-dynamic";

export default function DashboardPage() {
  return <DashboardView venue="nar" />;
}
