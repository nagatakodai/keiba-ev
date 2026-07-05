// ダッシュボード（中央）: JRA のみの計測 (研究系カードは地方ページ側に集約)。
// ユーザ指示 2026-07-05「JRAはいったん別ページに避難して。ダッシュボード（地方）と
// ダッシュボード（中央）で分ける」。本体は components/DashboardView.tsx を共有。
import { DashboardView } from "@/components/DashboardView";

export const dynamic = "force-dynamic";

export default function JraDashboardPage() {
  return <DashboardView venue="jra" />;
}
