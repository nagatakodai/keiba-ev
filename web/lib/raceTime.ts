// 走破時計 (秒) の表示フォーマッタ。タイム換算比較カード (components/TimeEvalCard.tsx) 等で共用。

// 秒 → "M:SS.s" (60 秒未満は "SS.s")。null / 非有限 / 負値は "—"。
//   72.8 → "1:12.8" / 58.4 → "58.4" / 125.0 → "2:05.0"
// 先に 0.1 秒単位へ丸めてから分を切り出す (59.96 → "1:00.0"。"60.0" や "0:60.0" にしない)。
export function fmtRaceTime(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
  const tenths = Math.round(sec * 10);
  if (tenths < 600) return (tenths / 10).toFixed(1);
  const min = Math.floor(tenths / 600);
  const rest = (tenths - min * 600) / 10;
  return `${min}:${rest.toFixed(1).padStart(4, "0")}`;
}

// 符号付きの秒差 ("+0.3" / "-0.2" / "0.0")。丸めて 0 になる値は符号を付けない。null → "—"。
export function fmtSignedSec(sec: number | null | undefined, digits = 1): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const s = sec.toFixed(digits);
  if (Number(s) === 0) return (0).toFixed(digits);
  return sec > 0 ? `+${s}` : s;
}
