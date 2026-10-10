"""タイム換算比較 (te1): 各馬の過去走の走破時計を当レースの条件 (場・芝ダ・距離・馬場状態) に換算して比べる。

ユーザ指示 (2026-10-10):「予測分析の仕方をガラっとかえる。馬の過去の走りから、当レース・馬場の距離と
同じレースの時計がどれくらいで、他の距離、馬場を当レースの距離・馬場に換算するとどれくらいの評価が
つくか自動で計算して出力して比較する」。

  - 同条件 (同じ場・芝ダ・距離) の走は生の時計をそのまま並べる (同条件ベスト)。
  - それ以外の走は time_model の条件効果で当レース条件の時計に換算する (開催日の馬場差も補正)。
  - 推定タイム = 換算時計を「新しい走ほど・距離が近いほど・同じ芝ダほど・同じ場ほど」重く平均 (直近 5 走)。
    この集約と換算モデルは 2026-10-10 の walk-forward 検証で選定 (scripts/time_eval_backtest.py)。
  - 市場 (オッズ・人気) は一切使わない = 市場・Claude 指数と独立に比較できる。

出力 (snapshot の "time_eval" / Web の TimeEvalCard / CLI 表) は全て丸め済み・NaN なし。
"""
from __future__ import annotations

import math
import sys
from typing import Any

from . import time_model as tm

# 推定タイムの集約 (直近 5 走の重み付き平均)。重みは事前に決めた値 (検証で調整していない)。
EST_RUNS = 5
RECENCY_DECAY = 0.85        # 1 走古くなるごとの重み
DIST_SCALE = 0.2            # 距離の近さ: exp(-|ln(当距離/走距離)| / 0.2)
CROSS_SURFACE_W = 0.35      # 芝↔ダの換算は信頼度が低い
SAME_VENUE_W = 1.15
INDEX_PER_SEC = 10.0        # タイム指数: レース中央値 50、1 秒速いごとに +10
SHOW_RUNS = 12              # 明細に出す過去走の最大数


def _r1(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), 1)


def _fmt_date(d: int) -> str:
    return f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}" if d else ""


def target_of(race: Any) -> dict:
    """当レースの条件 {venue, surface, distance, going, going_source}。"""
    venue = tm.norm_venue(getattr(race, "venue_name", ""))
    surface = tm.norm_surface(getattr(race, "surface", ""), "")
    distance = int(getattr(race, "distance", 0) or 0)
    going = ""
    w = getattr(race, "weather", None)
    if w is not None and getattr(w, "track_condition", ""):
        going = tm.norm_going(w.track_condition)
    if not going:
        wt = getattr(race, "weather_text", "") or ""
        if "/" in wt:
            going = tm.norm_going(wt.split("/")[-1])
    return {"venue": venue, "surface": surface, "distance": distance,
            "going": going or "良", "going_source": "announced" if going else "assumed"}


def _unavailable(target: dict, reason: str, model: dict | None = None) -> dict:
    return {"version": tm.VERSION, "available": False, "reason": reason,
            "target": {**target, "par_sec": None, "par_n": None},
            "model": _model_meta(model), "horses": []}


def _model_meta(model: dict | None) -> dict | None:
    if not model:
        return None
    return {"fitted_at": model.get("fitted_at"), "data_until": model.get("data_until"),
            "n_runs": model.get("n_runs"),
            "day_effects": bool((model.get("params") or {}).get("day_effects"))}


def _run_record(pr: Any, tgt: dict, model: dict, lt: float) -> tuple[dict, float | None, int]:
    """1 走 → (表示用 dict, 換算時計 or None, 日付)。"""
    venue = tm.norm_venue(getattr(pr, "venue", ""))
    race_class = getattr(pr, "race_class", "") or ""
    surface = tm.norm_surface(getattr(pr, "surface", ""), race_class)
    distance = int(getattr(pr, "distance", 0) or 0)
    going = tm.norm_going(getattr(pr, "going", ""))
    own = float(getattr(pr, "own_time_sec", 0.0) or 0.0)
    d = tm.date_key(getattr(pr, "date", ""))
    fp = getattr(pr, "finish_pos", None)
    rec = {"date": _fmt_date(d), "venue": venue, "surface": surface, "distance": distance,
           "going": going, "race_class": race_class or None,
           "finish_pos": fp if fp in (1, 2, 3) else None,
           "own_sec": _r1(own) if own > 0 else None, "conv_sec": None, "day_adj_sec": None,
           "kind": "excluded", "weight": None, "note": None}
    if own <= 0:
        rec["note"] = "時計なし"
    elif surface == "障害":
        rec["note"] = "障害"
    elif not surface or venue == "帯広":
        rec["note"] = "ばんえい/不明"
    elif not tm.is_known_venue(venue):
        rec["note"] = "海外/不明の場"
    elif not tm.valid_flat_time(surface, distance, own):
        rec["note"] = "時計異常"
    else:
        lr = tm.log_effect(model, surface, distance, venue, going)
        if lr is None:
            rec["note"] = "換算データなし"
        else:
            dlt = tm.day_effect(model, venue, d, surface)
            conv = own * math.exp(lt - lr - dlt)
            rec["conv_sec"] = _r1(conv)
            rec["day_adj_sec"] = _r1(own * math.exp(lt - lr) - conv) if dlt else 0.0
            if surface != tgt["surface"]:
                rec["kind"] = "surface"
            elif distance != tgt["distance"]:
                rec["kind"] = "distance"
            elif venue != tgt["venue"]:
                rec["kind"] = "same_dist"
            else:
                rec["kind"] = "same"
            return rec, conv, d
    return rec, None, d


def _weight(i: int, rec: dict, tgt: dict) -> float:
    w = RECENCY_DECAY ** i
    w *= math.exp(-abs(math.log(tgt["distance"] / rec["distance"])) / DIST_SCALE)
    if rec["surface"] != tgt["surface"]:
        w *= CROSS_SURFACE_W
    if rec["venue"] == tgt["venue"]:
        w *= SAME_VENUE_W
    return w


def _confidence(used: list[dict], tgt: dict) -> str:
    """high: 3 走以上 + 同じ芝ダで距離が近い (±15%) 走あり / mid: 2 走以上で同じ芝ダあり / low: それ以外。"""
    if not used:
        return "none"
    same_surface = [r for r in used if r["kind"] != "surface"]
    near = [r for r in same_surface if abs(math.log(tgt["distance"] / r["distance"])) <= 0.15]
    if len(used) >= 3 and near:
        return "high"
    if len(used) >= 2 and same_surface:
        return "mid"
    return "low"


def evaluate(rd: Any, model: dict | None = None) -> dict:
    """RaceData → time_eval dict (snapshot / Web / CLI 共通)。例外は投げない設計 (呼び出し側でも保護)。"""
    race = getattr(rd, "race", rd)
    tgt = target_of(race)
    if model is None:
        model = tm.load_model()
    if tgt["venue"] == "帯広" or str(getattr(race, "venue_id", "")) == "65":
        return _unavailable(tgt, "ばんえいは換算対象外 (200m 直線・馬場は水分量表記)", model)
    if tgt["surface"] == "障害":
        return _unavailable(tgt, "障害レースは換算対象外", model)
    if tgt["surface"] not in ("芝", "ダート") or tgt["distance"] <= 0:
        return _unavailable(tgt, "当レースの距離・芝ダが不明 (出馬表ヘッダ未取得)", model)
    if not model or not model.get("theta"):
        return _unavailable(tgt, "換算モデル未生成 (make time-model を実行してください)", model)
    lt = tm.log_effect(model, tgt["surface"], tgt["distance"], tgt["venue"], tgt["going"])
    if lt is None:
        return _unavailable(tgt, f"{tgt['venue']}の換算データがありません", model)

    # 安全策: 当日以降の走は使わない (各経路の leakage フィルタの二重化。過去レース再解析時の結果混入防止)
    race_day = 0
    sa = int(getattr(race, "start_at", 0) or 0)
    if sa > 0:
        import datetime as _dt
        jd = _dt.datetime.fromtimestamp(sa, _dt.timezone(_dt.timedelta(hours=9))).date()
        race_day = jd.year * 10000 + jd.month * 100 + jd.day

    rows = []
    for h in getattr(race, "horses", []) or []:
        if getattr(h, "absent", False):
            continue
        runs = list(getattr(h, "past_runs_ext", None) or getattr(h, "past_runs", None) or [])
        recs: list[dict] = []
        used: list[tuple[float, float, dict]] = []
        valid_i = 0
        if race_day:
            runs = [pr for pr in runs if 0 < tm.date_key(getattr(pr, "date", "")) < race_day]
        for pr in runs[:SHOW_RUNS]:
            rec, conv, _d = _run_record(pr, tgt, model, lt)
            recs.append(rec)
            # 推定に使うのは「時計が妥当な平地走」の新しい順 5 本 (検証と同じ数え方: 換算不可でも枠は消費)
            counted = rec["note"] not in ("時計なし", "障害", "ばんえい/不明", "海外/不明の場", "時計異常")
            if counted:
                if valid_i < EST_RUNS and conv is not None:
                    w = _weight(valid_i, rec, tgt)
                    rec["weight"] = round(w, 3)
                    used.append((w, conv, rec))
                valid_i += 1
        est = (sum(w * c for w, c, _ in used) / sum(w for w, _, _ in used)) if used else None
        same = [r for r in recs if r["kind"] == "same" and r["own_sec"]]
        same_best = min(same, key=lambda r: r["own_sec"]) if same else None
        convs = [r["conv_sec"] for r in recs if r["conv_sec"] is not None]
        conf = _confidence([r for _, _, r in used], tgt)
        rows.append({
            "number": int(getattr(h, "number", 0) or 0), "name": getattr(h, "name", ""),
            "rank": None, "est_sec": est, "gap_sec": None, "index": None,
            "same_n": len(same), "same_best_sec": same_best["own_sec"] if same_best else None,
            "same_best_going": same_best["going"] if same_best else None,
            "conv_best_sec": _r1(min(convs)) if convs else None,
            "n_runs": len(recs), "n_used": len(used), "confidence": conf, "runs": recs,
        })

    ests = sorted(r["est_sec"] for r in rows if r["est_sec"] is not None)
    if ests:
        best = ests[0]
        mid = ests[len(ests) // 2] if len(ests) % 2 else (ests[len(ests) // 2 - 1] + ests[len(ests) // 2]) / 2
        ranked = sorted((r for r in rows if r["est_sec"] is not None), key=lambda r: (r["est_sec"], r["number"]))
        for k, r in enumerate(ranked, 1):
            r["rank"] = k
            r["gap_sec"] = _r1(r["est_sec"] - best)
            r["index"] = _r1(max(0.0, min(100.0, 50.0 + INDEX_PER_SEC * (mid - r["est_sec"]))))
    for r in rows:
        r["est_sec"] = _r1(r["est_sec"])
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["number"]))
    pw = tm.par_win(model, tgt["surface"], tgt["distance"], tgt["venue"],
                    tgt["going"] if tgt["going_source"] == "announced" else "")
    return {"version": tm.VERSION, "available": True, "reason": None,
            "target": {**tgt, "par_sec": _r1(pw[0]) if pw else None, "par_n": pw[1] if pw else None},
            "model": _model_meta(model), "horses": rows}


def time_index(te: dict | None) -> dict[int, float]:
    """{馬番: タイム指数} (順位を使う側向け。provisional_index と同じ形)。"""
    if not te or not te.get("available"):
        return {}
    return {h["number"]: h["index"] for h in te.get("horses", []) if h.get("index") is not None}


# ------------------------------------------------------------ CLI ----

def fmt_time(sec: float | None) -> str:
    """秒 → "M:SS.s" (60 秒未満は "SS.s")。0.1 秒に丸めてから分を切る (59.96 → "1:00.0")。"""
    if sec is None or not math.isfinite(sec) or sec < 0:
        return "—"
    tenths = int(round(float(sec) * 10))
    if tenths < 600:
        return f"{tenths / 10:.1f}"
    m, rest = divmod(tenths, 600)
    return f"{m}:{rest / 10:04.1f}"


_KIND_LABEL = {"same": "同条件", "same_dist": "同距離", "distance": "距離換算",
               "surface": "芝ダ換算", "excluded": "対象外"}


def print_time_eval(te: dict, console: Any = None, *, detail: bool = False) -> None:
    from rich.console import Console
    from rich.table import Table
    con = console or Console()
    tgt = te.get("target") or {}
    cond = f"{tgt.get('venue', '')} {tgt.get('surface', '')}{tgt.get('distance', 0)}m {tgt.get('going', '')}"
    if tgt.get("going_source") == "assumed":
        cond += " (馬場未発表→良で換算)"
    if not te.get("available"):
        con.print(f"[bold]タイム換算比較[/bold] {cond}: [dim]{te.get('reason')}[/dim]")
        return
    t = Table(title=f"タイム換算比較 — {cond}", show_lines=False)
    for col in ("順", "馬", "馬名", "推定", "差", "指数", "同条件", "換算ベスト", "走", "信頼"):
        t.add_column(col, justify="right" if col not in ("馬名", "信頼", "同条件") else "left")
    for h in te.get("horses", []):
        same = (f"{h['same_n']}走 {fmt_time(h['same_best_sec'])}({h.get('same_best_going') or ''})"
                if h["same_n"] else "—")
        t.add_row(str(h["rank"] or "—"), str(h["number"]), h["name"], fmt_time(h["est_sec"]),
                  f"+{h['gap_sec']:.1f}" if h.get("gap_sec") else ("—" if h["rank"] != 1 else "0.0"),
                  f"{h['index']:.0f}" if h.get("index") is not None else "—",
                  same, fmt_time(h["conv_best_sec"]), f"{h['n_used']}/{h['n_runs']}",
                  {"high": "高", "mid": "中", "low": "低", "none": "—"}[h["confidence"]])
    con.print(t)
    if tgt.get("par_sec"):
        con.print(f"[dim]参考: この条件の平均的な勝ち時計 {fmt_time(tgt['par_sec'])} (n={tgt.get('par_n')})[/dim]")
    if detail:
        for h in te.get("horses", []):
            con.print(f"[bold]{h['number']} {h['name']}[/bold]  推定 {fmt_time(h['est_sec'])}")
            for r in h["runs"]:
                conv = fmt_time(r["conv_sec"]) if r["conv_sec"] is not None else "—"
                con.print(f"   {r['date']} {r['venue']} {r['surface'][:1]}{r['distance']} {r['going'] or '-'}"
                          f" {fmt_time(r['own_sec'])} → {conv} [{_KIND_LABEL[r['kind']]}"
                          f"{(':' + r['note']) if r.get('note') else ''}]")


def _cli(argv: list[str]) -> int:
    """python -m src.time_eval <netkeiba_rid> [--json] [--detail]  (data/raw の netkeiba キャッシュから)"""
    import json
    if not argv or argv[0].startswith("-"):
        print(_cli.__doc__)
        return 2
    rid = argv[0]
    from .dataset import load_race
    try:
        loaded = load_race(rid)
    except Exception as e:  # noqa: BLE001
        print(f"load_race 失敗: {e}")
        return 1
    if loaded is None:
        print(f"{rid}: data/raw にキャッシュがありません")
        return 1
    rd, _res = loaded
    te = evaluate(rd)
    if "--json" in argv:
        print(json.dumps(te, ensure_ascii=False, indent=1, allow_nan=False))
    else:
        print_time_eval(te, detail="--detail" in argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
