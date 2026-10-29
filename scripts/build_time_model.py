"""タイム換算モデル (src/time_model.py, te1〜te2) の学習データ抽出 + 学習。

  python scripts/build_time_model.py                # 公式データ同期 → ledger を全部読んで学習 → data/cache/time_model.json
  python scripts/build_time_model.py --extract-raw  # data/raw から netkeiba_raw.jsonl を作り直してから学習
  python scripts/build_time_model.py --no-official  # 地方の公式データ (keiba.go.jp) を取りに行かない (手元の ledger だけ)

data/raw の netkeiba キャッシュ (馬柱 5 走 + 結果表の全着時計) は netkeiba 規制で増えないので、
netkeiba_raw.jsonl は初回 (ファイルが無いとき) だけ自動で抽出する。地方は地方競馬全国協会の公式データ
(src/nar_official.py の月次 CSV → nar_official-YYYYMM.jsonl) を学習窓ぶん同期する (確定した月は 1 回だけ・
当月は 1 日 1 回まで)。中央は解析時に追記される live-YYYYMM.jsonl (JRA 公式 accessU の過去走) が加わる。
data/raw は読むだけ。`make time-model` と API 常駐ループの日次再学習から呼ぶ。
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import time_model as tm  # noqa: E402

RAW = ROOT / "data" / "raw"
RAW_LEDGER = tm.LEDGER_DIR / "netkeiba_raw.jsonl"


def _result_times(rid: str) -> dict[int, float]:
    """netkeiba 結果表 → {馬番: 走破時計(秒)}。列 0 着順 / 2 馬番 / 7 タイム (JRA/NAR 共通)。"""
    from bs4 import BeautifulSoup
    from src.parse import _parse_time_to_sec
    p = RAW / f"{rid}-result.html.gz"
    if not p.exists():
        return {}
    soup = BeautifulSoup(gzip.open(p, "rt", encoding="utf-8").read(), "lxml")
    t = soup.select_one("table.ResultTableWrap") or soup.select_one(".RaceTable01")
    if not t:
        return {}
    out: dict[int, float] = {}
    for tr in t.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 11:
            continue
        pos = tds[0].get_text(strip=True)
        num = tds[2].get_text(strip=True)
        if not num.isdigit() or not pos.isdigit():    # 取消/中止/除外/失格 は除く
            continue
        sec = _parse_time_to_sec(tds[7].get_text(strip=True))
        if sec and sec > 0:
            out[int(num)] = float(sec)
    return out


def _extract_one(rid: str) -> list[dict]:
    from src.dataset import _race_date, load_race
    try:
        loaded = load_race(rid)
    except Exception:  # noqa: BLE001
        return []
    if loaded is None:
        return []
    rd, _res = loaded
    race = rd.race
    rows: list[dict] = []
    for h in race.horses:
        for pr in h.past_runs or []:
            r = tm.ledger_row(h.name, pr)
            if r is not None:
                rows.append(r)
    # 当該レース自身の全着時計 (結果表)。学習時は日付で窓を切るので評価のリークにはならない。
    try:
        times = _result_times(rid)
    except Exception:  # noqa: BLE001
        times = {}
    surface = tm.norm_surface(race.surface, race.race_class)
    venue = tm.norm_venue(race.venue_name)
    wt = race.weather_text or ""
    going = tm.norm_going(wt.split("/")[-1]) if "/" in wt else ""
    d = int(_race_date(rid, rd) or 0)
    if times and d and tm.is_known_venue(venue):
        win = min(times.values())
        names = {h.number: h.name for h in race.horses}
        for num, sec in times.items():
            if num in names and tm.valid_flat_time(surface, race.distance, sec):
                rows.append({"h": tm.norm_horse(names[num]), "d": d, "v": venue, "s": surface,
                             "dist": int(race.distance), "g": going, "t": round(sec, 2),
                             "wt": round(win, 2)})
    return rows


def extract_raw(workers: int) -> int:
    from src.dataset import _iter_race_ids
    rids = list(_iter_race_ids())
    print(f"extracting ledger from {len(rids):,} cached netkeiba races (workers={workers}) ...", flush=True)
    tm.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RAW_LEDGER.with_suffix(".jsonl.tmp")
    n = 0
    with ProcessPoolExecutor(max_workers=workers) as ex, open(tmp, "w", encoding="utf-8") as f:
        for rows in ex.map(_extract_one, rids, chunksize=16):
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
    tmp.replace(RAW_LEDGER)
    print(f"wrote {RAW_LEDGER} ({n:,} rows before dedup)", flush=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract-raw", action="store_true", help="data/raw から netkeiba_raw.jsonl を作り直す")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-official", action="store_true",
                    help="地方の公式データ (keiba.go.jp) を同期しない (環境変数 KEIBA_NAR_OFFICIAL=0 と同じ)")
    ap.add_argument("--official-since", default=None, help="公式データを同期する最初の月 YYYYMM (既定: 学習窓の始まり)")
    args = ap.parse_args()
    t0 = time.time()
    if args.extract_raw or not RAW_LEDGER.exists():
        extract_raw(args.workers)
    if not args.no_official:
        # 通信失敗でも手元の ledger で学習は続ける (取得失敗は meta に残り、API は 3 時間後に再試行する)
        try:
            from src import nar_official
            res = nar_official.sync(since=args.official_since, log=lambda m: print(m, flush=True))
            print(f"nar_official: {'DISABLED (KEIBA_NAR_OFFICIAL=0) ' if res['disabled'] else ''}"
                  f"downloaded={len(res['downloaded'])} cached={len(res['cached'])} "
                  f"missing={res['missing'] or '-'} failed={res['failed'] or '-'} "
                  f"deferred={res['deferred'] or '-'} ledger_written={len(res['ledger_written'])}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"nar_official sync failed (続行): {e!r}", flush=True)
    import datetime as _dt
    lim = _dt.date.today() - _dt.timedelta(days=tm.WINDOW_DAYS + tm.LOAD_SLACK_DAYS)   # rebuild と同じ
    ledger = tm.load_ledger(min_date=lim.year * 10000 + lim.month * 100 + lim.day)
    print(f"ledger rows (dedup, >= {lim}) = {len(ledger):,} from {len(list(tm.LEDGER_DIR.glob('*.jsonl')))} file(s)",
          flush=True)
    model = tm.fit(ledger)
    tm.save_model(model)
    print(f"saved {tm.MODEL_PATH}: version={model['version']} data_until={model.get('data_until')} "
          f"runs={model.get('n_runs'):,} horses={model.get('n_horses'):,} courses={model.get('n_cells'):,} "
          f"days={model.get('n_days'):,} iterations={(model.get('params') or {}).get('iterations')} "
          f"({time.time() - t0:.0f}s)", flush=True)
    if (model.get("params") or {}).get("converged") is False:
        # 本番の ledger は 70 回前後で収束する。上限まで回ったのは、他の場と馬を共有しない場 (θ の水準がデータで
        # 決まらない) など ledger が疎なとき — 換算はその場と他場の間で信頼できない (GET /api/results/auto の time_model)
        print(f"WARNING: 換算モデルが収束していません (iterations={model['params'].get('iterations')} = MAX_ITERS)",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
