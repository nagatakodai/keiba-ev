"""レース結果を data/results/<race-id>.json に記録する CLI (競馬版)。

使い方:
    python -m src.record 2026052102-3-2 5,2,7
    python -m src.record 2026052102-3-2 5,2,7 --payout 25400
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "data" / "results"
PREDICTIONS_DIR = ROOT / "data" / "predictions"

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def main(
    race_id: str = typer.Argument(..., help="レース ID (例: 2026052102-3-2)"),
    order: str = typer.Argument(..., help="1-2-3 着の馬番をカンマ区切り (例: 5,2,7)"),
    payout: int = typer.Option(0, "--payout", help="3 連単払戻金額"),
    note: str = typer.Option("", "--note", help="自由記述 (取消・除外等)"),
):
    """レース結果を保存し、prediction との突き合わせを表示。"""
    parts = [p.strip() for p in order.split(",")]
    if len(parts) != 3:
        console.print(f"[red]エラー: 着順は 3 つ必要 (例: 5,2,7)。受け取った: {order}[/red]")
        raise typer.Exit(2)
    try:
        finish_order = [int(p) for p in parts]
    except ValueError:
        console.print(f"[red]エラー: 馬番は整数のみ。受け取った: {order}[/red]")
        raise typer.Exit(2)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{race_id}.json"
    data = {
        "race_id": race_id,
        "finish_order": finish_order,
        "trifecta_payout": int(payout),
        "note": note,
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]saved:[/green] {out_path}")

    pred_path = PREDICTIONS_DIR / f"{race_id}.json"
    if not pred_path.exists():
        console.print(
            f"[yellow]⚠ prediction snapshot 未保存 ({pred_path})。"
            "キャリブレーション集計の対象外。[/yellow]"
        )
        return

    pred = json.loads(pred_path.read_text(encoding="utf-8"))
    finish_tuple = tuple(finish_order)
    winning_row = next(
        (r for r in pred.get("rows", []) if tuple(r["key"]) == finish_tuple),
        None,
    )
    if winning_row:
        console.print(
            f"[cyan]✓ 的中目の予測:[/cyan] "
            f"P={winning_row['prob']*100:.2f}% "
            f"オッズ={winning_row['odds']:.1f} "
            f"P×O={winning_row['px_o']:.2f} "
            f"tier={winning_row['tier']} "
            f"人気={winning_row['popularity']}"
        )
    else:
        console.print(
            f"[yellow]✗ 的中目 {finish_order} は prediction rows に含まれていません[/yellow]"
        )

    for plan_name, key_list in (
        ("Plan A", pred.get("plan_a_keys", [])),
        ("Plan B", pred.get("plan_b_keys", [])),
        ("Plan C", pred.get("plan_c_keys", [])),
    ):
        keys_set = {tuple(k) for k in key_list}
        hit = finish_tuple in keys_set
        mark = "[green]✓ HIT[/green]" if hit else "[dim]miss[/dim]"
        size = len(key_list)
        console.print(f"  {plan_name} ({size}点): {mark}")


if __name__ == "__main__":
    app()
