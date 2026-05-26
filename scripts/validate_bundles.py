"""既存スナップショットの「Claude 総合オススメ」束を claude -p で順次検証する。

非空バンドルを持つ snapshot を 1 件ずつ取り、live analyze と同じ手順
(rd 再構築 → estimate_probs → build_table/build_all_bet_tables →
analyze._validate_and_update_bundle) で web 調査検証し、llm_review を付与する。

  - 既に llm_review.validated=True のものは skip (--force で再検証)
  - レース 1 件ずつ順次 (netkeiba/claude を叩きすぎない)
  - raw HTML が無い / 検証不能なものは skip

使い方:
  .venv/bin/python scripts/validate_bundles.py
  .venv/bin/python scripts/validate_bundles.py --force
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import analyze as az  # noqa: E402
from src import ev as ev_mod  # noqa: E402
from src import llm as llm_mod  # noqa: E402
from src.aptitude import compute_aptitudes  # noqa: E402
from src.features import build_features  # noqa: E402
from src.market_signal import compute_market_signals  # noqa: E402
from src.models import BetOdds, TrifectaOdds  # noqa: E402
from src.parse import parse_past_runs, parse_shutuba  # noqa: E402

# backfill と同じ rid 逆引きを再利用
sys.path.insert(0, str(ROOT / "scripts"))
from backfill_bundle import _derive_netkeiba_rid, _rid_map  # noqa: E402

PRED_DIR = ROOT / "data" / "predictions"
RAW_DIR = ROOT / "data" / "raw"

app = typer.Typer(add_completion=False)


def _read_gz(path: Path) -> str | None:
    if not path.exists():
        return None
    return gzip.open(path, "rt", encoding="utf-8").read()


@app.command()
def main(force: bool = typer.Option(False, "--force", help="既に検証済でも再検証")):
    if not llm_mod.is_available():
        print("claude CLI が見つかりません。中断。")
        raise typer.Exit(1)
    rid_map = _rid_map()
    snaps = sorted(PRED_DIR.glob("*.json"))
    done = skipped = failed = 0
    for sp in snaps:
        race_id = sp.stem
        snap = json.loads(sp.read_text(encoding="utf-8"))
        b = snap.get("recommended_bundle")
        if not b or not b.get("legs"):
            skipped += 1
            continue  # 見送り (空束) は検証対象なし
        if b.get("llm_review", {}).get("validated") and not force:
            print(f"  SKIP {race_id}: 既に検証済")
            skipped += 1
            continue
        nk = rid_map.get(race_id) or _derive_netkeiba_rid(race_id)
        sh = _read_gz(RAW_DIR / f"{nk}-shutuba.html.gz") if nk else None
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
            feats = build_features(rd)
            probs = ev_mod.estimate_probs(rd, market_blend=0.78)
            rows = ev_mod.build_table(rd, probs)
            bet_tables = ev_mod.build_all_bet_tables(rd, probs)
            apts = compute_aptitudes(rd, feats=feats)
            sigs = compute_market_signals(rd)
            best_times = az._serialize_best_times(rd, feats)
            print(f"\n===== validating {race_id} ({nk}) =====")
            az._validate_and_update_bundle(
                race_id, rd, probs, rows, bet_tables,
                aptitudes=apts, market_signals=sigs,
                horse_best_times=best_times, model="opus",
            )
            done += 1
        except Exception as ex:  # noqa: BLE001
            print(f"  FAIL {race_id}: {type(ex).__name__}: {ex}")
            failed += 1
    print(f"\ndone={done} skipped={skipped} failed={failed} (total {len(snaps)})")


if __name__ == "__main__":
    app()
