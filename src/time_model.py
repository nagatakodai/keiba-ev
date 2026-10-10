"""走破時計の条件換算モデル (te1)。

「別の距離・馬場・競馬場で走った時計を、当レースの条件ならどれくらいの時計か」に換算するための
条件効果を、**同じ馬が別条件を走った差** から推定する (固定効果モデル)。勝ち時計の中央値で基準
タイムを作る方式と違い、条件ごとのクラス構成の偏り (例: 大井2000m は上級条件が多く勝ち時計が
速い) が混入しない。

    log(走破時計) = θ(芝ダ, 距離, 場) + γ(中央/地方, 芝ダ, 馬場状態) + δ(場, 開催日, 芝ダ) + a(馬) + ε

  θ: 条件 (コース) 効果。サンプルの少ないコースは場ごとの log 距離曲線 a(場,芝ダ)+b(中央地方,芝ダ)·log距離
     へ K 走分の重みで縮約 (未出走コースは曲線そのもの)。
  γ: 馬場状態効果 (良=0)。
  δ: 開催日の馬場差 (その日の時計の出やすさ。風・含水率・クッション等)。K_DAY で 0 へ縮約。
     残差分散の約 36% が開催日単位で、これを差し引くと換算精度が明確に上がる (2026-10-10 検証)。
  a: 馬の能力 (換算には使わない)。

換算:  換算時計 = 走破時計 × exp(θ_当 + γ_当 − θ_走 − γ_走 − δ_走)   (当日の δ は未知なので 0)

学習データ (ledger = 1 馬 1 走の走破時計):
  - data/cache/time_ledger/netkeiba_raw.jsonl: data/raw の netkeiba 馬柱 + 結果表 (全着時計) から一度だけ抽出
  - data/cache/time_ledger/live-YYYYMM.jsonl: 解析時に取得した過去走 (keiba.go.jp / JRA 公式 / oddspark) を追記
    → netkeiba 規制後の開催日 (2026-06〜) の馬場差もデータが貯まるほど推定できるようになる。
モデルは data/cache/time_model.json。`make time-model` または API 常駐ループの日次再学習で更新。

検証 (scripts/time_eval_backtest.py, walk-forward・月初より前のデータだけで学習):
  勝ち馬 AUC (レース内) 地方 0.79/0.77・JRA 0.72/0.72 (選定 1-3月 / 確認 4-5月)、
  素朴な勝ち時計 par 換算 (地方 0.75/0.74・JRA 0.68/0.69) より一貫して上。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = ROOT / "data" / "cache" / "time_ledger"
MODEL_PATH = ROOT / "data" / "cache" / "time_model.json"
VERSION = "te1"

JRA_VENUES = {"札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"}
# 地方の場名 (JRA accessU は 2 文字に切り詰める: 名古→名古屋)。海外・不明は換算対象外。
NAR_VENUES = {"帯広", "門別", "盛岡", "水沢", "浦和", "船橋", "大井", "川崎", "金沢", "笠松",
              "名古屋", "園田", "姫路", "高知", "佐賀"}
_VENUE_ALIAS = {"名古": "名古屋"}

# 学習パラメータ (2026-10-10 walk-forward で確認。K/窓/大差除外は感度が小さく、δ の有無だけが効く)
K = 8.0              # コース効果 θ の曲線への縮約 (走数換算)
K_DAY = 3.0          # 開催日馬場差 δ の 0 への縮約
WINDOW_DAYS = 540    # 学習に使う走の期間 (最新日から遡る)
MAX_BEHIND = 3.0     # 勝ち時計が分かる走で 3 秒超の大差負けは条件効果の推定から除外 (ノイズ)
ITERS = 25


# ------------------------------------------------------------ 正規化 ----

def norm_venue(v: str) -> str:
    """場名の正規化: NFKC・(ば)/先頭Ｊの除去・切り詰め名の復元。"""
    v = unicodedata.normalize("NFKC", v or "").strip().replace("(ば)", "")
    if v.startswith("J") and v[1:] in JRA_VENUES:
        v = v[1:]
    return _VENUE_ALIAS.get(v, v)


def is_known_venue(v: str) -> bool:
    return v in JRA_VENUES or v in NAR_VENUES


def circuit(venue: str) -> str:
    return "jra" if venue in JRA_VENUES else "nar"


def norm_going(g: str) -> str:
    """馬場状態を 1 文字 (良/稍/重/不) に。不明は ""。"""
    g = unicodedata.normalize("NFKC", g or "").strip()
    for c in ("良", "稍", "重", "不"):
        if g.startswith(c):
            return c
    return ""


_JUMP_KEYS = ("障害", "J・G", "ジャンプ", "JS", "GJ")


def norm_surface(surface: str, race_class: str = "") -> str:
    """芝/ダート/障害。JRA accessU は障害を「芝」で返すのでクラス名で判定する。"""
    s = unicodedata.normalize("NFKC", surface or "").strip()
    if s in ("ダ", "ダート"):
        s = "ダート"
    rc = unicodedata.normalize("NFKC", race_class or "")
    if any(k in rc for k in _JUMP_KEYS) or s == "障":
        return "障害"
    return s


def norm_horse(name: str) -> str:
    """馬名キー: NFKC + 空白除去 + 先頭の (地)/[外]/□/○ 等の印を除去。"""
    n = unicodedata.normalize("NFKC", name or "").strip()
    while n and n[0] in "([【□○◇△▲●◆□■":
        close = {"(": ")", "[": "]", "【": "】"}.get(n[0])
        if close and close in n[:4]:
            n = n[n.index(close) + 1:]
        else:
            n = n[1:]
    return "".join(n.split())


def date_key(s: str) -> int:
    """'2026.3.1' / '2026.03.01' / '2026/03/01' / '20260301' → 20260301 (失敗 0)。"""
    s = (s or "").strip()
    if s.isdigit() and len(s) == 8:
        return int(s)
    parts = s.replace("/", ".").replace("-", ".").split(".")
    try:
        y, m, d = (int(p) for p in parts[:3])
        return y * 10000 + m * 100 + d
    except ValueError:
        return 0


def valid_flat_time(surface: str, distance: int, own_sec: float) -> bool:
    """平地の走破時計として常識的か (秒/100m)。取消行・誤パース (負担重量を時計と誤認等) を落とす。"""
    if surface not in ("芝", "ダート") or distance <= 0 or own_sec <= 0:
        return False
    s100 = own_sec / distance * 100.0
    lo, hi = (5.3, 7.2) if surface == "芝" else (5.5, 8.0)
    return lo <= s100 <= hi


# ------------------------------------------------------------ ledger ----

def ledger_row(horse: str, pr: Any) -> dict | None:
    """PastRun → ledger 1 行 (平地・時計が妥当なもののみ)。"""
    surface = norm_surface(getattr(pr, "surface", ""), getattr(pr, "race_class", ""))
    distance = int(getattr(pr, "distance", 0) or 0)
    own = float(getattr(pr, "own_time_sec", 0.0) or 0.0)
    venue = norm_venue(getattr(pr, "venue", ""))
    d = date_key(getattr(pr, "date", ""))
    if not d or not is_known_venue(venue) or not valid_flat_time(surface, distance, own):
        return None
    wt = float(getattr(pr, "winner_time_sec", 0.0) or 0.0)
    diff = float(getattr(pr, "time_diff_sec", 0.0) or 0.0)
    # netkeiba 以外は winner_time_sec に自走時計が入る (diff=0) ので勝ち時計としては使わない
    win = wt if (diff > 0 or getattr(pr, "finish_pos", None) == 1) and wt > 0 else 0.0
    return {"h": norm_horse(horse), "d": d, "v": venue, "s": surface, "dist": distance,
            "g": norm_going(getattr(pr, "going", "")), "t": round(own, 2), "wt": round(win, 2)}


def _row_key(r: dict) -> tuple:
    return (r["h"], r["d"], r["v"], r["dist"])


def load_ledger(paths: Iterable[Path] | None = None) -> list[dict]:
    """ledger を全ファイルから読み、(馬, 日付, 場, 距離) で重複除去 (後勝ち)。"""
    files = list(paths) if paths is not None else sorted(LEDGER_DIR.glob("*.jsonl"))
    seen: dict[tuple, dict] = {}
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or not r.get("h") or not r.get("t"):
                        continue
                    seen[_row_key(r)] = r
        except OSError:
            continue
    return list(seen.values())


def record_live_runs(rd: Any, race_key: str = "") -> int:
    """解析中のレースの各馬の過去走を live ledger に追記 (換算モデルの学習データを貯める)。

    race_key を渡すと同じレースは月に 1 回だけ記録する (score/bet/オッズ更新のたびに同じ過去走を
    重ねて書かない)。重複は load 時にも除去される。失敗は呑む。
    """
    month = f"{_dt.date.today():%Y%m}"
    idx_path = LEDGER_DIR / f"live-{month}.idx"
    if race_key:
        try:
            if race_key in idx_path.read_text(encoding="utf-8").split():
                return 0
        except OSError:
            pass
    rows: list[dict] = []
    for h in getattr(getattr(rd, "race", None), "horses", None) or []:
        runs = getattr(h, "past_runs_ext", None) or getattr(h, "past_runs", None) or []
        for pr in runs:
            r = ledger_row(getattr(h, "name", ""), pr)
            if r is not None:
                rows.append(r)
    if not rows:
        return 0
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        path = LEDGER_DIR / f"live-{month}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if race_key:
            with open(idx_path, "a", encoding="utf-8") as f:
                f.write(race_key + "\n")
    except OSError:
        return 0
    return len(rows)


# ------------------------------------------------------------ fitting ----

def _going_level(g: str) -> str:
    return g or "良"


def fit(rows: list[dict], *, day_effects: bool = True) -> dict:
    """固定効果モデルを当てはめて、換算に必要なパラメータ dict を返す (JSON 化可能)。"""
    if not rows:
        return {"version": VERSION, "theta": {}, "gamma": {}, "a_vs": {}, "b": {}, "delta": {},
                "par_win": {}, "n_runs": 0}
    last = max(r["d"] for r in rows)
    ld = _dt.date(last // 10000, last // 100 % 100, last % 100) - _dt.timedelta(days=WINDOW_DAYS)
    lim = ld.year * 10000 + ld.month * 100 + ld.day
    use = [r for r in rows if r["d"] >= lim and (not r.get("wt") or r["t"] - r["wt"] <= MAX_BEHIND)]
    cnt: dict[str, int] = defaultdict(int)
    for r in use:
        cnt[r["h"]] += 1
    use = [r for r in use if cnt[r["h"]] >= 2]     # 1 走だけの馬は条件効果を識別しない
    if not use:
        return {"version": VERSION, "theta": {}, "gamma": {}, "a_vs": {}, "b": {}, "delta": {},
                "par_win": {}, "n_runs": 0}

    cells = sorted({(r["s"], r["dist"], r["v"]) for r in use})
    cidx = {c: i for i, c in enumerate(cells)}
    glev = sorted({(circuit(r["v"]), r["s"], _going_level(r["g"])) for r in use})
    gidx = {g: i for i, g in enumerate(glev)}
    horses = sorted({r["h"] for r in use})
    hidx = {h: i for i, h in enumerate(horses)}
    days = sorted({(r["v"], r["d"], r["s"]) for r in use})
    didx = {k: i for i, k in enumerate(days)}

    y = np.array([math.log(r["t"]) for r in use])
    ci = np.array([cidx[(r["s"], r["dist"], r["v"])] for r in use])
    gi = np.array([gidx[(circuit(r["v"]), r["s"], _going_level(r["g"]))] for r in use])
    hi = np.array([hidx[r["h"]] for r in use])
    di = np.array([didx[(r["v"], r["d"], r["s"])] for r in use])
    nC, nG, nH, nD = len(cells), len(glev), len(horses), len(days)
    nc = np.bincount(ci, minlength=nC).astype(float)
    nh = np.bincount(hi, minlength=nH).astype(float)
    ng = np.bincount(gi, minlength=nG).astype(float)
    nd = np.bincount(di, minlength=nD).astype(float)

    logd = np.array([math.log(c[1]) for c in cells])
    vs_keys = sorted({(c[2], c[0]) for c in cells})
    vs_index = {k: i for i, k in enumerate(vs_keys)}
    vsi = np.array([vs_index[(c[2], c[0])] for c in cells])
    cs_keys = sorted({(circuit(c[2]), c[0]) for c in cells})
    cs_index = {k: i for i, k in enumerate(cs_keys)}
    csi = np.array([cs_index[(circuit(c[2]), c[0])] for c in cells])
    g_cs = [(g[0], g[1]) for g in glev]
    g_good = [g[2] == "良" for g in glev]

    theta = np.bincount(ci, weights=y, minlength=nC) / np.maximum(nc, 1)
    gam = np.zeros(nG)
    delta = np.zeros(nD)
    a = np.zeros(nH)
    b = np.full(len(cs_keys), 1.1)
    a_vs = np.zeros(len(vs_keys))
    for _ in range(ITERS):
        a = np.bincount(hi, weights=y - theta[ci] - gam[gi] - delta[di], minlength=nH) / np.maximum(nh, 1)
        res_g = np.bincount(gi, weights=y - theta[ci] - a[hi] - delta[di], minlength=nG) / np.maximum(ng, 1)
        base = {g_cs[j]: res_g[j] for j in range(nG) if g_good[j]}
        gam = np.array([res_g[j] - base.get(g_cs[j], 0.0) for j in range(nG)])
        raw = np.bincount(ci, weights=y - a[hi] - gam[gi] - delta[di], minlength=nC)
        mean_c = raw / np.maximum(nc, 1)
        for k in range(len(cs_keys)):
            m = csi == k
            xs, ys_, ws = [], [], []
            for v in set(vsi[m].tolist()):
                mm = m & (vsi == v)
                if mm.sum() < 2:
                    continue
                w = nc[mm]
                xs.extend((logd[mm] - np.average(logd[mm], weights=w)).tolist())
                ys_.extend((mean_c[mm] - np.average(mean_c[mm], weights=w)).tolist())
                ws.extend(w.tolist())
            xa, ya, wa = np.array(xs), np.array(ys_), np.array(ws)
            den = float(np.sum(wa * xa * xa)) if len(xa) else 0.0
            b[k] = float(np.sum(wa * xa * ya) / den) if den > 0 else 1.1
        for v in range(len(vs_keys)):
            m = vsi == v
            a_vs[v] = float(np.average(mean_c[m] - b[csi[m]] * logd[m], weights=np.maximum(nc[m], 1e-9)))
        curve = a_vs[vsi] + b[csi] * logd
        theta = (raw + K * curve) / (nc + K)
        if day_effects:
            delta = np.bincount(di, weights=y - theta[ci] - gam[gi] - a[hi], minlength=nD) / (nd + K_DAY)

    # 表示用の参考: 条件別の勝ち時計中央値 (馬場別 n>=10 / 全馬場 n>=5)
    races: dict[tuple, float] = {}
    for r in rows:
        if r.get("wt"):
            races[(r["v"], r["d"], r["s"], r["dist"], r["g"])] = r["wt"]
    pw: dict[str, list[float]] = defaultdict(list)
    for (v, d, s, dist, g), wt in races.items():
        pw[f"{s}|{dist}|{v}|"].append(wt)
        if g:
            pw[f"{s}|{dist}|{v}|{g}"].append(wt)
    par_win = {k: [round(float(np.median(v)), 2), len(v)] for k, v in pw.items()
               if len(v) >= (10 if not k.endswith("|") else 5)}

    return {
        "version": VERSION,
        "fitted_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "data_until": f"{last // 10000:04d}-{last // 100 % 100:02d}-{last % 100:02d}",
        "n_runs": len(use), "n_horses": nH, "n_cells": nC, "n_days": nD,
        "params": {"K": K, "K_DAY": K_DAY, "WINDOW_DAYS": WINDOW_DAYS, "MAX_BEHIND": MAX_BEHIND,
                   "day_effects": day_effects},
        "theta": {f"{s}|{d}|{v}": round(float(theta[i]), 6) for i, (s, d, v) in enumerate(cells)},
        "theta_n": {f"{s}|{d}|{v}": int(nc[i]) for i, (s, d, v) in enumerate(cells)},
        "gamma": {f"{c}|{s}|{g}": round(float(gam[i]), 6) for i, (c, s, g) in enumerate(glev)},
        "a_vs": {f"{v}|{s}": round(float(a_vs[i]), 6) for i, (v, s) in enumerate(vs_keys)},
        "b": {f"{c}|{s}": round(float(b[i]), 6) for i, (c, s) in enumerate(cs_keys)},
        "delta": ({f"{v}|{d}|{s}": round(float(delta[i]), 6) for i, (v, d, s) in enumerate(days)
                   if abs(delta[i]) > 1e-9} if day_effects else {}),
        "par_win": par_win,
    }


def save_model(model: dict, path: Path | None = None) -> None:
    path = path or MODEL_PATH        # 既定は呼び出し時に解決 (テストの差し替えが効くように)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


_MODEL_CACHE: dict[str, Any] = {"mtime": None, "model": None}


def load_model(path: Path | None = None) -> dict | None:
    """換算モデルを読む (mtime が変わったら再読込)。無ければ None。"""
    path = path or MODEL_PATH
    try:
        mt = path.stat().st_mtime
    except OSError:
        return None
    key = (str(path), mt)
    if _MODEL_CACHE["mtime"] != key:
        try:
            _MODEL_CACHE["model"] = json.loads(path.read_text(encoding="utf-8"))
            _MODEL_CACHE["mtime"] = key
        except (OSError, ValueError):
            return None
    return _MODEL_CACHE["model"]


def rebuild(paths: Iterable[Path] | None = None) -> dict:
    """ledger 全体から再学習して保存。返り値はモデル dict。"""
    model = fit(load_ledger(paths))
    save_model(model)
    return model


def rebuild_if_stale(max_age_hours: float = 24.0) -> bool:
    """モデルが古く (max_age 超) かつ ledger がモデル以降に更新されていれば再学習。常駐ループ用。"""
    try:
        m_mtime = MODEL_PATH.stat().st_mtime
    except OSError:
        m_mtime = 0.0
    import time
    if m_mtime and time.time() - m_mtime < max_age_hours * 3600:
        return False
    newest = max((p.stat().st_mtime for p in LEDGER_DIR.glob("*.jsonl")), default=0.0)
    if not newest or newest <= m_mtime:
        return False
    rebuild()
    return True


# ------------------------------------------------------------ conversion ----

def log_effect(model: dict, surface: str, distance: int, venue: str, going: str) -> float | None:
    """θ(コース) + γ(馬場状態)。未出走コースは場の距離曲線。場の曲線も無ければ None。"""
    th = model["theta"].get(f"{surface}|{distance}|{venue}")
    if th is None:
        a = model["a_vs"].get(f"{venue}|{surface}")
        b = model["b"].get(f"{circuit(venue)}|{surface}")
        if a is None or b is None:
            return None
        th = a + b * math.log(distance)
    return th + model["gamma"].get(f"{circuit(venue)}|{surface}|{_going_level(going)}", 0.0)


def day_effect(model: dict, venue: str, date: int, surface: str) -> float:
    return float(model.get("delta", {}).get(f"{venue}|{date}|{surface}", 0.0))


def par_win(model: dict, surface: str, distance: int, venue: str, going: str) -> tuple[float, int] | None:
    """表示用: 当該条件の勝ち時計中央値 (馬場別があれば優先)。"""
    pw = model.get("par_win", {})
    v = (pw.get(f"{surface}|{distance}|{venue}|{going}") if going else None) or pw.get(f"{surface}|{distance}|{venue}|")
    return (float(v[0]), int(v[1])) if v else None
