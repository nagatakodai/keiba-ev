"""research_mode (agentic vs prefetch) A/B 比較レポート (読み取り専用)。

shobu の `--research ab` (2026-07-06〜既定) がレースごとに md5(rid) 偶奇で
prefetch/agentic を決定論 50/50 割当する。その蓄積を 2 つの表で集計する:

【表1: 実績 mode 別 (per-protocol)】 snapshot の `llm_research_mode` (dispatcher の
実績値 = prefetch が dossier 不可で agentic に落ちた場合はその実績) で分割し、
  (a) レース数 (指数あり / 結果確定 / 発走前採点)
  (c) 戦略ROI (win1/place1/place2/quinella12) — api.store._strategy_race_legs を
      そのまま再利用 (≤1.1 フィルタ・複勝頭数ルール・同着判定が計測本体と同一)
  (d) Claude#1 が勝者だった率 / 勝者が Claude 上位3頭に入る率
を出す。mode 欠落 (旧 snapshot / 刻印前) は "unknown" として別集計。

【表2: 割当腕別 (ITT)】 指数生成に失敗したレースは llm_research_mode が刻まれない
(analyze は llm_win_index が無いと None 保存) ため、**生成成功率は実績 mode では
出せない**。そこで割当腕 (md5(rid) 偶奇 = shobu._ab_research_arm、決定論なので後から
再計算できる) で分割し、--ab-since (既定 2026-07-06 = ab 既定化日) 以降の snapshot
について 腕別の指数生成成功率と実績 mode 内訳 (= prefetch→agentic フォールバック率)
を出す。注意: scrape ごと失敗したレースは snapshot 自体が無く母数に入らない。

usage:
  python scripts/research_mode_ab.py                 # 既定 --since 20260705 (ARCH-B 導入日)
  python scripts/research_mode_ab.py --all           # 期間フィルタなし (unknown が旧全件になる)
  python scripts/research_mode_ab.py --since 20260710 --ab-since 20260710
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.store import (  # noqa: E402
    PRED_DIR,
    RESULT_DIR,
    _claude_index_by_number,
    _finish_ranks,
    _scored_at,
    _strategy_race_legs,
    netkeiba_rid_from_internal,
)
from src.shobu import _ab_research_arm  # noqa: E402

# 比較する戦略 (STRATEGY_DEFS のサブセット・per の key)。
_STRATEGIES = ["win1", "place1", "place2", "quinella12"]
_JST = timezone(timedelta(hours=9))


def _norm_date(s: str) -> str:
    """YYYYMMDD / YYYY-MM-DD → ISO 接頭辞 (snapshot の _scored_at と接頭辞比較する)。"""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _has_index(snap: dict[str, Any]) -> bool:
    """Claude 指数が実際に付いたか (store.index_version_of と同じ規約)。"""
    return any(r.get("claude_index") is not None
               for r in (snap.get("index_compare") or [])) or bool(snap.get("llm_win_index"))


def _scored_pre_start(snap: dict[str, Any]) -> bool:
    """指数採点時刻 < 発走時刻 (store._tagged_eval_races と同じ ISO 接頭辞比較・不明は False)。"""
    ts = snap.get("start_at") or 0
    scored = _scored_at(snap)
    if not ts or not scored:
        return False
    start_iso = (datetime.fromtimestamp(int(ts), _JST)
                 .replace(tzinfo=None).isoformat(timespec="seconds"))
    return scored < start_iso


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _new_bucket() -> dict[str, Any]:
    return {
        "races_index": 0,          # 指数あり
        "pre_start": 0,            # うち発走前採点
        "races_ok": 0,             # 戦略脚を評価できた (結果確定 + 払戻あり)
        "no_result": 0,
        "no_odds": 0,
        "no_index": 0,             # 指数3頭未満 (指数ありでも順位を組めない)
        "win_hits": 0,             # Claude#1 が勝者 (races_ok 母数)
        "top3_hits": 0,            # 勝者が Claude 上位3頭 (races_ok 母数)
        "per": {k: {"stake": 0, "payout": 0, "bets": 0, "hits": 0} for k in _STRATEGIES},
    }


def collect(since: str | None, ab_since: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(表1: 実績 mode 別, 表2: 割当腕別 ITT) の集計 dict を返す。"""
    modes: dict[str, dict[str, Any]] = {}
    itt: dict[str, dict[str, Any]] = {}
    for path in sorted(PRED_DIR.glob("*.json")):
        if path.name.endswith(".llm.json"):
            continue
        snap = _load_json(path)
        if not snap:
            continue
        race_id = snap.get("race_id") or path.stem
        scored = _scored_at(snap)
        has_index = _has_index(snap)

        # --- 表2 (ITT): ab_since 以降の snapshot を割当腕で分ける (成功/失敗とも) ---
        if scored >= ab_since:
            rid = netkeiba_rid_from_internal(race_id)
            if rid:
                arm = _ab_research_arm(rid)
                b = itt.setdefault(arm, {"snapshots": 0, "with_index": 0, "actual": {}})
                b["snapshots"] += 1
                if has_index:
                    b["with_index"] += 1
                    actual = snap.get("llm_research_mode") or "unknown"
                    b["actual"][actual] = b["actual"].get(actual, 0) + 1

        # --- 表1 (per-protocol): 指数ありレースを実績 mode で分ける ---
        if not has_index:
            continue
        if since and scored < since:
            continue
        mode = snap.get("llm_research_mode") or "unknown"
        bucket = modes.setdefault(mode, _new_bucket())
        bucket["races_index"] += 1
        if _scored_pre_start(snap):
            bucket["pre_start"] += 1

        result = _load_json(RESULT_DIR / f"{path.stem}.json")
        if not result:
            bucket["no_result"] += 1
            continue
        detail, reason = _strategy_race_legs(snap, result, point_cost=100,
                                             meta={"race_id": race_id})
        if reason != "ok" or detail is None:
            bucket[reason if reason in ("no_result", "no_odds", "no_index")
                   else "no_result"] += 1
            continue
        bucket["races_ok"] += 1
        per = detail.get("per") or {}
        for k in _STRATEGIES:
            s = per.get(k) or {}
            for f in ("stake", "payout", "bets", "hits"):
                bucket["per"][k][f] += int(s.get(f) or 0)
        # (d) Claude#1 勝者率 / 勝者∈上位3頭率 (同着 1着は複数馬になり得る)
        idx = _claude_index_by_number(snap)
        ranked = [n for n, _v in sorted(idx.items(), key=lambda kv: (-kv[1], kv[0]))]
        winners = {n for n, r in _finish_ranks(result).items() if r == 1}
        if winners and ranked:
            if ranked[0] in winners:
                bucket["win_hits"] += 1
            if any(w in ranked[:3] for w in winners):
                bucket["top3_hits"] += 1
    return modes, itt


def _pct(a: int, b: int) -> str:
    return f"{a / b * 100:5.1f}%" if b else "    – "


def _roi(payout: int, stake: int) -> str:
    return f"{payout / stake * 100:6.1f}%" if stake else "     – "


def render(modes: dict[str, Any], itt: dict[str, Any], since: str | None,
           ab_since: str) -> str:
    out: list[str] = []
    order = [m for m in ("prefetch", "agentic", "unknown") if m in modes]
    order += [m for m in sorted(modes) if m not in order]

    out.append(f"== 表1: 実績 research_mode 別 (per-protocol{f', scored_at ≥ {since}' if since else ''}) ==")
    if not order:
        out.append("  (対象レースなし — --all で期間フィルタを外すか蓄積を待つ)")
    for m in order:
        b = modes[m]
        out.append(f"\n[{m}] 指数あり {b['races_index']}R (発走前採点 {b['pre_start']}) / "
                   f"評価可 {b['races_ok']}R "
                   f"(no_result {b['no_result']} / no_odds {b['no_odds']} / no_index {b['no_index']})")
        if b["races_ok"]:
            out.append(f"  Claude#1 勝率 {_pct(b['win_hits'], b['races_ok'])} / "
                       f"勝者∈上位3頭 {_pct(b['top3_hits'], b['races_ok'])}")
        out.append(f"  {'戦略':12s} {'bets':>5s} {'hits':>5s} {'的中率':>7s} "
                   f"{'stake':>8s} {'payout':>8s} {'ROI':>8s}")
        for k in _STRATEGIES:
            s = b["per"][k]
            out.append(f"  {k:12s} {s['bets']:5d} {s['hits']:5d} "
                       f"{_pct(s['hits'], s['bets']):>7s} "
                       f"{s['stake']:8d} {s['payout']:8d} {_roi(s['payout'], s['stake']):>8s}")

    out.append(f"\n== 表2: 割当腕別 ITT (scored_at ≥ {ab_since} = ab 既定化以降) ==")
    if not itt:
        out.append("  (対象 snapshot なし — ab 割当での scan 実行後に貯まる)")
    for arm in ("prefetch", "agentic"):
        if arm not in itt:
            continue
        b = itt[arm]
        actual = " / ".join(f"{k}:{v}" for k, v in sorted(b["actual"].items())) or "-"
        out.append(f"  [{arm}腕] snapshot {b['snapshots']} / 指数生成成功 {b['with_index']} "
                   f"({_pct(b['with_index'], b['snapshots']).strip()}) / 実績mode内訳: {actual}")
    if "prefetch" in itt:
        pf = itt["prefetch"]
        fb = pf["with_index"] - pf["actual"].get("prefetch", 0)
        if pf["with_index"]:
            out.append(f"  prefetch腕の agentic フォールバック率: "
                       f"{_pct(fb, pf['with_index']).strip()} "
                       f"(dossier 不可 → agentic に内部フォールバックした分)")
    out.append("\n注: 表1 は成功レースのみ (per-protocol)。表2 が ITT (割当ベース) で、"
               "scrape ごと失敗したレースは snapshot が無く両表とも母数外。")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="research_mode (agentic/prefetch) A/B 比較レポート")
    ap.add_argument("--since", default="20260705",
                    help="表1 の対象 (llm_scored_at ≥ これ)。既定 20260705 = ARCH-B 導入日")
    ap.add_argument("--ab-since", default="20260706",
                    help="表2 (ITT) の対象。既定 20260706 = --research ab 既定化日")
    ap.add_argument("--all", action="store_true", help="表1 の期間フィルタを外す (旧 snapshot 全件)")
    args = ap.parse_args(argv)
    since = None if args.all else _norm_date(args.since)
    ab_since = _norm_date(args.ab_since)
    modes, itt = collect(since, ab_since)
    print(render(modes, itt, since, ab_since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
