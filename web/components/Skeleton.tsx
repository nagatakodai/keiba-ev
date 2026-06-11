// ローディング中のプレースホルダ (shimmer は globals.css の .skeleton-shimmer)。
// server / client どちらからでも使える純表示コンポーネント。
// wave-2 のページ刷新で fetch 中スケルトンとして使う。

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton-shimmer rounded-md ${className}`} aria-hidden />;
}

// 1 行テキスト相当
export function SkeletonText({ className = "w-32" }: { className?: string }) {
  return <Skeleton className={`h-3.5 ${className}`} />;
}

// Card と同形 (タイトルバー + 本文ブロック)
export function SkeletonCard({
  lines = 4,
  className = "",
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <div
      className={`bg-(--color-card) border border-(--color-line) rounded-xl overflow-hidden ${className}`}
      aria-hidden
    >
      <div className="px-4 py-2.5 border-b border-(--color-line)">
        <Skeleton className="h-4 w-40" />
      </div>
      <div className="p-4 space-y-2.5">
        {Array.from({ length: lines }).map((_, i) => (
          <Skeleton key={i} className={`h-3.5 ${i === lines - 1 ? "w-2/3" : "w-full"}`} />
        ))}
      </div>
    </div>
  );
}

// Stat と同形 (label + 大きい数値)
export function SkeletonStat({ className = "" }: { className?: string }) {
  return (
    <div
      className={`bg-(--color-card) border border-(--color-line) border-l-4 border-l-(--color-line) rounded-xl px-4 py-3 ${className}`}
      aria-hidden
    >
      <Skeleton className="h-3 w-20" />
      <Skeleton className="h-7 w-28 mt-2" />
    </div>
  );
}

// テーブル行 (cols 個のセル)。<tbody> 直下で rows 行分描画する。
export function SkeletonTableRows({
  rows = 5,
  cols = 4,
}: {
  rows?: number;
  cols?: number;
}) {
  return (
    <>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} aria-hidden>
          {Array.from({ length: cols }).map((_, c) => (
            <td key={c} className="px-2 py-2">
              <Skeleton className={`h-3.5 ${c === 0 ? "w-16" : "w-full max-w-24"}`} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
