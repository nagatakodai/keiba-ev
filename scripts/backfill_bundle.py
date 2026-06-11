"""既存スナップショットに recommended_bundle (joint Kelly まとめ買い) を後付けする。

live watch-auto 由来の古いスナップショットは recommended_bundle を持たない。
本スクリプトは cached raw HTML (shutuba/past) + スナップショット保存済オッズから
production と同じ手順で probs を再構築し、bundle を計算して JSON に注入する。

  probs 再構築: backtest.py の offline pattern と同じ
    parse_shutuba + parse_past_runs + (snapshot odds を rd へ) + estimate_probs(β=0.78)

使い方:
  .venv/bin/python scripts/backfill_bundle.py            # 全 snapshot
  .venv/bin/python scripts/backfill_bundle.py --force    # 既に bundle ありでも再計算
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import ev as ev_mod  # noqa: E402
from src import portfolio as pf_mod  # noqa: E402
from src.models import BetOdds, TrifectaOdds  # noqa: E402
from src.parse import parse_past_runs, parse_shutuba  # noqa: E402

PRED_DIR = ROOT / "data" / "predictions"
RAW_DIR = ROOT / "data" / "raw"
HISTORY = ROOT / "data" / "cache" / "auto_watch_history.jsonl"

app = typer.Typer(add_completion=False)


def _rid_map() -> dict[str, str]:
    """auto_watch_history から race_id → netkeiba_race_id を構築。"""
    m: dict[str, str] = {}
    if HISTORY.exists():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid, nk = rec.get("race_id"), rec.get("netkeiba_race_id")
            if rid and nk:
                m[rid] = nk
    return m


def _derive_netkeiba_rid(race_id: str) -> str | None:
    """snapshot race_id 'cup_id-schedule-raceno' から netkeiba 12 桁 rid を復元。

    _normalize_race_id の逆変換 (history に無い snapshot 用):
      NAR: cup_id=rid[:10] (10桁) → rid = cup_id + RR
      JRA: cup_id=rid[:8] (8桁) + schedule=開催日 → rid = cup_id + DD + RR
    """
    parts = race_id.split("-")
    if len(parts) != 3:
        return None
    cup_id, sched, raceno = parts
    try:
        rr = f"{int(raceno):02d}"
    except ValueError:
        return None
    if len(cup_id) == 10:        # NAR
        return cup_id + rr
    if len(cup_id) == 8:         # JRA
        try:
            dd = f"{int(sched):02d}"
        except ValueError:
            return None
        return cup_id + dd + rr
    return None


def _read_gz(path: Path) -> str | None:
    if not path.exists():
        return None
    return gzip.open(path, "rt", encoding="utf-8").read()


@app.command()
def main(force: bool = typer.Option(False, "--force", help="既に bundle があっても再計算")):
    rid_map = _rid_map()
    snaps = sorted(PRED_DIR.glob("*.json"))
    done = skipped = failed = 0
    for sp in snaps:
        race_id = sp.stem
        snap = json.loads(sp.read_text(encoding="utf-8"))
        existing = snap.get("recommended_bundle")
        # live で実際に組まれた束 (backfilled フラグ無し) は --force でも上書きしない —
        # 実弾計測の対象を「実際に賭けた束」から「再計算した別の束」にすり替えてしまう
        # (2026-06-11 bughunt 第5R)。再計算してよいのは backfill 自身が書いた束のみ。
        if existing and not existing.get("backfilled"):
            if force:
                print(f"  SKIP {sp.stem}: live 束は --force でも上書きしない (計測保護)")
            skipped += 1
            continue
        if existing and not force:
            skipped += 1
            continue
        nk = rid_map.get(race_id) or _derive_netkeiba_rid(race_id)
        if not nk:
            print(f"  SKIP {race_id}: netkeiba_rid 不明 (history/derive 失敗)")
            skipped += 1
            continue
        sh = _read_gz(RAW_DIR / f"{nk}-shutuba.html.gz")
        if sh is None:
            print(f"  SKIP {race_id}: raw shutuba 無し ({nk})")
            skipped += 1
            continue
        try:
            rd = parse_shutuba(sh, race_id=nk)
            past = _read_gz(RAW_DIR / f"{nk}-past.html.gz")
            if past:
                runs = parse_past_runs(past)
                for h in rd.race.horses:
                    h.past_runs = runs.get(h.number, [])
            # snapshot 保存済オッズを rd に載せる (保存時のオッズに忠実)
            rd.trifecta = [
                TrifectaOdds(key=tuple(r["key"]), odds=r["odds"], popularity=r["popularity"])
                for r in snap.get("rows", [])
            ]
            rd.other_bets = {
                bt: [
                    BetOdds(bet_type=bt, key=tuple(r["key"]), odds=r["odds"],
                            popularity=r.get("popularity", 0))
                    for r in rows
                ]
                for bt, rows in (snap.get("bet_tables") or {}).items()
            }
            probs = ev_mod.estimate_probs(rd, market_blend=0.78)
            cands = pf_mod.candidates_from_snapshot_rows(snap.get("rows", []), snap.get("bet_tables"))
            bundle = pf_mod.build_bundle(cands, probs)
            # backfill 印: 実弾系列の計測 (bundle_calibration_report 等) が「実際に賭けた
            # 束」と区別できるように (2026-06-11 第5R)。
            bundle["backfilled"] = True
            snap["recommended_bundle"] = bundle
            # 締切/発走時刻も再パースで補正 (旧 snapshot は HH:MM発走 表記を拾えず 0 → UI で "—")。
            if rd.race.start_at:
                snap["start_at"] = rd.race.start_at
                snap["close_at"] = rd.race.close_at
            sp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
            n_legs = len(bundle.get("legs", []))
            print(f"  OK   {race_id}: {n_legs} legs ¥{bundle.get('total_stake', 0)} "
                  f"hit={bundle.get('bundle_hit_prob', 0) * 100:.1f}% start_at={snap.get('start_at')} "
                  f"dropped_torigami={bundle.get('dropped_torigami', 0)}")
            done += 1
        except Exception as ex:  # noqa: BLE001
            print(f"  FAIL {race_id}: {type(ex).__name__}: {ex}")
            failed += 1
    print(f"\ndone={done} skipped={skipped} failed={failed} (total {len(snaps)})")


if __name__ == "__main__":
    app()
