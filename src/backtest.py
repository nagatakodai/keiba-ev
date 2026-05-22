"""バックテストハーネス — モデル変種を過去レースに適用して定量比較する。

`src/calibrate.py` がレース snapshot と result を突合して tier 別 ratio / Plan ROI を
出すのに対し、`backtest.py` は次の研究レベル指標群を追加する:

  - **log loss** (negative log-likelihood of actual triple)
  - **Brier score** (Σ (p − 1[actual])² over all triples)
  - **ECE** (expected calibration error, quantile-binned)
  - **top-K accuracy** (actual triple が P×O 上位 K に入った割合)
  - **market baseline** (1/odds を素朴 prior として同じ指標)

設計方針:
  - Phase D (本ファイル) は **既存の data/predictions/ snapshot** を入力にする版を
    まず作る。snapshot には rows = [(key, odds, popularity, prob, px_o), ...] が
    入っているので、これだけで全指標が計算できる。
  - Phase A-C (新モデル) を実装したら、`backtest_with_model` が raw HTML を
    再パース → 新 `estimate_probs` を呼ぶ → 同 harness で比較できる、というように
    interface を分けてある。

CLI:
  make backtest                     # 全 snapshot で current モデル指標
  python -m src.backtest --since 20260501 --until 20260601
  python -m src.backtest --baseline  # 市場ベースラインも一緒に出す
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RESULT_DIR = ROOT / "data" / "results"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


# --- データクラス ---


@dataclass
class TripleRow:
    """1 個の 3 連単買い目に対するモデル出力 + 実オッズ。"""
    key: tuple[int, int, int]
    odds: float
    popularity: int
    prob: float                  # モデル予測 1 着率 (Plackett-Luce 連鎖済の 3 連単 prob)
    px_o: float

    @classmethod
    def from_snapshot_row(cls, r: dict) -> "TripleRow":
        return cls(
            key=tuple(r["key"]),  # type: ignore[arg-type]
            odds=float(r["odds"]),
            popularity=int(r.get("popularity", 0)),
            prob=float(r["prob"]),
            px_o=float(r["px_o"]),
        )


@dataclass
class RaceCase:
    """1 レース分の backtest 入力。"""
    race_id: str
    venue: str
    distance: int
    surface: str
    rows: list[TripleRow]            # 全 3 連単オッズ + モデル prob
    finish: tuple[int, int, int]     # 実着順 (1, 2, 3 着 馬番)
    payout: int                      # 3 連単 100 円あたり払戻


@dataclass
class RaceMetrics:
    """1 レース 1 モデル の指標。"""
    race_id: str
    log_loss: float                  # -log(p[actual])
    brier: float                     # Σ(p - 1[actual])² over rows
    rank_of_actual: int              # actual triple の P×O 順位 (1-based、不在=0)
    actual_p: float                  # モデルが actual に振った prob
    actual_odds: float               # 実オッズ
    n_rows: int


@dataclass
class AggregateMetrics:
    """複数レースの集約指標。"""
    name: str
    n_races: int = 0
    n_rows_total: int = 0
    mean_log_loss: float = 0.0
    mean_brier: float = 0.0
    top1_acc: float = 0.0
    top10_acc: float = 0.0
    top30_acc: float = 0.0
    top100_acc: float = 0.0
    ece: float = 0.0
    reliability: list[tuple[float, float, int]] = field(default_factory=list)
    # (bin_mean_predicted, bin_actual_freq, bin_count)
    # Synthetic ROI (Plan A/B/C を毎レース当てた場合の累積回収率)
    plan_a_stake: int = 0
    plan_a_payout: int = 0
    plan_b_stake: int = 0
    plan_b_payout: int = 0
    plan_c_stake: int = 0
    plan_c_payout: int = 0
    plan_a_hits: int = 0
    plan_b_hits: int = 0
    plan_c_hits: int = 0

    @property
    def plan_a_roi(self) -> float:
        return self.plan_a_payout / self.plan_a_stake if self.plan_a_stake > 0 else 0.0

    @property
    def plan_b_roi(self) -> float:
        return self.plan_b_payout / self.plan_b_stake if self.plan_b_stake > 0 else 0.0

    @property
    def plan_c_roi(self) -> float:
        return self.plan_c_payout / self.plan_c_stake if self.plan_c_stake > 0 else 0.0


# --- モデルインタフェース ---

# RaceCase.rows を受けて、各 row.prob を上書きした新 rows を返す Callable。
# - current_model_passthrough = snapshot に保存済の prob を使う (なにもしない)
# - market_baseline = 1/odds を正規化して prob に上書き
# - 将来 Phase A-C の新モデルもこの interface で plug する
ModelFn = Callable[[RaceCase], list[TripleRow]]


def current_model_passthrough(case: RaceCase) -> list[TripleRow]:
    """snapshot に保存済の prob をそのまま使う (= 取得時点の現モデル)。"""
    return case.rows


def market_baseline(case: RaceCase) -> list[TripleRow]:
    """市場ベースライン: 1/odds を正規化したものを prob にする (控除率は正規化で吸収)。

    モデルが市場以上の情報を出せているかを判定するベンチマーク。
    """
    raw = [(r, 1.0 / r.odds if r.odds > 0 else 0.0) for r in case.rows]
    s = sum(p for _, p in raw)
    if s <= 0:
        return case.rows
    out: list[TripleRow] = []
    for r, p in raw:
        new_prob = p / s
        out.append(TripleRow(
            key=r.key, odds=r.odds, popularity=r.popularity,
            prob=new_prob, px_o=new_prob * r.odds,
        ))
    out.sort(key=lambda x: x.px_o, reverse=True)
    return out


def make_rerun_model(market_blend: float = 0.4) -> ModelFn:
    """raw HTML から RaceData を再構築 → 現 estimate_probs を回し直して TripleRow を返す。

    backtest 時に最新の estimate_probs (LightGBM 学習済モデル含む) を当てるため。
    snapshot の rows は odds 情報源として使うが、prob は新モデルで上書きする。
    """
    import gzip
    from pathlib import Path as _Path

    from .ev import build_table, estimate_probs
    from .models import TrifectaOdds
    from .parse import parse_past_runs, parse_shutuba

    root = _Path(__file__).resolve().parents[1]
    raw_dir = root / "data" / "raw"

    def model(case: RaceCase) -> list[TripleRow]:
        rid = case.race_id
        # 正規化 ID (cup_id-schedule-race) を bulk_fetch の生 race_id に逆引き
        # snapshot の race_id は cup_id-schedule-race 形式なので、shutuba HTML を
        # ファイル名でマッチさせる必要あり。最初に試した一致 OR scan で対応する。
        netkeiba_rid = _resolve_netkeiba_rid(rid, raw_dir)
        if not netkeiba_rid:
            return case.rows
        sh_path = raw_dir / f"{netkeiba_rid}-shutuba.html.gz"
        past_path = raw_dir / f"{netkeiba_rid}-past.html.gz"
        if not sh_path.exists():
            return case.rows
        try:
            sh_html = gzip.open(sh_path, "rt", encoding="utf-8").read()
            rd = parse_shutuba(sh_html, race_id=netkeiba_rid)
            if past_path.exists():
                past_html = gzip.open(past_path, "rt", encoding="utf-8").read()
                runs = parse_past_runs(past_html)
                for h in rd.race.horses:
                    h.past_runs = runs.get(h.number, [])
            # オッズは snapshot の rows から復元 (再 fetch しない)
            rd.trifecta = [
                TrifectaOdds(key=r.key, odds=r.odds, popularity=r.popularity)
                for r in case.rows
            ]
            probs = estimate_probs(rd, market_blend=market_blend)
            ev_rows = build_table(rd, probs)
            return [
                TripleRow(
                    key=er.key, odds=er.odds, popularity=er.popularity,
                    prob=er.prob, px_o=er.px_o,
                )
                for er in ev_rows
            ]
        except Exception:
            return case.rows

    return model


def _resolve_netkeiba_rid(normalized_rid: str, raw_dir) -> str | None:
    """`2026440521-521-9` (cup_id-sched-no) のような正規化 ID から、
    raw_dir 内の対応する `<netkeiba_rid>-shutuba.html.gz` の netkeiba 12 桁 ID を逆引き。
    """
    # 正規化 ID から netkeiba ID を再構築:
    # NAR: cup_id=YYYY+PP+MMDD (10 桁), sched=MMDD, race_no
    #   → netkeiba_rid = cup_id + zero-padded race_no = 12 桁
    # JRA: cup_id=YYYY+PP+回 (8 桁), sched=日 (1-2 桁), race_no
    #   → netkeiba_rid = cup_id + zero-pad sched + zero-pad race_no = 12 桁
    parts = normalized_rid.split("-")
    if len(parts) != 3:
        return None
    cup_id, sched_s, race_no_s = parts
    try:
        sched = int(sched_s)
        race_no = int(race_no_s)
    except ValueError:
        return None
    if len(cup_id) == 10:
        # NAR
        rid = f"{cup_id}{race_no:02d}"
    elif len(cup_id) == 8:
        # JRA
        rid = f"{cup_id}{sched:02d}{race_no:02d}"
    else:
        return None
    if (raw_dir / f"{rid}-shutuba.html.gz").exists():
        return rid
    return None


# --- 指標計算 ---


def evaluate_race(case: RaceCase, model: ModelFn) -> RaceMetrics:
    """1 レース 1 モデルの指標を計算。"""
    rows = model(case)
    actual_p = 0.0
    actual_odds = 0.0
    rank = 0
    brier = 0.0

    # rank 用に P×O 降順 (current_model 経由の場合は元から sorted)
    sorted_rows = sorted(rows, key=lambda r: r.px_o, reverse=True)
    for i, r in enumerate(sorted_rows, 1):
        if r.key == case.finish:
            rank = i
            actual_p = r.prob
            actual_odds = r.odds
        # Brier: 全 row について (p - target)²。target は actual のみ 1、他は 0。
        target = 1.0 if r.key == case.finish else 0.0
        brier += (r.prob - target) ** 2

    log_loss = -math.log(max(actual_p, 1e-12))
    return RaceMetrics(
        race_id=case.race_id,
        log_loss=log_loss,
        brier=brier,
        rank_of_actual=rank,
        actual_p=actual_p,
        actual_odds=actual_odds,
        n_rows=len(rows),
    )


def aggregate(
    name: str,
    per_race: list[RaceMetrics],
    rows_all_races: list[tuple[float, float]],
    *,
    n_bins: int = 10,
) -> AggregateMetrics:
    """複数レース集約 + ECE / reliability diagram (quantile binning)。

    rows_all_races: 全レースの全 row の (predicted_prob, actual_indicator) リスト。
    quantile binning は equal-frequency なので、確率の極端な分布でも各 bin が空に
    ならず ECE が安定する (Yurelu 2026: equal-width binning は人工的な改善を示す)。
    """
    n = len(per_race)
    if n == 0:
        return AggregateMetrics(name=name)

    mean_log_loss = sum(m.log_loss for m in per_race) / n
    mean_brier = sum(m.brier for m in per_race) / n

    def topk(k: int) -> float:
        hits = sum(1 for m in per_race if 1 <= m.rank_of_actual <= k)
        return hits / n

    # Quantile-binned reliability
    if not rows_all_races:
        return AggregateMetrics(
            name=name, n_races=n, n_rows_total=0,
            mean_log_loss=mean_log_loss, mean_brier=mean_brier,
            top1_acc=topk(1), top10_acc=topk(10), top30_acc=topk(30), top100_acc=topk(100),
            ece=0.0, reliability=[],
        )
    sorted_pairs = sorted(rows_all_races, key=lambda x: x[0])
    total = len(sorted_pairs)
    bin_size = max(total // n_bins, 1)
    reliability: list[tuple[float, float, int]] = []
    ece = 0.0
    for b in range(n_bins):
        lo = b * bin_size
        hi = (b + 1) * bin_size if b < n_bins - 1 else total
        chunk = sorted_pairs[lo:hi]
        if not chunk:
            continue
        bin_mean_pred = sum(p for p, _ in chunk) / len(chunk)
        bin_actual_freq = sum(y for _, y in chunk) / len(chunk)
        reliability.append((bin_mean_pred, bin_actual_freq, len(chunk)))
        ece += (len(chunk) / total) * abs(bin_mean_pred - bin_actual_freq)

    return AggregateMetrics(
        name=name,
        n_races=n,
        n_rows_total=total,
        mean_log_loss=mean_log_loss,
        mean_brier=mean_brier,
        top1_acc=topk(1),
        top10_acc=topk(10),
        top30_acc=topk(30),
        top100_acc=topk(100),
        ece=ece,
        reliability=reliability,
    )


# --- データロード ---


def load_cases_from_snapshots(
    *,
    since: str | None = None,
    until: str | None = None,
) -> Iterator[RaceCase]:
    """data/predictions/ と data/results/ を race_id で join して RaceCase を yield。

    since/until は race_id 文字列の上界・下界 (例 "2026300521" — YYYYPPMMDD 部分まで)。
    """
    if not PRED_DIR.exists():
        return
    for pred_path in sorted(PRED_DIR.glob("*.json")):
        race_id = pred_path.stem
        if since and race_id < since:
            continue
        if until and race_id > until:
            continue
        result_path = RESULT_DIR / f"{race_id}.json"
        if not result_path.exists():
            continue
        try:
            pred = json.loads(pred_path.read_text(encoding="utf-8"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        finish_list = result.get("finish_order") or []
        if len(finish_list) < 3:
            continue
        rows = [TripleRow.from_snapshot_row(r) for r in pred.get("rows", [])]
        if not rows:
            continue
        yield RaceCase(
            race_id=race_id,
            venue=pred.get("venue_name", ""),
            distance=int(pred.get("distance") or 0),
            surface=pred.get("surface", ""),
            rows=rows,
            finish=(int(finish_list[0]), int(finish_list[1]), int(finish_list[2])),
            payout=int(result.get("trifecta_payout") or 0),
        )


def run_models(
    cases: Sequence[RaceCase],
    models: dict[str, ModelFn],
    *,
    point_cost: int = 100,
) -> dict[str, AggregateMetrics]:
    """各モデルに対して全レースを評価し、集約指標を返す。

    Plan A/B/C synthetic ROI も計算 (各レースで Plan を組み、的中したら配当を加算)。
    """
    from .ev import EvRow, plan_balanced, plan_max_ev, plan_wide

    out: dict[str, AggregateMetrics] = {}
    for name, model_fn in models.items():
        per_race: list[RaceMetrics] = []
        rows_for_ece: list[tuple[float, float]] = []
        plan_a_stake = plan_a_payout = plan_a_hits = 0
        plan_b_stake = plan_b_payout = plan_b_hits = 0
        plan_c_stake = plan_c_payout = plan_c_hits = 0

        for case in cases:
            m = evaluate_race(case, model_fn)
            per_race.append(m)
            evaluated_rows = model_fn(case)
            for r in evaluated_rows:
                target = 1.0 if r.key == case.finish else 0.0
                rows_for_ece.append((r.prob, target))

            # Plan synthetic ROI: TripleRow を EvRow に詰め替え、plan 関数に通す
            ev_rows = [
                EvRow(
                    key=r.key, odds=r.odds, popularity=r.popularity,
                    prob=r.prob, px_o=r.px_o,
                    tier=_tier_for_pxo(r.px_o),
                )
                for r in evaluated_rows
            ]
            ev_rows.sort(key=lambda x: x.px_o, reverse=True)
            for plan_label, plan_fn, (stake_acc, payout_acc, hits_acc) in [
                ("A", plan_balanced, (plan_a_stake, plan_a_payout, plan_a_hits)),
                ("B", plan_max_ev,   (plan_b_stake, plan_b_payout, plan_b_hits)),
                ("C", plan_wide,     (plan_c_stake, plan_c_payout, plan_c_hits)),
            ]:
                try:
                    plan_rows = plan_fn(ev_rows)
                except Exception:
                    plan_rows = []
                n_pts = len(plan_rows)
                if n_pts == 0:
                    continue
                stake = n_pts * point_cost
                hit = any(r.key == case.finish for r in plan_rows)
                payout = case.payout if (hit and case.payout > 0) else 0
                if plan_label == "A":
                    plan_a_stake += stake
                    plan_a_payout += payout
                    if hit:
                        plan_a_hits += 1
                elif plan_label == "B":
                    plan_b_stake += stake
                    plan_b_payout += payout
                    if hit:
                        plan_b_hits += 1
                elif plan_label == "C":
                    plan_c_stake += stake
                    plan_c_payout += payout
                    if hit:
                        plan_c_hits += 1

        agg = aggregate(name, per_race, rows_for_ece)
        agg.plan_a_stake = plan_a_stake
        agg.plan_a_payout = plan_a_payout
        agg.plan_a_hits = plan_a_hits
        agg.plan_b_stake = plan_b_stake
        agg.plan_b_payout = plan_b_payout
        agg.plan_b_hits = plan_b_hits
        agg.plan_c_stake = plan_c_stake
        agg.plan_c_payout = plan_c_payout
        agg.plan_c_hits = plan_c_hits
        out[name] = agg
    return out


def _tier_for_pxo(pxo: float) -> str:
    # ev.py の _tier と同じロジック (循環 import 回避のためミラー)
    from .ev import PXO_CHUANA, PXO_FLOOR, PXO_HONSEN
    if pxo < PXO_FLOOR:
        return "minus"
    if pxo <= PXO_HONSEN[1]:
        return "honsen"
    if pxo <= PXO_CHUANA[1]:
        return "chuana"
    return "oana"


# --- 表示 ---


def print_summary(metrics: dict[str, AggregateMetrics]) -> None:
    if not metrics:
        console.print("[yellow]対象レースなし[/yellow]")
        return
    first = next(iter(metrics.values()))
    tbl = Table(
        title=f"バックテスト指標 (n={first.n_races} races, {first.n_rows_total:,} rows total)",
        show_lines=False,
    )
    tbl.add_column("Model", style="bold")
    tbl.add_column("log loss", justify="right")
    tbl.add_column("Brier", justify="right")
    tbl.add_column("ECE", justify="right")
    tbl.add_column("top-1", justify="right")
    tbl.add_column("top-10", justify="right")
    tbl.add_column("top-30", justify="right")
    tbl.add_column("top-100", justify="right")
    for name, m in metrics.items():
        tbl.add_row(
            name,
            f"{m.mean_log_loss:.3f}",
            f"{m.mean_brier:.4f}",
            f"{m.ece:.4f}",
            f"{m.top1_acc*100:.1f}%",
            f"{m.top10_acc*100:.1f}%",
            f"{m.top30_acc*100:.1f}%",
            f"{m.top100_acc*100:.1f}%",
        )
    console.print(tbl)

    # Synthetic ROI table (Plan A/B/C 仮想エントリ累積)
    tbl2 = Table(title="Synthetic Plan ROI (¥100/pt 仮想エントリ)", show_lines=False)
    tbl2.add_column("Model", style="bold")
    tbl2.add_column("Plan A ROI", justify="right")
    tbl2.add_column("(hits/stake/payout)")
    tbl2.add_column("Plan B ROI", justify="right")
    tbl2.add_column("(hits/stake/payout)")
    tbl2.add_column("Plan C ROI", justify="right")
    tbl2.add_column("(hits/stake/payout)")
    for name, m in metrics.items():
        def _fmt_roi(roi: float) -> str:
            if roi >= 1.0:
                return f"[bold green]{roi:.3f}[/]"
            elif roi >= 0.85:
                return f"[green]{roi:.3f}[/]"
            else:
                return f"[red]{roi:.3f}[/]"
        tbl2.add_row(
            name,
            _fmt_roi(m.plan_a_roi),
            f"{m.plan_a_hits}/{m.plan_a_stake:,}/{m.plan_a_payout:,}",
            _fmt_roi(m.plan_b_roi),
            f"{m.plan_b_hits}/{m.plan_b_stake:,}/{m.plan_b_payout:,}",
            _fmt_roi(m.plan_c_roi),
            f"{m.plan_c_hits}/{m.plan_c_stake:,}/{m.plan_c_payout:,}",
        )
    console.print(tbl2)


def print_reliability(metrics: dict[str, AggregateMetrics]) -> None:
    """Quantile-binned reliability diagram (ASCII)。"""
    for name, m in metrics.items():
        if not m.reliability:
            continue
        console.rule(f"[bold]Reliability — {name}[/bold]")
        tbl = Table(show_lines=False)
        tbl.add_column("bin", justify="right", style="dim")
        tbl.add_column("rows", justify="right")
        tbl.add_column("mean pred P", justify="right")
        tbl.add_column("actual freq", justify="right")
        tbl.add_column("diff")
        for i, (pred, actual, cnt) in enumerate(m.reliability, 1):
            diff = actual - pred
            sign = "+" if diff >= 0 else ""
            if abs(diff) < 1e-4:
                style = "[green]"
            elif diff > 0:
                style = "[cyan]"  # actual > pred → モデル過小予測
            else:
                style = "[red]"   # actual < pred → モデル過大予測
            tbl.add_row(
                str(i),
                f"{cnt:,}",
                f"{pred:.4f}",
                f"{actual:.4f}",
                f"{style}{sign}{diff:.4f}[/]",
            )
        console.print(tbl)


# --- CLI ---


@app.command()
def main(
    since: str | None = typer.Option(None, "--since", help="race_id 下界 (YYYY... 前方一致比較)"),
    until: str | None = typer.Option(None, "--until", help="race_id 上界 (YYYY... 前方一致比較)"),
    baseline: bool = typer.Option(True, "--baseline/--no-baseline", help="市場 baseline も計算"),
    reliability: bool = typer.Option(False, "--reliability", help="Reliability diagram を表示"),
    rerun: bool = typer.Option(False, "--rerun", help="raw HTML から再構築して現 estimate_probs を回す (LightGBM 学習済モデルを試したい時)"),
    market_blend: float = typer.Option(0.4, "--market-blend", help="rerun 時のモデル/市場ブレンド比"),
):
    """data/predictions/ + data/results/ を join してバックテスト指標を出す。"""
    cases = list(load_cases_from_snapshots(since=since, until=until))
    if not cases:
        console.print(
            "[yellow]対象 race なし。"
            "data/predictions/ と data/results/ の race_id が一致するエントリが要ります。[/yellow]"
        )
        raise typer.Exit(1)

    models: dict[str, ModelFn] = {"snapshot": current_model_passthrough}
    if baseline:
        models["market"] = market_baseline
    if rerun:
        models["rerun_new"] = make_rerun_model(market_blend=market_blend)
        # 純モデルも別 entry で
        models["rerun_pure"] = make_rerun_model(market_blend=0.0)

    metrics = run_models(cases, models)
    print_summary(metrics)
    if reliability:
        print_reliability(metrics)

    if cases and len(cases) < 30:
        console.print(
            f"\n[yellow]⚠ サンプル {len(cases)} < 30。"
            "モデル変種間の差は誤差で消える可能性大。"
            "race を蓄積してから判断してください。[/yellow]"
        )


if __name__ == "__main__":
    app()
