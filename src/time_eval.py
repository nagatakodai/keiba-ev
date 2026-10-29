"""タイム換算比較 (te2〜te2.1): 各馬の過去走の走破時計を当レースの条件 (場・芝ダ・距離・馬場状態) に換算して比べる。

ユーザ指示 (2026-10-10):「予測分析の仕方をガラっとかえる。馬の過去の走りから、当レース・馬場の距離と
同じレースの時計がどれくらいで、他の距離、馬場を当レースの距離・馬場に換算するとどれくらいの評価が
つくか自動で計算して出力して比較する」。
ユーザ指示 (2026-10-14):「芝ダの違いや距離の違いは換算しても弱めにする。距離適性、芝ダ適性、馬場適性を
ちゃんと出す。馬場含む同条件を最重視する。同条件が無い馬は仕方が無いので換算で評価するが、弱めにする。
ダートの不良や重めは良より早くなる性質がある。このような性質もしっかり考える」。

  - 1 走の種別 (当レースとの関係):
      same        同じ場・芝ダ・距離・馬場状態 (良/稍/渋=重・不良) — 生の時計をそのまま比較できる。最重視
      same_course 同じ場・芝ダ・距離で馬場状態だけ違う — その場の馬場差 γ_v で換算
      same_dist   他場の同じ芝ダ・距離 — 場の差 θ で換算
      distance    同じ芝ダの別距離 — 距離差を換算 (弱め: 距離が離れるほど急に軽く)
      surface     芝↔ダの換算 — 最も弱い
  - 推定タイム = 換算時計の重み付き平均 (重み = 新しさ 0.85^i × 種別の重み × 距離の近さ)。使う走 = 妥当な平地走の
    新しい順 EST_RUNS 本 + それより古い 同条件・同コース の走 (te2.1〜)。証拠の重みの合計 W が小さい馬 (同条件が無く
    換算だけ) はレース中央値へ寄せる: est = (W·平均 + W0·中央値) / (W + W0)。
  - 距離適性 / 芝ダ適性 / 馬場適性 = 「当レースに近い条件の走」と「それ以外の走」の換算時計の差 (負 = 平均的な
    換算が言うより当条件で走れている = 得意)。換算モデルは条件の平均効果しか持たないので、馬ごとの得手不得手は
    ここで出す。得意/不安 は差が走ごとのばらつきの誤差を十分に超えたときだけ (z ≥ FIT_Z, te2.1〜)。
  - 馬場差は場ごとに学習 (time_model γ_v)。「ダートの道悪は速い」は中央と一部の地方 (水沢・高知・浦和・門別) で
    成り立ち、南関東・東海・兵庫では逆 (重で遅い) だったので、向きを決め打ちせずデータに語らせる。
  - 市場 (オッズ・人気) は一切使わない = 市場・Claude 指数と独立に比較できる。

出力 (snapshot の "time_eval" / Web の TimeEvalCard / CLI 表) は全て丸め済み・NaN なし。
"""
from __future__ import annotations

import math
import sys
from typing import Any

from . import time_model as tm

# ---- 集約のパラメータ (2026-10-14 の設計。値は walk-forward 検証 scripts/time_eval_agg_sweep.py で選定, 2026-10-15) ----
# 換算 (distance / surface) の弱め方は、事前に用意した候補のうち最も穏やかな conv-mild を採用
# (distance 0.7 / surface 0.25 / DIST_SCALE 0.2)。より弱い候補 (0.5 / 0.15 / 0.15 や 0.35 / 0.10 / 0.12) は
# 勝ち馬 AUC が全期間で単調に下がった (換算した時計にも勝ち馬情報がある)。詳細は CLAUDE.md の te2 節。
EST_RUNS = 8                 # 集約に使う「時計が妥当な平地走」の数 (新しい順)
RECENCY_DECAY = 0.85         # 1 走古くなるごとの重み
KIND_W = {"same": 1.0, "same_course": 0.8, "same_dist": 0.6, "distance": 0.7, "surface": 0.25}
# ↑ distance には下の距離の近さ (と同場 ×1.15) が掛かるので、同場の ±6% 以内の距離の走 (例 大井 1600 → 1650) は
#   他場の同距離走 (same_dist 0.6) より重くなる (0.7 × 0.86 × 1.15 = 0.69。境目は |ln 比| < 0.2·ln(0.805/0.6) = 0.059)。序列の逆転は設計の意図ではないが、
#   same_course / same_dist を 0.6〜1.0 で振っても検証値に差が無かったのでそのまま (2026-10-15 sweep)。
DIST_SCALE = 0.2             # 距離の近さ: exp(-|ln(当距離/走距離)| / DIST_SCALE) (distance / surface 種別のみ)
SAME_VENUE_W = 1.15          # 別距離・別芝ダでも同じ場なら少し重く
SHRINK_W0 = 0.3              # 証拠の重み W がこれと同程度以下の馬はレース中央値へ寄る (W0/(W+W0) だけ)
STRONG_W = 1.0               # 寄せ先の中央値を作る「証拠が十分な馬」の W の下限
NEAR_DIST = 0.10             # 距離適性の「同距離帯」: |ln(当距離/走距離)| <= 0.10 (1400m なら 1270〜1545m)
FIT_SEC = 0.3                # 適性ラベルのしきい値 (秒): 差 <= -0.3 得意 / >= +0.3 不安 (かつ下の z を満たすとき)
FIT_SIGMA = 0.011            # 同じ馬の換算時計の走ごとのばらつき (時計比。地方公式 2026-09 の実測 1.08% ≒ 90 秒で 1.0 秒)
FIT_Z = 1.645                # 適性ラベルに要る z = 差 / (FIT_SIGMA·時計·√(1/近い走数 + 1/それ以外の走数))。同じ馬の走を
                             # ランダムに 2 群へ分けた差 (= 適性差なし) でラベルが付く率は ±0.3 秒だけだと 65%、z 1.645 で 10%
INDEX_PER_SEC = 10.0         # タイム指数: レース中央値 50、1 秒速いごとに +10
SHOW_RUNS = 12               # 明細・適性に使う過去走の最大数
SAME_ALL_RUNS = True         # 同条件・同コースの走は EST_RUNS の枠外でも (SHOW_RUNS まで・新しさの減衰付きで) 使う (te2.1。
                             # te2 は枠外を捨てていたので、直近 8 走が換算走で埋まった馬は 同条件 列に出る走が推定に入らなかった)

_PARAM_NAMES = ("EST_RUNS", "RECENCY_DECAY", "KIND_W", "DIST_SCALE", "SAME_VENUE_W", "SHRINK_W0",
                "STRONG_W", "NEAR_DIST", "FIT_SEC", "FIT_SIGMA", "FIT_Z", "SAME_ALL_RUNS", "SHOW_RUNS", "INDEX_PER_SEC")


def params() -> dict:
    """現在の集約パラメータ (検証スクリプト・表示用)。"""
    return {k: (dict(globals()[k]) if isinstance(globals()[k], dict) else globals()[k]) for k in _PARAM_NAMES}


def set_params(**kw: Any) -> None:
    """集約パラメータを差し替える (walk-forward 検証のスイープ用。本番コードからは呼ばない)。"""
    for k, v in kw.items():
        if k not in _PARAM_NAMES:
            raise KeyError(k)
        globals()[k] = dict(v) if isinstance(v, dict) else v


# ---- 版ごとの設定 (事前登録の判定と検証スクリプト用。凍結 — 定義を変えるなら新しい名前で登録し直す) ----
#   te1   = 2026-10-13 凍結の te1 市場ロジット事前登録 (te1 の重み・中央値寄せなし・馬場状態の効果は中央/地方 1 本)
#   te2   = 2026-10-15 凍結の te2 事前登録 (te2 の集約 + γ_v の旧推定 GOING_FIT="transfer")
#   te2.1 = 2026-10-29 凍結・本番 (γ_v を設計どおりに縮約・δ の水準合わせで θ が漂流しない・枠外の同条件走・適性に z)
# 値はすべて literal で、換算モデルの学習の定数 (time_model) も全部持つ = 本番の既定を後で変えても過去の版は動かない。
# 本番の既定 = 最新の版 (tests/test_time_eval.py が一致を確認する: 既定を変えるなら新しい版を足して登録し直す)。
# 版で止められるのは定数だけ — 判定のコード (正規化・種別・寄せ方) を変えると過去の版も変わるので、変えるときは
# 過去の版で同じ値が出ることを確かめる (GOING_FIT のように分岐を残す)。
# FIT_SIGMA / FIT_Z は適性ラベルにだけ効く (推定タイム・順位・指数は変えない)。te1 / te2 は当時の ±0.3 秒だけの判定。
_TE2_PARAMS = {"EST_RUNS": 8, "RECENCY_DECAY": 0.85,
               "KIND_W": {"same": 1.0, "same_course": 0.8, "same_dist": 0.6, "distance": 0.7, "surface": 0.25},
               "DIST_SCALE": 0.2, "SAME_VENUE_W": 1.15, "SHRINK_W0": 0.3, "STRONG_W": 1.0, "NEAR_DIST": 0.10,
               "FIT_SEC": 0.3, "FIT_SIGMA": 0.011, "FIT_Z": 0.0, "SAME_ALL_RUNS": False, "SHOW_RUNS": 12,
               "INDEX_PER_SEC": 10.0}
_FIT_CONSTS = {"K": 8.0, "K_DAY": 3.0, "K_GOING": 30.0, "WINDOW_DAYS": 540, "MAX_BEHIND": 3.0,
               "DENSE_MIN_ROWS": 50, "MAX_ITERS": 500, "TOL": 1e-7}       # 3 版とも同じ (10-13 / 10-15 の commit で確認)
PRESETS: dict[str, dict] = {
    "te1": {"params": {**_TE2_PARAMS, "EST_RUNS": 5, "SHRINK_W0": 0.0,
                       "KIND_W": {"same": 1.15, "same_course": 1.15, "same_dist": 1.0, "distance": 1.0, "surface": 0.35}},
            "model": {**_FIT_CONSTS, "VENUE_GOING": False, "GOING_FIT": "transfer"}},
    "te2": {"params": dict(_TE2_PARAMS),
            "model": {**_FIT_CONSTS, "VENUE_GOING": True, "GOING_FIT": "transfer"}},
    "te2.1": {"params": {**_TE2_PARAMS, "FIT_Z": 1.645, "SAME_ALL_RUNS": True},
              "model": {**_FIT_CONSTS, "VENUE_GOING": True, "GOING_FIT": "shrink"}},
}


def use_preset(name: str) -> None:
    """集約 (この module) と換算モデルの学習設定 (time_model の定数) を版 name に揃える。以後の tm.fit と evaluate が
    その版で動く (検証スクリプト用。本番コードからは呼ばない)。"""
    p = PRESETS[name]
    set_params(**p["params"])                 # dict の値は set_params が複製する (PRESETS を書き換えない)
    for k, v in p["model"].items():
        if not hasattr(tm, k):
            raise KeyError(k)
        setattr(tm, k, v)


def _r1(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), 1)


def _r2(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), 2)


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


def _cond(target: dict | None) -> tuple:
    """当レース条件の比較キー (par や馬場差の表示値は含めない — モデル再学習で変わる)。"""
    t = target or {}
    return (t.get("venue"), t.get("surface"), t.get("distance"), t.get("going"))


def _unavailable(target: dict, reason: str, model: dict | None = None) -> dict:
    return {"version": tm.VERSION, "available": False, "reason": reason,
            "target": {**target, "par_sec": None, "par_n": None, "going_effect": None},
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
            "day_effects": bool((model.get("params") or {}).get("day_effects")),
            "venue_going": bool(model.get("gamma_venue"))}


def _going_effect_info(model: dict, tgt: dict, base_sec: float | None) -> dict:
    """当レースの馬場状態が 良 に対してどれだけ時計を動かすか (学習値)。sec は base_sec (標準勝ち時計 or
    レース中央値) に対する秒差。負 = 良より速い馬場。"""
    g = tgt["going"]
    val, source, n = tm.going_effect_detail(model, tgt["venue"], tgt["surface"], g)
    return {"going": g, "group": tm.going_group(g), "pct": round(val * 100, 2),
            "sec": _r1(base_sec * (math.exp(val) - 1.0)) if base_sec else None,
            "source": source, "n": n}


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
            elif tm.going_group(going) != tm.going_group(tgt["going"]):
                rec["kind"] = "same_course"
            else:
                rec["kind"] = "same"
            return rec, conv, d
    return rec, None, d


def _weight(i: int, rec: dict, tgt: dict) -> float:
    """集約の重み: 新しさ × 種別 × (別距離・別芝ダなら距離の近さと同場)。"""
    w = (RECENCY_DECAY ** i) * KIND_W[rec["kind"]]
    if rec["kind"] in ("distance", "surface"):
        w *= math.exp(-abs(math.log(tgt["distance"] / rec["distance"])) / DIST_SCALE)
        if rec["venue"] == tgt["venue"]:
            w *= SAME_VENUE_W
    return w


_CORE_KINDS = ("same", "same_course", "same_dist")


def _confidence(used: list[tuple[float, float, dict]]) -> str:
    """high: 同じ芝ダ・距離 (馬場込み同条件 / 同コース / 他場同距離) の証拠が重み合計 1.5 以上 かつ 3 走以上
    / mid: その証拠が 0.5 以上 かつ 2 走以上 / low: 別距離・芝ダ換算だけ、または 1 走 / none: 使える走なし。"""
    if not used:
        return "none"
    core = sum(w for w, _c, r in used if r["kind"] in _CORE_KINDS)
    if len(used) >= 3 and core >= 1.5:
        return "high"
    if len(used) >= 2 and core >= 0.5:
        return "mid"
    return "low"


def _fit_block(items: list[tuple[dict, float]], like: Any) -> dict:
    """適性 1 軸: 当レースに近い条件の走 (like) と それ以外の走の換算時計の差。負 = 当条件の方が走れている。

    同じ馬の換算時計は走ごとに ±1% (90 秒で ±1 秒) ばらつくので、差が 0.3 秒あっても走数が少なければ偶然と
    見分けが付かない (1 走 vs 1 走の差の誤差は ±1.4 秒)。得意/不安 は差が FIT_SEC 以上 **かつ** 誤差の FIT_Z 倍以上の
    ときだけ付け、それ以外は 普通 (= 差が誤差の範囲)。誤差 se_sec と z も出す (2026-10-29)。"""
    L = [(r, c) for r, c in items if like(r)]
    O = [(r, c) for r, c in items if not like(r)]
    out = {"n_like": len(L), "n_other": len(O), "delta_sec": None, "se_sec": None, "z": None,
           "best_sec": _r1(min(c for _r, c in L)) if L else None,
           "mean_sec": _r1(sum(c for _r, c in L) / len(L)) if L else None,
           "top3_like": sum(1 for r, _c in L if r["finish_pos"] in (1, 2, 3)),
           "label": "—"}
    if L and O:
        delta = sum(c for _r, c in L) / len(L) - sum(c for _r, c in O) / len(O)
        scale = sum(c for _r, c in items) / len(items)
        se = FIT_SIGMA * scale * math.sqrt(1.0 / len(L) + 1.0 / len(O))
        z = delta / se if se > 0 else 0.0
        out["delta_sec"], out["se_sec"], out["z"] = _r1(delta), _r1(se), _r1(z)
        sig = abs(delta) >= FIT_SEC and abs(z) >= FIT_Z
        out["label"] = ("得意" if delta < 0 else "不安") if sig else "普通"
    elif L:
        out["label"] = "実績のみ"
    elif O:
        out["label"] = "未経験"
    return out


def _fits(items: list[tuple[dict, float]], tgt: dict) -> dict:
    """距離 / 芝ダ / 馬場 の適性。馬場は同じ芝ダの走だけで見る (芝の重と ダの渋は別物)。"""
    td = tgt["distance"]
    tg = tm.going_group(tgt["going"])
    same_surface = [(r, c) for r, c in items if r["surface"] == tgt["surface"]]
    return {
        "distance": _fit_block(items, lambda r: abs(math.log(td / r["distance"])) <= NEAR_DIST),
        "surface": _fit_block(items, lambda r: r["surface"] == tgt["surface"]),
        "going": _fit_block(same_surface, lambda r: tm.going_group(r["going"]) == tg),
    }


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
    raw_by_row: list[tuple[float | None, float]] = []     # (重み付き平均 or None, 証拠の重み W)
    for h in getattr(race, "horses", []) or []:
        if getattr(h, "absent", False):
            continue
        runs = list(getattr(h, "past_runs_ext", None) or getattr(h, "past_runs", None) or [])
        recs: list[dict] = []
        used: list[tuple[float, float, dict]] = []
        items: list[tuple[dict, float]] = []
        valid_i = 0
        if race_day:
            runs = [pr for pr in runs if 0 < tm.date_key(getattr(pr, "date", "")) < race_day]
        for pr in runs[:SHOW_RUNS]:
            rec, conv, _d = _run_record(pr, tgt, model, lt)
            recs.append(rec)
            # 推定に使うのは「時計が妥当な平地走」の新しい順 EST_RUNS 本 (換算データの無いコースでも枠は消費)。
            counted = rec["note"] not in ("時計なし", "障害", "ばんえい/不明", "海外/不明の場", "時計異常")
            if conv is not None:
                items.append((rec, conv))
            if counted:
                if conv is not None and (valid_i < EST_RUNS
                                         or (SAME_ALL_RUNS and rec["kind"] in ("same", "same_course"))):
                    w = _weight(valid_i, rec, tgt)
                    rec["weight"] = round(w, 3)
                    used.append((w, conv, rec))
                valid_i += 1
        W = sum(w for w, _c, _r in used)
        raw = (sum(w * c for w, c, _r in used) / W) if used else None
        same = [r for r in recs if r["kind"] == "same" and r["own_sec"]]
        course = [r for r in recs if r["kind"] in ("same", "same_course") and r["own_sec"]]
        same_best = min(same, key=lambda r: r["own_sec"]) if same else None
        course_best = min(course, key=lambda r: r["own_sec"]) if course else None
        convs = [c for _r, c in items]
        rows.append({
            "number": int(getattr(h, "number", 0) or 0), "name": getattr(h, "name", ""),
            "rank": None, "est_sec": None, "est_raw_sec": _r1(raw), "gap_sec": None, "index": None,
            "evidence": _r2(W), "shrink": None,
            "same_n": len(same), "same_best_sec": same_best["own_sec"] if same_best else None,
            "same_best_going": same_best["going"] if same_best else None,
            "course_n": len(course), "course_best_sec": course_best["own_sec"] if course_best else None,
            "course_best_going": course_best["going"] if course_best else None,
            "conv_best_sec": _r1(min(convs)) if convs else None,
            "n_runs": len(recs), "n_used": len(used), "confidence": _confidence(used),
            "fit": _fits(items, tgt) if items else None,
            "runs": recs,
        })
        raw_by_row.append((raw, W))

    # 証拠の弱い馬はレース中央値へ寄せる (同条件が無く換算だけの馬が換算の運で 1 位にならないように)。
    # 寄せ先 = 証拠が十分な馬 (W >= STRONG_W) の重み付き平均の中央値 (2 頭未満なら全馬の中央値)。
    strong = sorted(r for r, w in raw_by_row if r is not None and w >= STRONG_W)
    anyr = sorted(r for r, _w in raw_by_row if r is not None)
    pool = strong if len(strong) >= 2 else anyr
    prior = (pool[len(pool) // 2] if len(pool) % 2 else (pool[len(pool) // 2 - 1] + pool[len(pool) // 2]) / 2) if pool else None
    for row, (raw, W) in zip(rows, raw_by_row):
        if raw is None:
            continue
        if prior is None or SHRINK_W0 <= 0:
            row["est_sec"], row["shrink"] = raw, 0.0
        else:
            row["est_sec"] = (W * raw + SHRINK_W0 * prior) / (W + SHRINK_W0)
            row["shrink"] = round(SHRINK_W0 / (W + SHRINK_W0), 2)

    _assign_ranks(rows)
    for r in rows:
        r["est_sec"] = _r1(r["est_sec"])
    pw = tm.par_win(model, tgt["surface"], tgt["distance"], tgt["venue"],
                    tgt["going"] if tgt["going_source"] == "announced" else "")
    base = pw[0] if pw else prior
    return {"version": tm.VERSION, "available": True, "reason": None,
            "target": {**tgt, "par_sec": _r1(pw[0]) if pw else None, "par_n": pw[1] if pw else None,
                       "going_effect": _going_effect_info(model, tgt, base),
                       "prior_sec": _r1(prior)},
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
    条件の比較は 場・芝ダ・距離・馬場状態 だけ (par や馬場差の表示値は再学習で変わる)。
    馬単位の引き継ぎは前回と今回の版 (version) が同じときだけ: 版が違うと推定の定義 (例 te1 → te2 の中央値寄せ・
    馬場込みの同条件) が違うので、同じ順位付けに混ぜない (2026-10-29)。前回を丸ごと使うときは前回の版のまま返す。
    """
    import copy
    usable_prev = prev if (prev and prev.get("available") and _n_ranked(prev) > 0) else None
    if cur and cur.get("available") and _n_ranked(cur) > 0:
        if (usable_prev and _cond(usable_prev.get("target")) == _cond(cur.get("target"))
                and usable_prev.get("version") == cur.get("version")):
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


_KIND_LABEL = {"same": "同条件", "same_course": "同コース", "same_dist": "同距離", "distance": "距離換算",
               "surface": "芝ダ換算", "excluded": "対象外"}


def _fit_text(f: dict | None) -> str:
    if not f:
        return "—"
    d = f.get("delta_sec")
    return f"{f['label']}" + (f"({d:+.1f})" if d is not None else "") + (f" {f['n_like']}走" if f["n_like"] else "")


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
    for col in ("順", "馬", "馬名", "推定", "差", "指数", "同条件", "距離", "芝ダ", "馬場", "証拠", "走", "信頼"):
        t.add_column(col, justify="right" if col not in ("馬名", "信頼", "同条件", "距離", "芝ダ", "馬場") else "left")
    for h in te.get("horses", []):
        same = (f"{h['same_n']}走 {fmt_time(h['same_best_sec'])}" if h["same_n"]
                else (f"同コース{h['course_n']}走" if h.get("course_n") else "—"))
        f = h.get("fit") or {}
        t.add_row(str(h["rank"] or "—"), str(h["number"]), h["name"], fmt_time(h["est_sec"]),
                  f"+{h['gap_sec']:.1f}" if h.get("gap_sec") else ("—" if h["rank"] != 1 else "0.0"),
                  f"{h['index']:.0f}" if h.get("index") is not None else "—",
                  same, _fit_text(f.get("distance")), _fit_text(f.get("surface")), _fit_text(f.get("going")),
                  f"{h['evidence']:.1f}" if h.get("evidence") is not None else "—",
                  f"{h['n_used']}/{h['n_runs']}",
                  {"high": "高", "mid": "中", "low": "低", "none": "—"}[h["confidence"]])
    con.print(t)
    ge = tgt.get("going_effect") or {}
    if ge and ge.get("group") != "良" and ge.get("sec") is not None:
        con.print(f"[dim]馬場 {ge['going']}: この場の{tgt.get('surface', '')}は良より {ge['sec']:+.1f} 秒 "
                  f"({ge['pct']:+.2f}%, 学習 {ge.get('n') or 0} 走)[/dim]")
    if tgt.get("par_sec"):
        con.print(f"[dim]参考: この条件の平均的な勝ち時計 {fmt_time(tgt['par_sec'])} (n={tgt.get('par_n')})[/dim]")
    if detail:
        for h in te.get("horses", []):
            con.print(f"[bold]{h['number']} {h['name']}[/bold]  推定 {fmt_time(h['est_sec'])}"
                      f" (重み付き平均 {fmt_time(h.get('est_raw_sec'))}・中央値寄せ {int((h.get('shrink') or 0) * 100)}%)")
            for r in h["runs"]:
                conv = fmt_time(r["conv_sec"]) if r["conv_sec"] is not None else "—"
                con.print(f"   {r['date']} {r['venue']} {r['surface'][:1]}{r['distance']} {r['going'] or '-'}"
                          f" {fmt_time(r['own_sec'])} → {conv} [{_KIND_LABEL[r['kind']]}"
                          f"{(':' + r['note']) if r.get('note') else ''}]"
                          f"{(' w=' + format(r['weight'], '.2f')) if r.get('weight') is not None else ''}")


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
