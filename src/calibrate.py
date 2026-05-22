"""data/predictions/ と data/results/ を突き合わせてキャリブレーションを集計する CLI。

集計内容:
  1. tier 別 (本線 / 中穴 / 大穴 / −EV) の calibration ratio
     = 実 hit 数 / 予測 P 合計 (ideally 1.0)
  2. Plan A/B/C の hit 率 + 累計払戻 + ROI 概算
  3. (オプション) レース単位の hit/miss 一覧

注: オフセットの自動適用は未実装。手動判断の参考データとして使う。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
PRED_DIR = ROOT / "data" / "predictions"
RESULT_DIR = ROOT / "data" / "results"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=False)


@app.command()
def main(
    per_race: bool = typer.Option(False, "--per-race", help="レース毎の hit/miss 一覧を出力"),
    point_cost: int = typer.Option(100, "--point-cost", help="1 点あたりの賭け金 (円)"),
):
    """キャリブレーションレポートを出力。"""
    pairs = list(_iter_pairs())
    if not pairs:
        console.print(
            "[yellow]predictions ↔ results の対応データなし。"
            "make record で結果を登録するか、make run で predictions を生成してください。[/yellow]"
        )
        raise typer.Exit(1)

    console.rule(f"[bold cyan]キャリブレーション (n={len(pairs)} races)[/bold cyan]")

    tier_stats: dict[str, dict] = defaultdict(
        lambda: {"prob_sum": 0.0, "hits": 0, "rows": 0}
    )
    plan_stats: dict[str, dict] = defaultdict(
        lambda: {"hits": 0, "payouts": 0, "races": 0, "total_points": 0}
    )
    per_race_records: list[dict] = []

    for race_id, pred, result in pairs:
        finish_tuple = tuple(result["finish_order"])
        payout = int(result.get("trifecta_payout") or 0)

        # tier 別集計: 全予測行で「予測 P 合計」と「実 hit (1 件)」をカウント
        winning_tier: str | None = None
        for r in pred.get("rows", []):
            tier = r.get("tier", "?")
            tier_stats[tier]["prob_sum"] += r["prob"]
            tier_stats[tier]["rows"] += 1
            if tuple(r["key"]) == finish_tuple:
                tier_stats[tier]["hits"] += 1
                winning_tier = tier

        # Plan 別集計 (G/H1/H2/F も含む。古いスナップショットにキーが無い場合はスキップ)
        race_plan_results = {}
        plan_specs = (
            ("Plan A", pred.get("plan_a_keys", [])),
            ("Plan B", pred.get("plan_b_keys", [])),
            ("Plan C", pred.get("plan_c_keys", [])),
            ("Plan G", pred.get("plan_g_keys")),
            ("Plan H1", pred.get("plan_h1_keys")),
            ("Plan H2", pred.get("plan_h2_keys")),
            ("Plan F", pred.get("plan_f_keys")),
        )
        for plan_name, key_list in plan_specs:
            if key_list is None:
                # 旧スナップショットにキー不在 → そのレースはこの plan の集計対象外
                continue
            plan_stats[plan_name]["races"] += 1
            plan_stats[plan_name]["total_points"] += len(key_list)
            keys_set = {tuple(k) for k in key_list}
            hit = finish_tuple in keys_set
            race_plan_results[plan_name] = hit
            if hit:
                plan_stats[plan_name]["hits"] += 1
                if payout > 0:
                    plan_stats[plan_name]["payouts"] += payout

        per_race_records.append({
            "race_id": race_id,
            "venue": pred.get("venue_name", ""),
            "finish": finish_tuple,
            "winning_tier": winning_tier,
            "payout": payout,
            "plan_a_hit": race_plan_results.get("Plan A", False),
            "plan_b_hit": race_plan_results.get("Plan B", False),
            "plan_c_hit": race_plan_results.get("Plan C", False),
            "plan_g_hit": race_plan_results.get("Plan G", False),
            "plan_h1_hit": race_plan_results.get("Plan H1", False),
            "plan_h2_hit": race_plan_results.get("Plan H2", False),
            "plan_f_hit": race_plan_results.get("Plan F", False),
        })

    _print_tier_table(tier_stats)
    _print_plan_table(plan_stats, point_cost)

    if per_race:
        _print_per_race(per_race_records)

    _print_summary_notes(len(pairs))


def _print_tier_table(tier_stats: dict[str, dict]) -> None:
    tbl = Table(title="Tier 別キャリブレーション (ratio = 実hit / 予測P合計)", show_lines=False)
    tbl.add_column("Tier", style="bold")
    tbl.add_column("予測 rows", justify="right")
    tbl.add_column("予測 P 合計", justify="right")
    tbl.add_column("実 hit 数", justify="right")
    tbl.add_column("ratio", justify="right")
    tbl.add_column("解釈")
    for tier in ("honsen", "chuana", "oana", "minus"):
        if tier not in tier_stats:
            continue
        s = tier_stats[tier]
        prob_sum = s["prob_sum"]
        hits = s["hits"]
        ratio = hits / prob_sum if prob_sum > 0 else 0.0
        if hits < 3:
            interp = "[dim]サンプル不足[/dim]"
        elif ratio < 0.7:
            interp = "[red]過大予測 (削減候補)[/red]"
        elif ratio < 0.85:
            interp = "[yellow]やや過大[/yellow]"
        elif ratio < 1.15:
            interp = "[green]ほぼ整合[/green]"
        elif ratio < 1.3:
            interp = "[cyan]やや過小[/cyan]"
        else:
            interp = "[bold cyan]過小 (機会)[/bold cyan]"
        tbl.add_row(
            _tier_label(tier),
            str(s["rows"]),
            f"{prob_sum:.3f}",
            str(hits),
            f"{ratio:.2f}×" if prob_sum > 0 else "-",
            interp,
        )
    console.print(tbl)


def _print_plan_table(plan_stats: dict[str, dict], point_cost: int) -> None:
    tbl = Table(title=f"Plan 別 ROI (1点 ¥{point_cost})", show_lines=False)
    tbl.add_column("Plan", style="bold")
    tbl.add_column("races", justify="right")
    tbl.add_column("hits", justify="right")
    tbl.add_column("hit 率", justify="right")
    tbl.add_column("総点数", justify="right")
    tbl.add_column("累計賭金", justify="right")
    tbl.add_column("累計払戻", justify="right")
    tbl.add_column("ROI")
    for name in ("Plan A", "Plan B", "Plan C", "Plan G", "Plan H1", "Plan H2", "Plan F"):
        s = plan_stats[name]
        if s["races"] == 0:
            continue
        rate = s["hits"] / s["races"] if s["races"] else 0.0
        stake = s["total_points"] * point_cost
        payout = s["payouts"]
        roi = (payout / stake) if stake > 0 else 0.0
        if roi >= 1.0:
            roi_style = "[bold green]"
        elif roi >= 0.85:
            roi_style = "[green]"
        else:
            roi_style = "[red]"
        tbl.add_row(
            name,
            str(s["races"]),
            str(s["hits"]),
            f"{rate*100:.1f}%",
            str(s["total_points"]),
            f"¥{stake:,}",
            f"¥{payout:,}",
            f"{roi_style}{roi:.2f}×[/]",
        )
    console.print(tbl)


def _print_per_race(records: list[dict]) -> None:
    tbl = Table(title="レース毎の結果", show_lines=False)
    tbl.add_column("race_id", style="dim")
    tbl.add_column("会場")
    tbl.add_column("着順", style="bold")
    tbl.add_column("tier")
    tbl.add_column("払戻", justify="right")
    tbl.add_column("A")
    tbl.add_column("B")
    tbl.add_column("C")
    tbl.add_column("G")
    tbl.add_column("H1")
    tbl.add_column("H2")
    tbl.add_column("F")
    for r in records:
        f = r["finish"]
        tbl.add_row(
            r["race_id"],
            r.get("venue") or "-",
            f"{f[0]}-{f[1]}-{f[2]}",
            _tier_label(r.get("winning_tier") or "?"),
            f"¥{r['payout']:,}" if r["payout"] else "-",
            "[green]✓[/green]" if r["plan_a_hit"] else "[dim]·[/dim]",
            "[green]✓[/green]" if r["plan_b_hit"] else "[dim]·[/dim]",
            "[green]✓[/green]" if r["plan_c_hit"] else "[dim]·[/dim]",
            "[green]✓[/green]" if r["plan_g_hit"] else "[dim]·[/dim]",
            "[green]✓[/green]" if r["plan_h1_hit"] else "[dim]·[/dim]",
            "[green]✓[/green]" if r["plan_h2_hit"] else "[dim]·[/dim]",
            "[green]✓[/green]" if r["plan_f_hit"] else "[dim]·[/dim]",
        )
    console.print(tbl)


def _print_summary_notes(n: int) -> None:
    if n < 30:
        console.print(
            f"[yellow]⚠ サンプル数 {n} < 30。"
            "係数調整の判断材料には不足。CLAUDE.md 「保守化の禁則」参照。[/yellow]"
        )
    console.print(
        "[dim]ratio の解釈:[/dim]\n"
        "[dim]  - 0.7 未満: 確率モデルがその tier で予測しすぎ → 係数を下げる検討[/dim]\n"
        "[dim]  - 1.3 以上: 予測しなさすぎ → その tier に +EV が滞留している可能性[/dim]\n"
        "[dim]  - 1.0 付近: モデルが現実と整合[/dim]"
    )


def _iter_pairs() -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """predictions と results を race_id で突き合わせて yield。"""
    if not PRED_DIR.exists():
        return
    for pred_path in sorted(PRED_DIR.glob("*.json")):
        race_id = pred_path.stem
        result_path = RESULT_DIR / f"{race_id}.json"
        if not result_path.exists():
            continue
        try:
            pred = json.loads(pred_path.read_text(encoding="utf-8"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        yield race_id, pred, result


def _tier_label(tier: str) -> str:
    return {"honsen": "本線", "chuana": "中穴", "oana": "大穴", "minus": "−EV"}.get(tier, tier)


if __name__ == "__main__":
    app()
