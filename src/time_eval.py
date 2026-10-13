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
            "model": _model_meta(model, target.get("venue", "")), "horses": []}


def _model_meta(model: dict | None, venue: str = "") -> dict | None:
    """表示用のモデル情報。data_until は当レースの circuit (中央/地方) の最新データ日 — 地方は公式データで
    当月まで揃う一方、中央は netkeiba キャッシュ + 解析時の live 行だけなので、全体の最新日を出すと中央の
    レースで学習データの新しさを過大に見せてしまう (2026-10-13)。"""
    if not model:
        return None
    by_c = model.get("data_until_by_circuit") or {}
    until = by_c.get(tm.circuit(venue)) if venue else None
    return {"fitted_at": model.get("fitted_at"), "data_until": until or model.get("data_until"),
            "n_runs": model.get("n_runs"),
            "day_effects": bool((model.get("params") or {}).get("day_effects"))}


def _run_record(pr: Any, tgt: dict, model: dict, lt: float) -> tuple[dict, float | None, int]:
    """1 走 → (表示用 dict, 換算時計 or None, 日付)。"""
    venue = tm.norm_venue(getattr(pr, "venue", ""))
    race_class = getattr(pr, "race_class", "") or ""
    distance = int(getattr(pr, "distance", 0) or 0)
    surface = tm.norm_surface(getattr(pr, "surface", ""), race_class, distance)
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
            # 馬場差を推定できた開催日だけ補正量を出す。ledger に無い日 (例: netkeiba 規制後で live
            # ledger 未蓄積の日) は None = 「未推定」(中立の 0.0 と区別する)。
            known = f"{venue}|{d}|{surface}" in (model.get("delta") or {})
            rec["day_adj_sec"] = _r1(own * math.exp(lt - lr) - conv) if known else None
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
            # 推定に使うのは「時計が妥当な平地走」の新しい順 5 本 (換算データの無いコースでも枠は消費)。
            # 検証ハーネスとの違いは海外/不明の場の走を枠に数えない点のみ (1.5 万頭中 3 頭が変わる程度)
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

    _assign_ranks(rows)
    for r in rows:
        r["est_sec"] = _r1(r["est_sec"])
    pw = tm.par_win(model, tgt["surface"], tgt["distance"], tgt["venue"],
                    tgt["going"] if tgt["going_source"] == "announced" else "")
    return {"version": tm.VERSION, "available": True, "reason": None,
            "target": {**tgt, "par_sec": _r1(pw[0]) if pw else None, "par_n": pw[1] if pw else None},
            "model": _model_meta(model, tgt["venue"]), "horses": rows}


def _assign_ranks(rows: list[dict]) -> None:
    """推定タイム (est_sec) から 順位・差・指数 を付け直し、順位順に並べる (順位なしは末尾)。"""
    for r in rows:
        r["rank"] = r["gap_sec"] = r["index"] = None
    ranked = sorted((r for r in rows if r.get("est_sec") is not None), key=lambda r: (r["est_sec"], r["number"]))
    if ranked:
        ests = [r["est_sec"] for r in ranked]
        best = ests[0]
        n = len(ests)
        mid = ests[n // 2] if n % 2 else (ests[n // 2 - 1] + ests[n // 2]) / 2
        for k, r in enumerate(ranked, 1):
            r["rank"] = k
            r["gap_sec"] = _r1(r["est_sec"] - best)
            r["index"] = _r1(max(0.0, min(100.0, 50.0 + INDEX_PER_SEC * (mid - r["est_sec"]))))
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["number"]))


def _n_ranked(te: dict | None) -> int:
    return sum(1 for h in (te or {}).get("horses", []) if h.get("rank"))


def merge_previous(cur: dict | None, prev: dict | None, runners: set[int]) -> dict | None:
    """前回 snapshot の time_eval と今回の計算を統合する (保存のたびに再計算するための劣化防止)。

    keiba.go.jp / oddspark は保存のたびに馬柱を取り直し、レート制限等で一部/全部の馬の過去走が空になり得る
    (JRA の bet 段は過去走キャッシュ頼み)。そこで:
      - 今回が評価不能 (全馬未評価・条件不明) なら前回を使う
      - 今回 過去走 0 走の馬は、前回が **同じ当レース条件** (場・芝ダ・距離・馬場状態) で評価済みなら引き継ぐ
    いずれも **runners (今回の出走馬 = 取消を除く) に居ない馬は落とし**、順位・差・指数を付け直す
    (前回の値を丸ごと返すと、後から取り消した馬が「タイム1位」のまま残っていた)。
    """
    import copy
    usable_prev = prev if (prev and prev.get("available") and _n_ranked(prev) > 0) else None
    if cur and cur.get("available") and _n_ranked(cur) > 0:
        if usable_prev and usable_prev.get("target") == cur.get("target"):
            old = {h["number"]: h for h in usable_prev.get("horses", [])}
            out = copy.deepcopy(cur)
            changed = False
            for i, h in enumerate(out["horses"]):
                p = old.get(h["number"])
                if h.get("n_runs", 0) == 0 and p and p.get("rank"):
                    out["horses"][i] = copy.deepcopy(p)
                    changed = True
            if changed:
                _assign_ranks(out["horses"])
            return out
        return cur
    if usable_prev:
        out = copy.deepcopy(usable_prev)
        if runners:
            out["horses"] = [h for h in out["horses"] if h["number"] in runners]
        _assign_ranks(out["horses"])
        return out if _n_ranked(out) > 0 else cur
    return cur


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
