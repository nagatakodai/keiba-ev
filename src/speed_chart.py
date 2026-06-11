"""実データ par + pace + trip 由来の v2 速度図表 (単一実装)。

`scripts/build_v2_features.py` (オフライン parquet 生成) と `src/ev.py`
(live fundamental への並列合成) が **同じ式**を使うよう、図表計算をここに集約する。
par table = `data/cache/par_times.json` (winner_time / last3f の condition 別 median)。

シグナル定義:
  - speed_v2: 実データ par からの自走破タイム差 (秒 → 指数点)。速い = +。
  - pace_v2:  上がり3F を condition 別 par_last3f と比較した終い脚。速い終い = +。
  - trip:     通過順 (passing) から前後位置・位置取り変化 (タイムに出ない利/不利)。

leakage 防止: past_runs は馬柱なので構造的に対象 race 以前のみ。live (発走前) では対象
race は未走なので含まれない。par table 自体は全 race 集計なので「平均的な時計水準」であり
個馬の結果ではない (市場も同じ par を見ている前提)。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PAR_PATH = ROOT / "data" / "cache" / "par_times.json"

PTS_PER_SEC = 10.0          # 1 秒 = 10 指数点 (Beyer 流スケール)
WEIGHTS = (0.5, 0.3, 0.2)   # 直近3走の加重平均 (新しい順)

_PAR_CACHE: dict | None = None


def _par_table() -> dict:
    global _PAR_CACHE
    if _PAR_CACHE is None:
        try:
            _PAR_CACHE = json.loads(PAR_PATH.read_text())
        except (FileNotFoundError, ValueError):
            _PAR_CACHE = {}
    return _PAR_CACHE


def _bucket(dist: int) -> int:
    return int(round(dist / 100.0) * 100)


# par table のキーは netkeiba 馬柱由来の 1 文字語彙 (稍/不)。keibago/JRA 馬柱は
# 「稍重/不良」(2文字) なので正規化しないと厳密キーが当たらず、稍重/不良の走が
# 全馬場混合の venue-median par で指数化されていた (2026-06-11 bughunt 第5R)。
_GOING_NORM = {"稍重": "稍", "不良": "不"}


def par_lookup(table_key: str, surf: str, dist: int, ven: str, going: str):
    """par lookup: 厳密キー → 馬場落とす → 場落とす → 両方落とす → surface|bucket 集約。"""
    going = _GOING_NORM.get(going, going)
    tab = _par_table().get(table_key, {})
    b = _bucket(dist)
    for k in (f"{surf}|{b}|{ven}|{going}", f"{surf}|{b}|{ven}|",
              f"{surf}|{b}||{going}", f"{surf}|{b}||"):
        if k in tab:
            return tab[k]["median"]
    vals = [v["median"] for kk, v in tab.items() if kk.startswith(f"{surf}|{b}|")]
    return float(np.median(vals)) if vals else None


def run_figs(pr) -> dict | None:
    """1 走 (PastRun) → speed_v2 / pace_v2 / trip 図表 (取れた指標のみ)。"""
    surf = getattr(pr, "surface", "") or ""
    dist = getattr(pr, "distance", 0) or 0
    if not surf or dist <= 0:
        return None
    ven = getattr(pr, "venue", "") or ""
    going = getattr(pr, "going", "") or ""
    wt = getattr(pr, "winner_time_sec", 0) or 0
    diff = getattr(pr, "time_diff_sec", 0) or 0
    out: dict = {}
    par_wt = par_lookup("winner_time", surf, dist, ven, going)
    if wt > 0 and par_wt:
        own = wt + diff   # 自走破タイム = 勝ち時計 + 着差(秒)
        out["speed_v2"] = (par_wt - own) * PTS_PER_SEC
    l3 = getattr(pr, "last_3f_sec", 0) or 0
    par_l3 = par_lookup("last3f", surf, dist, ven, going)
    if l3 > 0 and par_l3:
        out["pace_v2"] = (par_l3 - l3) * PTS_PER_SEC
    passing = getattr(pr, "passing", "") or ""
    nums = [int(x) for x in passing.replace("-", " ").split() if x.isdigit()]
    fs = getattr(pr, "field_size", 0) or 0
    if nums and fs > 0:
        early, late = nums[0], nums[-1]
        out["front"] = 1.0 if early <= 2 else 0.0
        out["gain"] = (early - late) / fs   # 正 = 位置を上げた (差し)
    return out or None


def wavg(vals: list[float]) -> float:
    if not vals:
        return 0.0
    v = vals[:3]
    w = WEIGHTS[:len(v)]
    return float(sum(a * b for a, b in zip(v, w)) / sum(w))


def horse_figures(past_runs) -> dict:
    """馬柱 (PastRun のリスト, 新しい順) → 集約図表 dict。

    build_v2_features.py の per-horse 行と同じ定義 (race_id/horse_number を除く)。
    """
    sp: list[float] = []
    pc: list[float] = []
    gains: list[float] = []
    fronts: list[float] = []
    for pr in (past_runs or []):
        f = run_figs(pr)
        if not f:
            continue
        if "speed_v2" in f:
            sp.append(f["speed_v2"])
        if "pace_v2" in f:
            pc.append(f["pace_v2"])
        if "gain" in f:
            gains.append(f["gain"])
            fronts.append(f["front"])
    return {
        "speed_v2_wavg": wavg(sp),
        "speed_v2_best": float(max(sp)) if sp else 0.0,
        "pace_v2_wavg": wavg(pc),
        "trip_gain_avg": float(np.mean(gains)) if gains else 0.0,
        "front_rate": float(np.mean(fronts)) if fronts else 0.0,
        "v2_n_runs": len(sp),
    }


def horse_charts(horses) -> dict[int, dict]:
    """非取消馬の馬番 → 集約図表。記録/表示 (snapshot) 用。"""
    return {
        h.number: horse_figures(getattr(h, "past_runs", None))
        for h in horses
        if not getattr(h, "absent", False)
    }


def speed_v2_win_probs(horses, *, temperature: float = 1.0,
                       min_coverage: float = 0.5) -> dict[int, float] | None:
    """live: 各馬の馬柱から speed_v2_best を field 内 z-score → softmax で 1着率分布。

    検証 (nar_dirt_speed_strategy.py) で standalone top-1 戦略に使われた speed_v2_best を
    シグナルに採る。図表データが薄い (有効馬が 3 頭未満 or field の min_coverage 未満) なら
    None を返し、呼び出し側は LightGBM 単独にフォールバックする。

    no-data 馬 (v2_n_runs==0) は z=0 (field 平均) に置き、不当な抑制/過大評価を避ける。
    """
    figs = horse_charts(horses)
    if not figs:
        return None
    with_data = {n: f["speed_v2_best"] for n, f in figs.items() if f["v2_n_runs"] > 0}
    if len(with_data) < 3 or len(with_data) < min_coverage * len(figs):
        return None
    xs = list(with_data.values())
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / max(len(xs) - 1, 1)
    sd = var ** 0.5
    t = max(temperature, 1e-6)
    logits: dict[int, float] = {}
    for n in figs:
        if n in with_data and sd > 1e-6:
            z = (with_data[n] - mean) / sd
        else:
            z = 0.0   # データ無し or 全馬同値 → 中立
        logits[n] = z / t
    m = max(logits.values())
    exps = {n: math.exp(v - m) for n, v in logits.items()}
    s = sum(exps.values()) or 1.0
    return {n: v / s for n, v in exps.items()}
