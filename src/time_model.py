"""走破時計の条件換算モデル (te1〜te2)。

「別の距離・馬場・競馬場で走った時計を、当レースの条件ならどれくらいの時計か」に換算するための
条件効果を、**同じ馬が別条件を走った差** から推定する (固定効果モデル)。勝ち時計の中央値で基準
タイムを作る方式と違い、条件ごとのクラス構成の偏り (例: 大井2000m は上級条件が多く勝ち時計が
速い) が混入しない。

    log(走破時計) = θ(芝ダ, 距離, 場) + γ_v(場, 芝ダ, 馬場状態) + δ(場, 開催日, 芝ダ) + a(馬) + ε

  (te2, 2026-10-14〜: 馬場状態効果は場別 γ_v。中央/地方全体の γ へ K_GOING 走分で縮約し、δ から馬場状態ぶんを
   γ_v へ移す再中心化を行う。te1 は γ(中央/地方, 芝ダ, 馬場状態) 1 本だった)

  θ: 条件 (コース) 効果。サンプルの少ないコースは場ごとの log 距離曲線 a(場,芝ダ)+b(中央地方,芝ダ)·log距離
     へ K 走分の重みで縮約 (未出走コースは曲線そのもの)。
  γ: 馬場状態効果 (良=0)。
  δ: 開催日の馬場差 (その日の時計の出やすさ。風・含水率・クッション等)。K_DAY で 0 へ縮約。
     残差分散の約 36% が開催日単位で、これを差し引くと換算精度が明確に上がる (2026-10-10 検証)。
  a: 馬の能力 (換算には使わない)。

換算:  換算時計 = 走破時計 × exp(θ_当 + γ_当 − θ_走 − γ_走 − δ_走)   (当日の δ は未知なので 0)

学習データ (ledger = 1 馬 1 走の走破時計):
  - data/cache/time_ledger/netkeiba_raw.jsonl: data/raw の netkeiba 馬柱 + 結果表 (全着時計) から一度だけ抽出
  - data/cache/time_ledger/nar_official-YYYYMM.jsonl: 地方競馬全国協会の公式データ (src/nar_official.py, 月次 CSV)
    の **地方全レース・全着** の走破時計 + 勝ち時計 + 馬場。学習窓ぶんを月 1 ファイルで持ち、当月は毎日取り直す
    → netkeiba 規制後 (2026-06〜) の地方の開催日の馬場差 δ も推定でき、大差負け除外も効く (2026-10-13)。
  - data/cache/time_ledger/live-YYYYMM.jsonl: 解析時に取得した過去走 (keiba.go.jp / JRA 公式 / oddspark) を追記
    → 中央は公式の一括データが無いので、netkeiba 規制後の開催日の馬場差はこれが貯まるほど推定できるようになる。
  同じ走 (馬・日・場・距離) は後のファイルが勝つ (live → nar_official → netkeiba_raw の順。ただし勝ち時計 wt と
  レース番号 r は後の行に無ければ前の行から引き継ぐ)。公式と netkeiba は重なる 94,993 走で時計・馬場が完全一致、
  勝ち時計も両方にある 93,704 走で一致 (netkeiba は 1,289 走で勝ち時計を欠く)。
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
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = ROOT / "data" / "cache" / "time_ledger"
MODEL_PATH = ROOT / "data" / "cache" / "time_model.json"
VERSION = "te2"       # te1 = 2026-10-10 / te2 = 2026-10-14 (場別の馬場状態効果 γ_v + 新しい集約 src/time_eval.py)

JRA_VENUES = {"札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"}
# 地方の場名 (JRA accessU は 2 文字に切り詰める: 名古→名古屋)。海外・不明は換算対象外。
NAR_VENUES = {"帯広", "門別", "盛岡", "水沢", "浦和", "船橋", "大井", "川崎", "金沢", "笠松",
              "名古屋", "園田", "姫路", "高知", "佐賀"}
_VENUE_ALIAS = {"名古": "名古屋"}

# 学習パラメータ (2026-10-10 walk-forward で確認。K/窓/大差除外は感度が小さく、δ の有無だけが効く)
K = 8.0              # コース効果 θ の曲線への縮約 (走数換算)
K_DAY = 3.0          # 開催日馬場差 δ の 0 への縮約
VENUE_GOING = True   # False = 旧 te1 (馬場状態の効果は中央/地方 1 本・δ の再中心化なし)。検証スクリプトの比較用
K_GOING = 30.0       # 場別の馬場状態効果 γ_v を、中央/地方全体の γ へ縮約する走数 (2026-10-14。場ごとに向きが違う:
                     # 同じ馬・同じコース・60 日以内の 道悪−良 で 水沢 不良 −3.0% / 高知 −2.4% / 浦和 −1.8% だが
                     # 大井 不良 +0.7% / 船橋・川崎・笠松・園田 重 +0.7〜0.8% と逆向き。地方 1 本 (不良 −0.56%) はどの場にも合わない)
WINDOW_DAYS = 540    # 学習に使う走の期間 (最新日から遡る)
MAX_BEHIND = 3.0     # 勝ち時計が分かる走で 3 秒超の大差負けは条件効果の推定から除外 (ノイズ)
LOAD_SLACK_DAYS = 365  # 再学習時、今日から WINDOW_DAYS+これ より古い行は読み込まない (メモリ節約)
DENSE_MIN_ROWS = 50  # 学習窓の起点に使う「データが揃った日」: その circuit の一括データ (netkeiba_raw / 公式) の
                     # 1 日の走数がこれ以上 (実データは 1 日 69〜500 走。live 行は数えない — fit 参照)
MAX_ITERS = 500      # 交互最小二乗の上限 (通常 100〜200 回で収束)
TOL = 1e-7           # 1 反復の最大変化 (log 秒) がこれ未満で収束 = 90 秒のレースで 0.00001 秒未満


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


# 障害の目印 (JRA accessU は障害を「芝」で返すのでレース名で判定する)。JS/GJ は直前が英字でないときだけ
# (ペガサスJS・中山GJ・中山GJJGI・牛若丸JSOP は障害、YJSF名1・LJS名(C) = ヤング/レディースジョッキーズ
# シリーズは平地)。GJ の直後が S = ＧＪＳ (ゴールデンジョッキーズシリーズ, 地方の平地, 2026-10-13 公式データで
# 発見) も平地。
_JUMP_RE = re.compile(r"障害|ジャンプ|J・G|(?<![A-Z])(?:JS|GJ(?!S))")
# 末尾 J の略称 (東京ハイJ / 阪神スプリJ / 三木ホースJ) は芝 2750m 以上のときだけ障害
# (「次開催は15年ぶりJ」ダ1800・「2025J」ダート のような平地のレース名を拾わないため)。
_JUMP_TAIL_RE = re.compile(r"(?<![A-Za-z0-9])J$")
JUMP_MIN_DISTANCE = 2750


def norm_surface(surface: str, race_class: str = "", distance: int = 0) -> str:
    """芝/ダート/障害。"""
    s = unicodedata.normalize("NFKC", surface or "").strip()
    if s in ("ダ", "ダート"):
        s = "ダート"
    rc = unicodedata.normalize("NFKC", race_class or "").strip()
    if s in ("障", "障害") or _JUMP_RE.search(rc):
        return "障害"
    if s == "芝" and distance >= JUMP_MIN_DISTANCE and _JUMP_TAIL_RE.search(rc):
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
    distance = int(getattr(pr, "distance", 0) or 0)
    surface = norm_surface(getattr(pr, "surface", ""), getattr(pr, "race_class", ""), distance)
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


def load_ledger(paths: Iterable[Path] | None = None, *, min_date: int = 0) -> list[dict]:
    """ledger を全ファイルから読み、(馬, 日付, 場, 距離) で重複除去 (後勝ち・ただし勝ち時計 wt とレース番号 r は
    後の行に無ければ前の行の値を引き継ぐ — netkeiba_raw は公式データにある勝ち時計を 1,289 走で欠いている)。

    min_date (YYYYMMDD) より古い行は辞書に入れずに読み飛ばす (学習窓の外は fit でも使わないので、
    常駐 API の日次再学習でメモリを無駄に使わない)。
    """
    files = list(paths) if paths is not None else sorted(LEDGER_DIR.glob("*.jsonl"))
    seen: dict[tuple, dict] = {}
    for p in files:
        live = Path(p).name.startswith("live-")      # 解析時の過去走 (学習窓の起点の判定に数えない — fit 参照)
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or not r.get("h") or not r.get("t"):
                        continue
                    if min_date and int(r.get("d") or 0) < min_date:
                        continue
                    if live:
                        r["_src"] = "live"
                    k = _row_key(r)
                    prev = seen.get(k)
                    if prev is not None:
                        if not r.get("wt") and prev.get("wt"):
                            r["wt"] = prev["wt"]
                        if not r.get("r") and prev.get("r"):
                            r["r"] = prev["r"]
                    seen[k] = r
        except OSError:
            continue
    return list(seen.values())


def record_live_runs(rd: Any, race_key: str = "") -> int:
    """解析中のレースの各馬の過去走を live ledger に追記 (換算モデルの学習データを貯める)。

    race_key を渡すと **(レース, 馬) ごとに月 1 回だけ** 記録する (score/bet/オッズ更新のたびに同じ
    過去走を重ねて書かない)。過去走が取れなかった馬は記録済みにしないので、後の保存で取れれば足される。
    重複は load 時にも除去される。失敗は呑む。
    """
    month = f"{_dt.date.today():%Y%m}"
    idx_path = LEDGER_DIR / f"live-{month}.idx"
    done: set[str] = set()
    if race_key:
        try:
            done = set(idx_path.read_text(encoding="utf-8").split("\n"))
        except OSError:
            pass
    rows: list[dict] = []
    new_keys: list[str] = []
    for h in getattr(getattr(rd, "race", None), "horses", None) or []:
        runs = getattr(h, "past_runs_ext", None) or getattr(h, "past_runs", None) or []
        key = f"{race_key}|{norm_horse(getattr(h, 'name', ''))}" if race_key else ""
        if key and key in done:
            continue
        got = [r for r in (ledger_row(getattr(h, "name", ""), pr) for pr in runs) if r is not None]
        if got:
            rows.extend(got)
            if key:
                new_keys.append(key)
    if not rows:
        return 0
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        path = LEDGER_DIR / f"live-{month}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if new_keys:
            with open(idx_path, "a", encoding="utf-8") as f:
                f.write("".join(k + "\n" for k in new_keys))
    except OSError:
        return 0
    return len(rows)


# ------------------------------------------------------------ fitting ----

def _going_level(g: str) -> str:
    return g or "良"


def going_group(g: str) -> str:
    """集約 (time_eval) 用の馬場状態の括り: 良 / 稍 / 渋 (重・不良)。重と不良は効果が近く (中央ダ −1.1% / −1.35%)、
    分けると「馬場込みの同条件」の走が足りなくなるので 1 つに。空 (不明) は良扱い (換算モデルと同じ)。"""
    g = norm_going(g) if g not in ("", "良", "稍", "重", "不") else g
    return "渋" if g in ("重", "不") else (g or "良")


def fit(rows: list[dict], *, day_effects: bool = True) -> dict:
    """固定効果モデルを当てはめて、換算に必要なパラメータ dict を返す (JSON 化可能)。"""
    if not rows:
        return {"version": VERSION, "theta": {}, "gamma": {}, "a_vs": {}, "b": {}, "delta": {},
                "par_win": {}, "n_runs": 0}
    # 学習窓は中央/地方ごとに「その circuit で一括データが揃った最新日 (1 日 DENSE_MIN_ROWS 走以上・今日以前)」から
    # WINDOW_DAYS 遡る (それより新しい行は全部使う)。地方は公式データで当月まで揃う一方、中央は netkeiba
    # キャッシュ (〜2026-05) + 解析時の live 行だけ。全体の最新日や「最新の 1 行」で切ると、中央の窓が
    # live 行 1 つで数か月ぶん削れ、表示する「〜までの走破時計で学習」も実態より新しく見えてしまう
    # (2026-10-13 レビュー: 1 行で中央の 2,756 走が窓外・換算が中央値 0.15 秒動いた)。walk-forward 検証は
    # 両 circuit とも評価月の直前まで揃った状態で窓を切っていたので、検証値はこの定義と一致する。
    # 起点の「揃った日」は一括データ (netkeiba_raw / 公式) の行だけで数える: live 行は各馬の過去 12 走ぶんが
    # 最近の開催日に積み上がる (107 レース解析で 1 日 60〜80 走) ので、数えると揃っていない日が起点になる。
    # 一括データの無い circuit (立ち上げ直後・テスト) だけ live 行も数える。
    today = _dt.date.today()
    today_i = today.year * 10000 + today.month * 100 + today.day
    bulk_day: dict[tuple[str, int], int] = defaultdict(int)
    all_day: dict[tuple[str, int], int] = defaultdict(int)
    for r in rows:
        if r["d"] <= today_i:
            k = (circuit(r["v"]), r["d"])
            all_day[k] += 1
            if r.get("_src") != "live":
                bulk_day[k] += 1
    bulk_c = {c for c, _d in bulk_day}
    per_day = dict(bulk_day)
    for k, n in all_day.items():
        if k[0] not in bulk_c:
            per_day[k] = n
    last_c: dict[str, int] = {}
    any_c: dict[str, int] = {}
    for (c, d), n in per_day.items():
        any_c[c] = max(any_c.get(c, 0), d)
        if n >= DENSE_MIN_ROWS:
            last_c[c] = max(last_c.get(c, 0), d)
    for c, d in any_c.items():
        last_c.setdefault(c, d)            # 揃った日が無い circuit (テスト・立ち上げ直後) は最新日
    if not last_c:                         # 全行が未来日付 (誤パース) — 窓を切れないので各 circuit の最大日を使う
        for r in rows:
            c = circuit(r["v"])
            last_c[c] = max(last_c.get(c, 0), r["d"])
    last = max(last_c.values())
    lim_c: dict[str, int] = {}
    for c, lc in last_c.items():
        ld = _dt.date(lc // 10000, lc // 100 % 100, lc % 100) - _dt.timedelta(days=WINDOW_DAYS)
        lim_c[c] = ld.year * 10000 + ld.month * 100 + ld.day
    use = [r for r in rows if r["d"] >= lim_c.get(circuit(r["v"]), 0)
           and (not r.get("wt") or r["t"] - r["wt"] <= MAX_BEHIND)]
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
    # 場別の馬場状態セル (場, 芝ダ, 馬場)。実際に換算で使う γ はこちら (中央/地方全体の γ へ K_GOING で縮約)
    vlev = sorted({(r["v"], r["s"], _going_level(r["g"])) for r in use})
    vidx = {k: i for i, k in enumerate(vlev)}
    horses = sorted({r["h"] for r in use})
    hidx = {h: i for i, h in enumerate(horses)}
    days = sorted({(r["v"], r["d"], r["s"]) for r in use})
    didx = {k: i for i, k in enumerate(days)}

    y = np.array([math.log(r["t"]) for r in use])
    ci = np.array([cidx[(r["s"], r["dist"], r["v"])] for r in use])
    gi = np.array([gidx[(circuit(r["v"]), r["s"], _going_level(r["g"]))] for r in use])
    vi = np.array([vidx[(r["v"], r["s"], _going_level(r["g"]))] for r in use])
    hi = np.array([hidx[r["h"]] for r in use])
    di = np.array([didx[(r["v"], r["d"], r["s"])] for r in use])
    nC, nG, nV, nH, nD = len(cells), len(glev), len(vlev), len(horses), len(days)
    nc = np.bincount(ci, minlength=nC).astype(float)
    nh = np.bincount(hi, minlength=nH).astype(float)
    ng = np.bincount(gi, minlength=nG).astype(float)
    nv = np.bincount(vi, minlength=nV).astype(float)
    nd = np.bincount(di, minlength=nD).astype(float)
    v_vs = [(k[0], k[1]) for k in vlev]                       # 場別セル → (場, 芝ダ) : 良 を基準にする単位
    v_good = np.array([k[2] == "良" for k in vlev])
    v_prior = np.array([gidx.get((circuit(k[0]), k[1], k[2]), -1) for k in vlev])   # 縮約先 = 全体の γ

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
    gamv = np.zeros(nV)
    delta = np.zeros(nD)
    a = np.zeros(nH)
    b = np.full(len(cs_keys), 1.1)
    a_vs = np.zeros(len(vs_keys))
    n_iter = 0
    for n_iter in range(1, MAX_ITERS + 1):
        prev_theta, prev_gam, prev_delta = theta.copy(), gamv.copy(), delta.copy()
        a = np.bincount(hi, weights=y - theta[ci] - gamv[vi] - delta[di], minlength=nH) / np.maximum(nh, 1)
        res = y - theta[ci] - a[hi] - delta[di]
        # 全体 (中央/地方 × 芝ダ × 馬場) の γ: 場別 γ_v の縮約先 (良 = 0)
        res_g = np.bincount(gi, weights=res, minlength=nG) / np.maximum(ng, 1)
        base = {g_cs[j]: res_g[j] for j in range(nG) if g_good[j]}
        gam = np.array([res_g[j] - base.get(g_cs[j], 0.0) for j in range(nG)])
        # 場別 γ_v: その場の良に対する差を、走数 n_v と K_GOING で全体の γ へ縮約 (良 = 0)
        res_v = np.bincount(vi, weights=res, minlength=nV) / np.maximum(nv, 1)
        base_v = {v_vs[j]: res_v[j] for j in range(nV) if v_good[j]}
        raw_v = np.array([res_v[j] - base_v.get(v_vs[j], 0.0) for j in range(nV)])
        prior_v = np.array([gam[v_prior[j]] if v_prior[j] >= 0 else 0.0 for j in range(nV)])
        gamv = np.where(v_good, 0.0, (nv * raw_v + K_GOING * prior_v) / (nv + K_GOING))
        if not VENUE_GOING:
            gamv = prior_v                                               # 旧 te1: 全体の γ をそのまま使う
        raw = np.bincount(ci, weights=y - a[hi] - gamv[vi] - delta[di], minlength=nC)
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
            delta = np.bincount(di, weights=y - theta[ci] - gamv[vi] - a[hi], minlength=nD) / (nd + K_DAY)
        if day_effects and VENUE_GOING:
            # 再中心化: δ の「馬場状態セル (場, 芝ダ, 馬場) ごとの平均」を γ_v (良との差) と θ (良の水準) へ移す。
            # 馬場状態は開催日ごとに決まるので δ と γ_v は見分けが付きにくく、放っておくと道悪の効果の一部が
            # δ に残る (2026-10-14 実測: 水沢 重 の日の δ 平均 −0.83%・大井 不良 +0.47%)。今日 (δ 未知) や
            # δ を推定していない日の換算には γ_v しか効かないので、馬場状態の効果は γ_v に集め、δ は
            # 「その馬場状態の日として速かったか遅かったか」だけにする。学習データ上の当てはめは変わらない。
            mv = np.bincount(vi, weights=delta[di], minlength=nV) / np.maximum(nv, 1)
            base_mv = {v_vs[j]: mv[j] for j in range(nV) if v_good[j]}
            level = np.array([base_mv.get(v_vs[j], 0.0) for j in range(nV)])
            gamv = np.where(v_good, 0.0, gamv + (mv - level))
            tr_run = mv[vi]                                                   # 各走から δ を引く量
            delta = delta - np.bincount(di, weights=tr_run, minlength=nD) / np.maximum(nd, 1)
            vs_of_cell = np.array([vs_index[(c[2], c[0])] for c in cells])
            lev_vs = np.zeros(len(vs_keys))
            for j in range(nV):
                if v_good[j]:
                    lev_vs[vs_index[v_vs[j]]] = mv[j]
            theta = theta + lev_vs[vs_of_cell]
        # 収束: θ は全体の水準が a と入れ替われるので「平均を引いた変化」で見る
        dth = (theta - prev_theta) - float(np.mean(theta - prev_theta))
        step = max(float(np.max(np.abs(dth))) if nC else 0.0,
                   float(np.max(np.abs(gamv - prev_gam))) if nV else 0.0,
                   float(np.max(np.abs(delta - prev_delta))) if nD else 0.0)
        if n_iter > 1 and step < TOL:
            break

    # 表示用の参考: 条件別の勝ち時計中央値 (馬場別 n>=10 / 全馬場 n>=5)
    # 1 レース = レース番号 r がある行 (地方の公式データ) は (場, 日, R)。無い行 (netkeiba / live) は
    # (場, 日, 芝ダ, 距離, 馬場, 勝ち時計) で同日同条件の別レースを区別する (旧 key は同日同条件を 1 レースに
    # 潰し n 過少・中央値が速めに偏った。勝ち時計が同じ別レースも 4.6% ある → r があれば r で数える)。
    # r の無い行は、同じ (場, 日, 芝ダ, 距離, 馬場, 勝ち時計) の r 付きレースがあれば二重に数えない。
    races_r: dict[tuple, tuple] = {}
    for r in rows:
        if r.get("wt") and r.get("r"):
            races_r[(r["v"], r["d"], r["r"])] = (r["v"], r["d"], r["s"], r["dist"], r["g"], r["wt"])
    seen_wkeys = set(races_r.values())
    races: dict[tuple, float] = {k: v[5] for k, v in races_r.items()}
    for r in rows:
        if r.get("wt") and not r.get("r"):
            wk = (r["v"], r["d"], r["s"], r["dist"], r["g"], r["wt"])
            if wk not in seen_wkeys:
                races[wk] = r["wt"]
    pw: dict[str, list[float]] = defaultdict(list)
    for key, wt in races.items():
        v, d, s, dist, g = (races_r[key][:5] if key in races_r else key[:5])
        pw[f"{s}|{dist}|{v}|"].append(wt)
        if g:
            pw[f"{s}|{dist}|{v}|{g}"].append(wt)
    par_win = {k: [round(float(np.median(v)), 2), len(v)] for k, v in pw.items()
               if len(v) >= (10 if not k.endswith("|") else 5)}

    return {
        "version": VERSION,
        "fitted_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "data_until": f"{last // 10000:04d}-{last // 100 % 100:02d}-{last % 100:02d}",
        "data_until_by_circuit": {c: f"{v // 10000:04d}-{v // 100 % 100:02d}-{v % 100:02d}"
                                  for c, v in sorted(last_c.items())},
        "n_runs": len(use), "n_horses": nH, "n_cells": nC, "n_days": nD,
        "params": {"K": K, "K_DAY": K_DAY, "K_GOING": K_GOING, "venue_going": VENUE_GOING,
                   "WINDOW_DAYS": WINDOW_DAYS, "MAX_BEHIND": MAX_BEHIND, "day_effects": day_effects,
                   "iterations": n_iter},
        "theta": {f"{s}|{d}|{v}": round(float(theta[i]), 6) for i, (s, d, v) in enumerate(cells)},
        "theta_n": {f"{s}|{d}|{v}": int(nc[i]) for i, (s, d, v) in enumerate(cells)},
        "gamma": {f"{c}|{s}|{g}": round(float(gam[i]), 6) for i, (c, s, g) in enumerate(glev)},
        # 場別の馬場状態効果 (換算で実際に使う。無い場・馬場は gamma → もう一方の circuit の順で補う)
        "gamma_venue": ({f"{v}|{s}|{g}": round(float(gamv[i]), 6) for i, (v, s, g) in enumerate(vlev)}
                        if VENUE_GOING else {}),
        "gamma_venue_n": {f"{v}|{s}|{g}": int(nv[i]) for i, (v, s, g) in enumerate(vlev)},
        "a_vs": {f"{v}|{s}": round(float(a_vs[i]), 6) for i, (v, s) in enumerate(vs_keys)},
        "b": {f"{c}|{s}": round(float(b[i]), 6) for i, (c, s) in enumerate(cs_keys)},
        "delta": ({f"{v}|{d}|{s}": round(float(delta[i]), 6) for i, (v, d, s) in enumerate(days)
                   if abs(delta[i]) > 1e-9} if day_effects else {}),
        "par_win": par_win,
    }


def save_model(model: dict, path: Path | None = None) -> None:
    path = path or MODEL_PATH        # 既定は呼び出し時に解決 (テストの差し替えが効くように)
    path.parent.mkdir(parents=True, exist_ok=True)
    import threading
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # 同時書き込みでも混ざらない
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
    """ledger から再学習して保存。返り値はモデル dict。明らかに学習窓の外の行は読まない。

    学習窓は「ledger の最新日」から WINDOW_DAYS。読み飛ばしは「今日」基準なので、最新データが
    古い間 (netkeiba 規制後で live ledger が薄い時期) も窓を削らないよう 1 年の余裕を持たせる。
    """
    lim = _dt.date.today() - _dt.timedelta(days=WINDOW_DAYS + LOAD_SLACK_DAYS)
    model = fit(load_ledger(paths, min_date=lim.year * 10000 + lim.month * 100 + lim.day))
    save_model(model)
    return model


def needs_rebuild(max_age_hours: float = 24.0) -> bool:
    """モデルが古く (max_age 超) かつ ledger がモデル以降に更新されているか。"""
    try:
        m_mtime = MODEL_PATH.stat().st_mtime
    except OSError:
        m_mtime = 0.0
    import time
    if m_mtime and time.time() - m_mtime < max_age_hours * 3600:
        return False
    newest = max((p.stat().st_mtime for p in LEDGER_DIR.glob("*.jsonl")), default=0.0)
    return bool(newest) and newest > m_mtime


def rebuild_if_stale(max_age_hours: float = 24.0) -> bool:
    """needs_rebuild なら同じプロセスで再学習。常駐 API は別プロセス (scripts/build_time_model.py) で回す。"""
    if not needs_rebuild(max_age_hours):
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
    return th + going_effect(model, venue, surface, going)


def going_effect(model: dict, venue: str, surface: str, going: str) -> float:
    """γ(馬場状態)。場別の値 (gamma_venue, 2026-10-14〜) → その circuit の値 → もう一方の circuit の値 の順。
    どれも無ければ 0 (=良扱い)。"""
    return going_effect_detail(model, venue, surface, going)[0]


def going_effect_detail(model: dict, venue: str, surface: str, going: str) -> tuple[float, str, int]:
    """(γ, 出どころ "venue"|"circuit"|"other"|"none", 学習に使った走数)。表示用の内訳付き版。"""
    g = _going_level(going)
    if g == "良":
        return 0.0, "venue", int((model.get("gamma_venue_n") or {}).get(f"{venue}|{surface}|{g}", 0))
    gv = model.get("gamma_venue") or {}
    key = f"{venue}|{surface}|{g}"
    if key in gv:
        return float(gv[key]), "venue", int((model.get("gamma_venue_n") or {}).get(key, 0))
    gam = model.get("gamma", {})
    c = circuit(venue)
    other = "nar" if c == "jra" else "jra"
    v = gam.get(f"{c}|{surface}|{g}")
    if v is not None:
        return float(v), "circuit", 0
    v = gam.get(f"{other}|{surface}|{g}")
    if v is not None:
        return float(v), "other", 0
    return 0.0, "none", 0


def day_effect(model: dict, venue: str, date: int, surface: str) -> float:
    return float(model.get("delta", {}).get(f"{venue}|{date}|{surface}", 0.0))


def par_win(model: dict, surface: str, distance: int, venue: str, going: str) -> tuple[float, int] | None:
    """表示用: 当該条件の勝ち時計中央値 (馬場別があれば優先)。"""
    pw = model.get("par_win", {})
    v = (pw.get(f"{surface}|{distance}|{venue}|{going}") if going else None) or pw.get(f"{surface}|{distance}|{venue}|")
    return (float(v[0]), int(v[1])) if v else None
