"""タイム換算比較 (te1) の walk-forward バックテスト — 地方・公式データ期間 (読み取り専用)。

評価レース = 地方競馬全国協会の公式月次データ (data/cache/nar_official/race-YYYYMM.zip) の平地レース
(ばんえい除外・5 頭以上・勝ち馬あり)。scripts/time_eval_backtest.py (netkeiba キャッシュ 2026-01〜05) の
選定・確認期間の **後** (2026-06〜) を、本番と同じ time_model.fit + time_eval.evaluate で検証する。

  - 各馬の過去走 = 公式データにある「評価レースより前の地方の走」+ ledger (netkeiba_raw / live) にある
    中央の走 (新しい順・最大 12 走 = past_runs_ext と同じ)。本番の keiba.go.jp 馬柱も中央の走を含む
    (「Ｊ函館」等)。中央の走を抜くと AUCw −0.010 (出走馬の 22%・レースの 71% が中央の走を持つ)。
    netkeiba は 2026-05-24 で止まっているので、それ以後の中央の走は欠ける (--no-jra-history で地方の走だけ)。
    クラスは racelist のレース名、馬場は当該レースの公式馬場。
  - 当レースの馬場は公式の発表値 (本番も発走前に発表済み)。
  - 学習 = 評価日の前日までの ledger 行だけ (--retrain daily, 既定 = API の日次再学習と同じ) か、
    評価月の 1 日より前 (--retrain monthly)。ledger の範囲を variant で切り替える:
        nk        netkeiba_raw のみ (公式データ導入前の本番相当: 2026-06 以降の開催日の馬場差 δ が無い)
        nk+off05  netkeiba_raw + 公式 2026-05 以降 (規制後の穴埋めだけ)
        nk+off    netkeiba_raw + 公式 全月 (学習窓ぶん。build_time_model.py の既定)
        off       公式のみ (中央の走を換算できない = netkeiba が要るかの確認)

  python scripts/time_eval_official_backtest.py [--variants nk,nk+off05,nk+off] [--months 202606,...]
                                                [--retrain daily|monthly] [--dump out.jsonl] [--no-jra-history]

指標 (scripts/time_eval_backtest.py と同じ定義): AUCw/AUC3 (レース内で勝ち馬/3着内が他馬より速く推定された
割合)・ρtime・MADdm (レース中央値を除いた推定と実時計の差の中央値, 秒 — 0.05 秒刻み)・MAE (同・平均, 連続量)・
top1 (te1 の順位 1 位の勝率)・ROI (推定 1 位の単勝 ¥100 均等買い, 公式払戻)・fav1/favROI (公式の 1 番人気)。
出走馬 = 完走馬 + 競走中止・失格の馬 (勝ち馬にも 3 着内にもならない)。出走取消・競走除外は除く。
指標の差の CI はこのスクリプトでは出さない — --dump をレース日単位の paired bootstrap で比べること。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import nar_official as no  # noqa: E402
from src import time_eval as te_mod  # noqa: E402
from src import time_model as tm  # noqa: E402
from src.models import Horse, PastRun, Race, RaceData  # noqa: E402

JST = _dt.timezone(_dt.timedelta(hours=9))


def _int(s: str) -> int:
    s = (s or "").strip()
    return int(s) if s.isdigit() else 0


def load_official(months: list[str]) -> tuple[dict, dict, dict]:
    """公式 ZIP 群 → (races {key: info}, entries {key: [row]}, payouts {key: {馬番: 単勝払戻円}})。"""
    races: dict = {}
    entries: dict = defaultdict(list)
    pays: dict = defaultdict(dict)
    for ym in months:
        p = no.monthly_zip_path(ym)
        if not p.exists():
            print(f"  (skip {ym}: {p} がありません — python -m src.nar_official sync)")
            continue
        t = no.read_zip(p)
        races.update(no.race_infos(t))
        for r in t.get("horselist", []):
            key = (r.get("競馬場", ""), _int(r.get("競走年月日", "")), _int(r.get("レース番号", "")))
            entries[key].append(r)
        for r in t.get("payback", []):
            key = (r.get("競馬場", ""), _int(r.get("競走年月日", "")), _int(r.get("レース番号", "")))
            num, yen = _int(r.get("単勝組番", "")), _int(r.get("単勝払戻金（円）", ""))
            if num and yen:
                pays[key][num] = yen
    return races, entries, pays


def jra_ledger_histories() -> dict[str, list[tuple[int, PastRun]]]:
    """ledger (netkeiba_raw / live) の中央の走 → 馬名 → [(日付, PastRun)] (本番の馬柱に混ざる中央の走の代わり)。"""
    files = [tm.LEDGER_DIR / "netkeiba_raw.jsonl", *sorted(tm.LEDGER_DIR.glob("live-*.jsonl"))]
    out: dict[str, list[tuple[int, PastRun]]] = defaultdict(list)
    for r in tm.load_ledger([f for f in files if f.exists()]):
        if r["v"] not in tm.JRA_VENUES:
            continue
        d = int(r["d"])
        out[r["h"]].append((d, PastRun(
            date=f"{d // 10000:04d}.{d // 100 % 100:02d}.{d % 100:02d}", venue=r["v"], surface=r["s"],
            distance=int(r["dist"]), going={"良": "良", "稍": "稍重", "重": "重", "不": "不良"}.get(r["g"], ""),
            winner_time_sec=float(r["t"]), time_diff_sec=0.0)))
    return out


def build_histories(races: dict, entries: dict, extra: dict | None = None) -> dict[str, list[tuple[int, PastRun]]]:
    """馬名 → [(日付, PastRun)] (古い順)。走破時計が取れた完走のみ (ledger と同じ条件)。extra は中央の走。"""
    hist: dict[str, list[tuple[int, PastRun]]] = defaultdict(list)
    for h, runs in (extra or {}).items():
        hist[h].extend(runs)
    for key, rows in entries.items():
        info = races.get(key)
        if not info or key[0] in no.BANEI_VENUES:
            continue
        d = info["date"]
        for r in rows:
            fin = (r.get("着順", "") or "").strip()
            sec = no.parse_time(r.get("タイム", ""))
            if not fin.isdigit() or sec is None:
                continue
            f = int(fin)
            hist[tm.norm_horse(r.get("馬名", ""))].append((d, PastRun(
                date=f"{d // 10000:04d}.{d // 100 % 100:02d}.{d % 100:02d}", venue=info["venue"],
                race_no=info["race_no"], race_class=info["race_name"], surface=info["surface"],
                distance=int(info["distance"]), going=info["going"], field_size=int(info["field_size"]),
                horse_number=_int(r.get("馬番", "")), popularity=_int(r.get("人気", "")),
                winner_time_sec=sec, time_diff_sec=0.0, finish_pos=f if f in (1, 2, 3) else None)))
    for k in hist:
        hist[k].sort(key=lambda x: x[0])
    return hist


def eval_races(races: dict, entries: dict, pays: dict, months: list[str]) -> list[dict]:
    """評価対象レース (平地・5 頭以上・勝ち馬あり) を日付順に。"""
    out = []
    for key, info in races.items():
        if key[0] in no.BANEI_VENUES or info["surface"] not in ("芝", "ダート") or info["distance"] <= 0:
            continue
        if f"{info['date'] // 100:06d}" not in months:
            continue
        runners = []
        for r in entries.get(key, []):
            fin = (r.get("着順", "") or "").strip()
            sec = no.parse_time(r.get("タイム", ""))
            margin = (r.get("着差", "") or "").strip()
            if fin.isdigit():
                runners.append({"num": _int(r.get("馬番", "")), "name": r.get("馬名", ""), "fin": int(fin),
                                "sec": sec, "pop": _int(r.get("人気", ""))})
            elif margin in ("競走中止", "失格"):
                # 出走した (本番の te1 は評価する) が完走扱いにならない馬: 勝ち馬にも 3 着内にもしない
                runners.append({"num": _int(r.get("馬番", "")), "name": r.get("馬名", ""), "fin": None,
                                "sec": None, "pop": _int(r.get("人気", ""))})
        if len(runners) < 5 or not any(x["fin"] == 1 for x in runners):
            continue
        out.append({"key": key, "info": info, "runners": runners, "pays": pays.get(key, {})})
    out.sort(key=lambda x: (x["info"]["date"], x["key"][0], x["key"][2]))
    return out


def race_data(er: dict, hist: dict) -> RaceData:
    info = er["info"]
    d = info["date"]
    hh, mm = (int(x) for x in (info["start_hhmm"] or "12:00").split(":"))
    start = int(_dt.datetime(d // 10000, d // 100 % 100, d % 100, hh, mm, tzinfo=JST).timestamp())
    horses = []
    for x in er["runners"]:
        runs = [pr for dd, pr in hist.get(tm.norm_horse(x["name"]), []) if dd < d]
        runs = list(reversed(runs))[:12]
        horses.append(Horse(number=x["num"], name=x["name"], past_runs=runs[:5], past_runs_ext=runs))
    race = Race(cup_id="", schedule_index=0, race_number=info["race_no"], venue_id=0,
                venue_name=info["venue"], race_class=info["race_name"], distance=int(info["distance"]),
                surface=info["surface"], direction=info["direction"],
                weather_text=f"{info['weather']} / {info['going']}" if info["going"] else "",
                start_at=start, horses=horses)
    return RaceData(race=race, trifecta=[], other_bets={})


def ledger_for(variant: str) -> list[dict]:
    nk = tm.LEDGER_DIR / "netkeiba_raw.jsonl"
    offs = sorted(tm.LEDGER_DIR.glob("nar_official-*.jsonl"))
    if variant == "nk":
        files = [nk]
    elif variant == "nk+off05":
        files = [p for p in offs if p.stem.split("-")[-1] >= "202605"] + [nk]
    elif variant == "nk+off":
        files = offs + [nk]
    elif variant == "off":
        files = offs
    else:
        raise SystemExit(f"unknown variant {variant}")
    return tm.load_ledger(files)


def _auc(sc, lab):
    w = [s for s, f in zip(sc, lab) if f]
    o = [s for s, f in zip(sc, lab) if not f]
    if not w or not o:
        return None
    return sum((1.0 if a < b else 0.5 if a == b else 0.0) for a in w for b in o) / (len(w) * len(o))


def score_race(er: dict, te: dict) -> dict | None:
    """1 レースの評価値。est が 3 頭未満なら None (scripts/time_eval_backtest.py と同じ足切り)。"""
    if not te.get("available"):
        return None
    est = {h["number"]: h["est_sec"] for h in te["horses"] if h["est_sec"] is not None}
    runners = [x["num"] for x in er["runners"]]
    if len(runners) < 5 or len(est) < 3:
        return None
    winners = {x["num"] for x in er["runners"] if x["fin"] == 1}
    top3 = {x["num"] for x in er["runners"] if x["fin"] is not None and x["fin"] <= 3}
    med = float(np.median(list(est.values())))
    sc = [est.get(n, med) for n in runners]
    out = {"aucw": _auc(sc, [n in winners for n in runners]), "auc3": _auc(sc, [n in top3 for n in runners])}
    times = {x["num"]: x["sec"] for x in er["runners"] if x["sec"]}
    pairs = [(est[n], times[n]) for n in runners if n in est and n in times]
    if len(pairs) >= 4:
        pe, ac = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        out["mad"] = np.abs((pe - np.median(pe)) - (ac - np.median(ac))).tolist()
        out["mae"] = np.abs((pe - pe.mean()) - (ac - ac.mean())).tolist()
        ra, rb = np.argsort(np.argsort(pe)), np.argsort(np.argsort(ac))
        out["rho"] = float(np.corrcoef(ra, rb)[0, 1]) if np.std(ra) and np.std(rb) else None
    # te1 の順位 1 位 (丸める前の推定で付いた rank)。丸めた est の最小 + 馬番で選ぶと同タイムで別の馬になる
    top = next((h["number"] for h in te["horses"] if h.get("rank") == 1), None) or min(est, key=lambda n: (est[n], n))
    out["top1"] = 1.0 if top in winners else 0.0
    out["pay"] = float(er["pays"].get(top, 0)) if top in winners else 0.0
    favs = [x for x in er["runners"] if x["pop"] == 1]
    if favs:
        fav = favs[0]["num"]
        out["fav"] = 1.0 if fav in winners else 0.0
        out["favpay"] = float(er["pays"].get(fav, 0)) if fav in winners else 0.0
    out["known_day_share"] = te.get("_known_share")
    return out


def summarize(rows: list[dict]) -> dict:
    A = defaultdict(list)
    for r in rows:
        if not r:
            continue
        A["races"].append(1)
        for k in ("aucw", "auc3", "rho", "top1", "pay", "fav", "favpay", "known_day_share"):
            if r.get(k) is not None:
                A[k].append(r[k])
        A["mad"].extend(r.get("mad") or [])
        A["mae"].extend(r.get("mae") or [])
    m = lambda k: float(np.mean(A[k])) if A[k] else float("nan")  # noqa: E731
    return {"races": len(A["races"]), "aucw": m("aucw"), "auc3": m("auc3"), "rho": m("rho"),
            "mad": float(np.median(A["mad"])) if A["mad"] else float("nan"), "mae": m("mae"), "top1": m("top1"),
            "roi": float(np.sum(A["pay"]) / max(1, 100 * len(A["top1"])) * 100),
            "fav": m("fav"), "favroi": float(np.sum(A["favpay"]) / max(1, 100 * len(A["fav"])) * 100),
            "known_day": m("known_day_share")}


def run_variant(variant: str, evr: list[dict], hist: dict, retrain: str, dump=None) -> dict:
    ledger = ledger_for(variant)
    t0 = time.time()
    by_day: dict[int, list[dict]] = defaultdict(list)
    for er in evr:
        by_day[er["info"]["date"]].append(er)
    res_by_month: dict[str, list] = defaultdict(list)
    model = None
    model_key = None
    n_fit_rows = -1
    for d in sorted(by_day):
        key = d if retrain == "daily" else d // 100
        if key != model_key:
            lim = d if retrain == "daily" else (d // 100) * 100 + 1
            sub = [r for r in ledger if r["d"] < lim]
            if len(sub) != n_fit_rows:       # 行は日付順に増えるだけ = 件数が同じなら同じ集合 (nk は 06 月以降不変)
                model = tm.fit(sub)
                n_fit_rows = len(sub)
            model_key = key
        for er in by_day[d]:
            rd = race_data(er, hist)
            te = te_mod.evaluate(rd, model)
            # 推定に使った走のうち「開催日の馬場差 δ を推定済み」の割合 (live の δ 被覆の目安)
            used = [r for h in te.get("horses", []) for r in h.get("runs", []) if r.get("weight")]
            if used:
                te["_known_share"] = sum(1 for r in used if r.get("day_adj_sec") is not None) / len(used)
            s = score_race(er, te)
            res_by_month[f"{d // 100:06d}"].append(s)
            if dump is not None and te.get("available"):
                dump.write(json.dumps({"variant": variant, "key": list(er["key"]),
                                       "est": {h["number"]: h["est_sec"] for h in te["horses"]},
                                       "index": {h["number"]: h["index"] for h in te["horses"]},
                                       "rank": {h["number"]: h["rank"] for h in te["horses"]},
                                       "runners": er["runners"], "pays": er["pays"]},
                                      ensure_ascii=False) + "\n")
    out = {"all": summarize([r for v in res_by_month.values() for r in v])}
    out.update({m: summarize(v) for m, v in sorted(res_by_month.items())})
    out["_sec"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="nk,nk+off05,nk+off")
    ap.add_argument("--months", default="202606,202607,202608,202609")
    ap.add_argument("--history-from", default="202504", help="過去走に使う公式データの最初の月")
    ap.add_argument("--retrain", choices=("daily", "monthly"), default="daily")
    ap.add_argument("--dump", default=None, help="各レースの推定を JSONL に書き出す (市場比較などの後段用)")
    ap.add_argument("--no-jra-history", action="store_true", help="過去走に中央の走を入れない (地方の走だけ)")
    args = ap.parse_args()
    months = args.months.split(",")
    hist_months = no.months_between(args.history_from, max(months))
    t0 = time.time()
    races, entries, pays = load_official(hist_months)
    hist = build_histories(races, entries, None if args.no_jra_history else jra_ledger_histories())
    evr = eval_races(races, entries, pays, months)
    print(f"official: {len(races):,} races / {len(hist):,} horses; eval races {len(evr):,} "
          f"({time.time() - t0:.0f}s)", flush=True)
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None
    results = {}
    for v in args.variants.split(","):
        results[v] = run_variant(v, evr, hist, args.retrain, dump)
        print(f"variant {v}: done ({results[v]['_sec']}s)", flush=True)
    if dump:
        dump.close()
    hdr = f"{'variant':<10}{'period':<8}{'races':>6}{'AUCw':>7}{'AUC3':>7}{'ρtime':>7}{'MADdm':>7}{'MAE':>7}{'top1':>7}{'ROI':>7}{'fav1':>7}{'favROI':>8}{'δknown':>8}"
    print("\n" + hdr)
    for v, res in results.items():
        for period in ["all"] + months:
            m = res.get(period)
            if not m:
                continue
            print(f"{v:<10}{period:<8}{m['races']:>6}{m['aucw']:>7.3f}{m['auc3']:>7.3f}{m['rho']:>7.3f}"
                  f"{m['mad']:>7.3f}{m['mae']:>7.3f}{m['top1']:>7.3f}{m['roi']:>7.1f}{m['fav']:>7.3f}"
                  f"{m['favroi']:>8.1f}{m['known_day']:>8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
