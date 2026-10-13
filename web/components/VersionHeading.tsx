import { INDEX_VERSION, INDEX_VERSION_DESC } from "@/lib/version";

// Claude 指数バージョン (v4/v3/v2/v1/β) のセクション見出し。ダッシュボード・競馬場別ページで共有。
//   現行 (INDEX_VERSION) は accent 強調・旧は muted・β (市場由来・〜2026-06-21) は dashed で見分ける。
export function VersionHeading({ version }: { version: "v1" | "v2" | "v3" | "v4" | "β" }) {
  const current = version === INDEX_VERSION;
  const suffix = current ? "（現行）" : version === "β" ? "（β・市場由来）" : "（旧）";
  const desc = INDEX_VERSION_DESC[version] ?? "";
  const badgeClass =
    current
      ? "bg-(--color-accent)/15 text-(--color-accent) border-(--color-accent)/40"
      : version === "β"
        ? "bg-(--color-surface-2) text-amber-300/80 border-amber-400/30 border-dashed"
        : "bg-(--color-surface-2) text-(--color-muted) border-(--color-line)";
  return (
    <div className="flex flex-wrap items-center gap-2 pt-4">
      <span className={`px-2 py-0.5 rounded text-xs font-black tnum border ${badgeClass}`}>
        {version}
      </span>
      <span className="text-sm font-bold">Claude 指数 {version} の計測 {suffix}</span>
      <span className="text-[11px] text-(--color-muted)">— {desc}</span>
    </div>
  );
}
