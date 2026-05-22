"""条件付き勝率の Beta-Binomial shrinkage 推定 + Gaussian distance kernel。

問題: 直近 5 走の馬で「同距離 ±200m 同サーフェスの勝率」を素朴に出すと、
  - 該当走 0 件 → ゼロ除算
  - 該当走 2 走で 1 勝 → 50% 推定 (実態とかけ離れた過大評価)
解: 母集団 prior (α_g, β_g) を Beta-Binomial empirical Bayes で持ち、
    馬個別データを Gaussian distance kernel で重み付け → 縮約。

事前 (prior):
  Tier 0 (実装): 暫定ハードコード prior.
    勝率: α=1.0, β=15.0 (期待値 1/16 ≒ 0.0625、有効サンプルサイズ 16)
    連対率: α=2.0, β=14.0 (期待値 0.125)
    3 連対率: α=3.0, β=13.0 (期待値 0.1875)
  Tier 1 (実装): data/raw/ の蓄積データから surface × distance_band ごとに MoM 推定。
    `data/priors/<surface>_<band>.json` に永続化。
    `load_prior_for_race(surface, distance, metric)` でレース条件に合わせて読み込む。

参考:
  Brown 2008 "In-Season Prediction of Batting Averages"
  https://arxiv.org/abs/0803.3697
"""
from __future__ import annotations

import gzip
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import PastRun

ROOT = Path(__file__).resolve().parents[1]
PRIORS_DIR = ROOT / "data" / "priors"
RAW_DIR = ROOT / "data" / "raw"


# --- 暫定 prior (Tier 0) ---
@dataclass(frozen=True)
class BetaPrior:
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


DEFAULT_WIN_PRIOR = BetaPrior(alpha=1.0, beta=15.0)       # 期待値 6.25%
DEFAULT_PLACE_PRIOR = BetaPrior(alpha=2.0, beta=14.0)      # 連対 期待値 12.5%
DEFAULT_SHOW_PRIOR = BetaPrior(alpha=3.0, beta=13.0)       # 3 着内 期待値 18.75%


# --- Gaussian distance kernel ---

DEFAULT_DISTANCE_SIGMA = 200.0  # 200m バンド (±1σ)


def distance_weight(d_past: int, d_target: int, sigma: float = DEFAULT_DISTANCE_SIGMA) -> float:
    """過去走の距離が当該レース距離からどれだけ離れているかでガウス重み。

    d_past == d_target で重み 1、|Δ| = sigma で 0.61、|Δ| = 2σ で 0.14 程度。
    """
    if d_past <= 0 or d_target <= 0:
        return 0.0
    delta = d_past - d_target
    return math.exp(-(delta * delta) / (2.0 * sigma * sigma))


# --- 条件付き shrunk 勝率 ---


def conditional_shrunk_rate(
    past_runs: list[PastRun],
    *,
    target_distance: int,
    target_surface: str,
    metric: str = "win",  # "win" | "place" | "show"
    prior: BetaPrior | None = None,
    distance_sigma: float = DEFAULT_DISTANCE_SIGMA,
    require_same_surface: bool = True,
) -> float:
    """過去走から (距離・サーフェス条件付き) shrinkage された勝率を推定。

    式:
        n_eff = Σ w_k                      (k は当該条件を満たす過去走)
        y_eff = Σ w_k · 1[metric_k]
        θ̂   = (y_eff + α) / (n_eff + α + β)

    metric:
        "win"   → finish_pos == 1
        "place" → finish_pos ∈ {1, 2}
        "show"  → finish_pos ∈ {1, 2, 3}
        注: 馬柱は 4 着以下を区別しないため、finish_pos が None なら全部「着外」扱い。

    require_same_surface=True (デフォルト) ではサーフェスが違う過去走は重み 0。
    """
    # prior が指定されていなければ data/priors/ から条件付きで取りに行く
    if prior is None:
        prior = load_prior_for_race(target_surface, target_distance, metric=metric)

    if metric == "win":
        is_hit = lambda r: r.finish_pos == 1
    elif metric == "place":
        is_hit = lambda r: r.finish_pos in (1, 2)
    elif metric == "show":
        is_hit = lambda r: r.finish_pos in (1, 2, 3)
    else:
        raise ValueError(f"unsupported metric: {metric}")

    n_eff = 0.0
    y_eff = 0.0
    for r in past_runs:
        if require_same_surface and r.surface != target_surface:
            continue
        w = distance_weight(r.distance, target_distance, sigma=distance_sigma)
        if w <= 0:
            continue
        n_eff += w
        if is_hit(r):
            y_eff += w

    return (y_eff + prior.alpha) / (n_eff + prior.alpha + prior.beta)


def naive_recent_rate(past_runs: list[PastRun], metric: str = "win") -> float:
    """shrinkage しない素朴な直近勝率 (比較用)。"""
    if not past_runs:
        return 0.0
    if metric == "win":
        is_hit = lambda r: r.finish_pos == 1
    elif metric == "place":
        is_hit = lambda r: r.finish_pos in (1, 2)
    elif metric == "show":
        is_hit = lambda r: r.finish_pos in (1, 2, 3)
    else:
        raise ValueError(f"unsupported metric: {metric}")
    hits = sum(1 for r in past_runs if is_hit(r))
    return hits / len(past_runs)


def effective_sample_size(
    past_runs: list[PastRun],
    *,
    target_distance: int,
    target_surface: str,
    distance_sigma: float = DEFAULT_DISTANCE_SIGMA,
) -> float:
    """重み付け後の effective sample size。shrinkage の強さを目視するための診断用。"""
    s = 0.0
    for r in past_runs:
        if r.surface != target_surface:
            continue
        s += distance_weight(r.distance, target_distance, sigma=distance_sigma)
    return s


# --- 母集団 prior 推定 (empirical Bayes from data/raw/) ---


def _distance_band(distance: int, band_width: int = 200) -> int:
    """距離を band 単位に丸める (例: 1234 → 1200, 1389 → 1400)。"""
    if distance <= 0:
        return 0
    return int(round(distance / band_width) * band_width)


def _mom_beta_params(rates: list[float], n_obs: list[int]) -> tuple[float, float]:
    """Method of Moments で Beta(α, β) を推定。

    p_i = wins_i / starts_i, w_i = starts_i / Σ starts_j を重みとする。
    μ = Σ w_i p_i (weighted mean)
    Var を計算し、Beta-Binomial の dispersion から α + β を求める。
    fallback: 標本平均が不安定なら (α=1, β=15) を返す。
    """
    total_n = sum(n_obs)
    if total_n <= 0 or not rates:
        return 1.0, 15.0
    weights = [n / total_n for n in n_obs]
    mu = sum(w * r for w, r in zip(weights, rates))
    if mu <= 0 or mu >= 1:
        return 1.0, 15.0
    # weighted variance
    var = sum(w * (r - mu) ** 2 for w, r in zip(weights, rates))
    # Beta-Binomial dispersion: var = mu(1-mu) / (1 + (α+β-1) * weight_factor)
    # Simplified: alpha + beta ≈ mu(1-mu) / var - 1 (when n_bar is moderate)
    n_bar = total_n / len(rates) if rates else 1.0
    inner = mu * (1 - mu)
    if var <= 0 or var >= inner:
        # 分散が極大 → 弱い prior に
        return mu * 4, (1 - mu) * 4
    # phi 推定
    phi = (var - inner / n_bar) / (inner - var / n_bar + var)
    phi = max(min(phi, 0.5), 1e-4)
    alpha = mu * (1 - phi) / phi
    beta = (1 - mu) * (1 - phi) / phi
    # 異常値ガード
    if alpha <= 0 or beta <= 0 or alpha + beta > 500:
        return mu * 16, (1 - mu) * 16
    return alpha, beta


def build_priors(*, min_horses_per_bucket: int = 50) -> dict[tuple[str, int], dict]:
    """data/raw/ の全 race から (surface, distance_band) 別 Beta-Binomial prior を推定。

    各バケットで:
      - 各馬の (wins / starts) を集計
      - method of moments で α, β を推定 (win/place/show 別)
    結果を data/priors/{surface}_{band}.json に保存。

    バケットの horse-races 数が `min_horses_per_bucket` 未満ならスキップ (default prior 使用)。
    """
    from .parse import parse_result, parse_shutuba

    # bucket → horse_id → (starts, wins, places, shows)
    buckets: dict[tuple[str, int], dict[str, list[int]]] = {}

    # data/raw/ から (surface, distance, horse_id, finish) を抽出
    rids: set[str] = set()
    for p in RAW_DIR.glob("*-shutuba.html.gz"):
        rid = p.name.split("-shutuba")[0]
        rids.add(rid)

    n_races_used = 0
    for rid in rids:
        sh_path = RAW_DIR / f"{rid}-shutuba.html.gz"
        res_path = RAW_DIR / f"{rid}-result.html.gz"
        if not sh_path.exists() or not res_path.exists():
            continue
        try:
            sh = gzip.open(sh_path, "rt", encoding="utf-8").read()
            rd = parse_shutuba(sh, race_id=rid)
            res_html = gzip.open(res_path, "rt", encoding="utf-8").read()
            res = parse_result(res_html)
            if not res or len(res.get("finish_order") or []) < 3:
                continue
        except (OSError, EOFError):
            continue
        n_races_used += 1
        finish = {int(n): i + 1 for i, n in enumerate(res["finish_order"])}
        band = _distance_band(rd.race.distance)
        surface = rd.race.surface
        if not surface or band == 0:
            continue
        key = (surface, band)
        bucket = buckets.setdefault(key, {})
        for h in rd.race.horses:
            if h.absent or not h.horse_id:
                continue
            rec = bucket.setdefault(h.horse_id, [0, 0, 0, 0])  # [starts, wins, places, shows]
            rec[0] += 1
            pos = finish.get(h.number)
            if pos == 1:
                rec[1] += 1
            if pos in (1, 2):
                rec[2] += 1
            if pos in (1, 2, 3):
                rec[3] += 1

    out: dict[tuple[str, int], dict] = {}
    PRIORS_DIR.mkdir(parents=True, exist_ok=True)
    for (surface, band), bucket in buckets.items():
        rates_w, rates_p, rates_s = [], [], []
        starts = []
        for _, (s, w, p, sh) in bucket.items():
            if s <= 0:
                continue
            rates_w.append(w / s)
            rates_p.append(p / s)
            rates_s.append(sh / s)
            starts.append(s)
        total_horses = len(starts)
        total_starts = sum(starts)
        if total_starts < min_horses_per_bucket:
            continue
        alpha_w, beta_w = _mom_beta_params(rates_w, starts)
        alpha_p, beta_p = _mom_beta_params(rates_p, starts)
        alpha_s, beta_s = _mom_beta_params(rates_s, starts)
        meta = {
            "surface": surface,
            "distance_band": band,
            "n_horses": total_horses,
            "n_horse_races": total_starts,
            "win":   {"alpha": round(alpha_w, 4), "beta": round(beta_w, 4)},
            "place": {"alpha": round(alpha_p, 4), "beta": round(beta_p, 4)},
            "show":  {"alpha": round(alpha_s, 4), "beta": round(beta_s, 4)},
        }
        out[(surface, band)] = meta
        # ファイル名はサーフェス記号化
        sname = {"芝": "turf", "ダート": "dirt", "障害": "jump"}.get(surface, surface)
        fname = f"{sname}_{band}.json"
        (PRIORS_DIR / fname).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    # 全体サマリ
    summary_path = PRIORS_DIR / "_summary.json"
    summary_path.write_text(json.dumps({
        "n_races_used": n_races_used,
        "n_buckets": len(out),
        "buckets": [
            {"surface": k[0], "distance_band": k[1], **{m: v[m] for m in ("win", "place", "show")},
             "n_horses": v["n_horses"], "n_horse_races": v["n_horse_races"]}
            for k, v in sorted(out.items())
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# load_prior_for_race: メモリキャッシュ付き
_PRIOR_CACHE: dict[tuple[str, int, str], BetaPrior] = {}


def load_prior_for_race(surface: str, distance: int, metric: str = "win") -> BetaPrior:
    """data/priors/{sname}_{band}.json があれば読む。無ければハードコード default。

    metric: "win" | "place" | "show"
    """
    band = _distance_band(distance)
    cache_key = (surface, band, metric)
    if cache_key in _PRIOR_CACHE:
        return _PRIOR_CACHE[cache_key]

    sname = {"芝": "turf", "ダート": "dirt", "障害": "jump"}.get(surface, "")
    if sname and band > 0:
        fp = PRIORS_DIR / f"{sname}_{band}.json"
        if fp.exists():
            try:
                meta = json.loads(fp.read_text(encoding="utf-8"))
                m = meta.get(metric)
                if m and m.get("alpha") and m.get("beta"):
                    prior = BetaPrior(alpha=float(m["alpha"]), beta=float(m["beta"]))
                    _PRIOR_CACHE[cache_key] = prior
                    return prior
            except (json.JSONDecodeError, OSError):
                pass

    # フォールバック
    default = {
        "win": DEFAULT_WIN_PRIOR,
        "place": DEFAULT_PLACE_PRIOR,
        "show": DEFAULT_SHOW_PRIOR,
    }.get(metric, DEFAULT_WIN_PRIOR)
    _PRIOR_CACHE[cache_key] = default
    return default


# --- CLI ---

def _cli():
    import typer
    from rich.console import Console
    from rich.table import Table

    app = typer.Typer(add_completion=False, no_args_is_help=False)
    console = Console()

    @app.command()
    def build(min_horses: int = 50):
        """data/raw/ から (surface, distance_band) 別 prior を推定して data/priors/ に保存。"""
        console.print(f"building priors (min_horses_per_bucket={min_horses}) ...")
        out = build_priors(min_horses_per_bucket=min_horses)
        if not out:
            console.print("[yellow]データ不足: data/raw に shutuba+result セットがありません[/yellow]")
            raise typer.Exit(1)
        tbl = Table(title=f"母集団 prior ({len(out)} buckets)")
        tbl.add_column("surface")
        tbl.add_column("dist", justify="right")
        tbl.add_column("n_horses", justify="right")
        tbl.add_column("n_starts", justify="right")
        tbl.add_column("win α", justify="right")
        tbl.add_column("win β", justify="right")
        tbl.add_column("win μ", justify="right")
        for (surface, band), meta in sorted(out.items()):
            a, b = meta["win"]["alpha"], meta["win"]["beta"]
            mu = a / (a + b) if (a + b) > 0 else 0
            tbl.add_row(
                surface, str(band), str(meta["n_horses"]), str(meta["n_horse_races"]),
                f"{a:.3f}", f"{b:.3f}", f"{mu*100:.2f}%",
            )
        console.print(tbl)
        console.print(f"[green]saved to {PRIORS_DIR}/[/green]")

    app()


if __name__ == "__main__":
    _cli()
