"""券種別ドリフトシェード (DRIFT_SHADE) の較正 — 読み取り専用 (ユーザ指示 2026-07-05)。

`data/cache/odds_timeline/<race_id>.jsonl` の **締切直前キャプチャ** (bet/poll 帯) と
`data/results/<race_id>.json` の **実払戻 (final_odds, 的中組)** を突合し、券種別に
    ratio = 実払戻オッズ / 締切直前オッズ
の分布を出す。これは「束を組んだ時のオッズから実払戻がどれだけ下振れするか」の実測で、
- `portfolio.DRIFT_SHADE` (odds_eff = odds×shade — EV ゲートとトリガミ判定の期待実払戻) は
  **median** で較正する (中心推定)。
- `TORIGAMI_MARGIN` (=1.10) は shade 適用後の残差テールを吸収する緩衝 — shade 較正後に
  「P(実払戻 < odds×shade)」の分位も併記して妥当性を確認する。

複勝/ワイドはキャプチャ側がレンジ**下限**なので、ratio は「下限に対して実払戻がどうだったか」
= まさに束計算に使った数値に対する実測 (これが較正したい量)。

使い方: .venv/bin/python scripts/calibrate_drift_shade.py [--max-lead 600] [--min-n 8]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TIMELINE_DIR = ROOT / "data" / "cache" / "odds_timeline"
RESULT_DIR = ROOT / "data" / "results"
JST = ZoneInfo("Asia/Tokyo")

# netkeiba 場コード 01-10 = JRA (rid 先頭 4 桁が年、次 2 桁が場)。内部 race_id からは
# cup_id で判定できないため、ratio の segment 分割は「頭数などでなく」タイムラインの
# odds に quinella/wide が揃うか (keibago/JRA 経路) 等では見ずに、レース id の形で近似する。
def _is_jra(race_id: str) -> bool:
    head = race_id.split("-")[0]
    return len(head) == 8 and head[4:6].isdigit() and 1 <= int(head[4:6]) <= 10


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso).replace(tzinfo=JST).timestamp()
    except ValueError:
        return 0.0


def _quantiles(vals: list[float]) -> dict[str, float]:
    vs = sorted(vals)
    q = lambda p: vs[min(len(vs) - 1, int(p * len(vs)))]
    return {"p5": q(0.05), "p10": q(0.10), "p25": q(0.25), "median": q(0.50),
            "p75": q(0.75), "p90": q(0.90)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-lead", type=int, default=600,
                    help="参照キャプチャとして許す 締切までの秒数上限 (既定 600 = bet/poll 帯)")
    ap.add_argument("--min-n", type=int, default=8, help="表に出す最小サンプル数")
    args = ap.parse_args()

    ratios: dict[str, list[float]] = {}
    ratios_seg: dict[tuple[str, str], list[float]] = {}
    n_races = n_used = 0
    leads: list[float] = []

    for f in sorted(TIMELINE_DIR.glob("*.jsonl")):
        race_id = f.stem
        rp = RESULT_DIR / f"{race_id}.json"
        if not rp.exists():
            continue
        n_races += 1
        try:
            result = json.loads(rp.read_text(encoding="utf-8"))
            entries = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        fo = result.get("final_odds") or {}
        if not fo or not entries:
            continue
        # 参照 = 締切前 max_lead 秒以内の最後のキャプチャ (bet/poll 帯)。
        ref = None
        ref_lead = None
        for e in entries:
            close = e.get("close_at") or 0
            ts = _ts(e.get("captured_at") or "")
            if not close or not ts:
                continue
            lead = close - ts
            if 0 <= lead <= args.max_lead and (ref is None or ts > _ts(ref.get("captured_at", ""))):
                ref, ref_lead = e, lead
        if ref is None:
            continue
        n_used += 1
        leads.append(float(ref_lead or 0))
        odds = ref.get("odds") or {}
        seg = "jra" if _is_jra(race_id) else "nar"
        for key, pay in fo.items():
            if ":" not in key:
                continue
            bt, combo = key.split(":", 1)
            try:
                pay_f = float(pay)
            except (TypeError, ValueError):
                continue
            ref_o = (odds.get(bt) or {}).get(combo)
            if not ref_o or pay_f <= 0:
                continue
            try:
                r = pay_f / float(ref_o)
            except (TypeError, ZeroDivisionError, ValueError):
                continue
            if r <= 0 or r > 50:      # 組違い等の異常値ガード
                continue
            ratios.setdefault(bt, []).append(r)
            ratios_seg.setdefault((bt, seg), []).append(r)

    print(f"timeline レース (結果あり): {n_races} / 参照キャプチャあり (≤{args.max_lead}s 前): {n_used}")
    if leads:
        ls = sorted(leads)
        print(f"参照キャプチャの締切前リード: median {ls[len(ls)//2]:.0f}s")

    print(f"\n== 実払戻 / 締切直前オッズ の分布 (的中組, 券種別) ==")
    print(f"{'券種':>10} {'n':>5} {'p5':>6} {'p10':>6} {'p25':>6} {'med':>6} {'p75':>6} "
          f"{'現行shade':>9} {'提案(med)':>9} {'P(r<shade)':>10}")
    from src.portfolio import DRIFT_SHADE, _DRIFT_SHADE_DEFAULT
    for bt in ("win", "place", "quinella", "wide", "exacta", "trio", "trifecta"):
        vals = ratios.get(bt) or []
        if len(vals) < args.min_n:
            print(f"{bt:>10} {len(vals):>5}  (n不足)")
            continue
        q = _quantiles(vals)
        cur = DRIFT_SHADE.get(bt, _DRIFT_SHADE_DEFAULT)
        below = sum(1 for v in vals if v < cur) / len(vals)
        prop = min(1.0, q["median"])   # win と同じく 1.0 でキャップ (上振れは EV に織り込まない)
        print(f"{bt:>10} {len(vals):>5} {q['p5']:>6.2f} {q['p10']:>6.2f} {q['p25']:>6.2f} "
              f"{q['median']:>6.2f} {q['p75']:>6.2f} {cur:>9.2f} {prop:>9.2f} {below:>9.0%}")

    print("\n== segment 別 (参考: NAR 薄 pool の自票インパクト検証) ==")
    for (bt, seg), vals in sorted(ratios_seg.items()):
        if len(vals) < args.min_n:
            continue
        q = _quantiles(vals)
        print(f"  {bt:>10} × {seg}: n={len(vals):>4} med {q['median']:.2f} "
              f"p25 {q['p25']:.2f} p10 {q['p10']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
