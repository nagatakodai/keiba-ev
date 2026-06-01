"""CLI: URL or キャッシュ済み state を渡して EV 表を出力 (競馬版)。"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time
from pathlib import Path
from typing import Optional


def _read_html_file(path: Path) -> str:
    """`*.html` も `*.html.gz` も透過に読む helper。

    data/raw/ のキャッシュは gz 圧縮なので、ユーザーがそのまま `--html` で
    渡せるように拡張子で分岐する。
    """
    if path.suffix == ".gz" or path.name.endswith(".html.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    return path.read_text(encoding="utf-8")

import typer
from rich.console import Console
from rich.table import Table

from . import ev as ev_mod
from . import llm as llm_mod
from . import portfolio as pf_mod
from .aptitude import AptitudeIndex, compute_aptitudes
from .ev import PXO_FLOOR
from .features import build_features
from .market_signal import MarketSignal, compute_market_signals
from .parse import fetch_and_parse, parse_shutuba, parse_trifecta
from .scrape import (
    NetkeibaBlocked,
    cache_html,
    extract_race_id,
    fetch_html,
    odds_trifecta_url,
    shutuba_url,
)

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def main(
    url: Optional[str] = typer.Argument(None, help="netkeiba 出馬表 / オッズ URL (race_id を含むもの)"),
    html_file: Optional[Path] = typer.Option(None, "--html", help="保存済み出馬表 HTML"),
    odds_html_file: Optional[Path] = typer.Option(None, "--odds-html", help="保存済み 3 連単オッズ HTML"),
    probs_file: Optional[Path] = typer.Option(None, "--probs", help="確率推定 YAML"),
    show: int = typer.Option(30, "--show", help="P×O 上位 N 件を表示"),
    show_prob: int = typer.Option(20, "--show-prob", help="推定当選率 上位 N 件を表示"),
    prob_floor: float = typer.Option(0.0, "--prob-floor", help="推定当選率ランキングに含める最低 P×O"),
    show_low: int = typer.Option(20, "--show-low", help="人気 51-150 位帯から +EV を表示"),
    no_cache: bool = typer.Option(False, "--no-cache", help="state をファイル保存しない"),
    no_llm: bool = typer.Option(False, "--no-llm", help="claude CLI による評価をスキップ"),
    llm_model: str = typer.Option("opus", "--llm-model", help="claude のモデル"),
    refresh: bool = typer.Option(False, "--refresh", "-R", help="締切 N 分前まで待機して再取得・再評価"),
    refresh_min: int = typer.Option(5, "--refresh-min", help="締切何分前に refresh するか"),
    ev_max: Optional[float] = typer.Option(None, "--ev-max", help="Plan に組む最大 P×O"),
    min_prob: Optional[float] = typer.Option(None, "--min-prob", help="Plan に組む最低当選率 (%)"),
    market_blend: float = typer.Option(0.78, "--market-blend", help="市場暗黙1着率とモデルの混合比 (holdout 291 races peak)"),
    market_floor: float = typer.Option(0.01, "--market-floor", help="市場確率のフロア"),
    hit_points: int = typer.Option(3, "--hit-points", help="Plan H の点数"),
    hit_budget_ratio: float = typer.Option(0.2, "--hit-budget-ratio", help="当て枠の予算比率"),
    aptitude_top: int = typer.Option(6, "--aptitude-top", help="Plan G の適性 top N (頭数)"),
    with_exacta: bool = typer.Option(False, "--with-exacta", help="馬単 (b5) も fetch (jiku iteration で重い)"),
    with_trio: bool = typer.Option(False, "--with-trio", help="3 連複 (b6) も fetch (jiku iteration で重い)"),
    phase: str = typer.Option("bet", "--phase", help="score = Claude 考察で各馬指数を出しキャッシュ / bet = 指数+市場でP→束→snapshot (既定)"),
    llm_blend: float = typer.Option(ev_mod.LLM_BLEND_DEFAULT, "--llm-blend", help="Claude 指数と model fundamental の合成重み (0=モデルのみ, 1=指数のみ)"),
):
    """URL (netkeiba) を渡して P×O ランキングと Plan A/B/C を出力。"""
    if not (url or html_file):
        console.print("[red]URL か --html のいずれかが必要です[/red]")
        raise typer.Exit(2)

    if html_file:
        # CLAUDE.md オフライン解析の用途で `data/raw/<rid>-shutuba.html.gz` を
        # そのまま渡せるよう gz 拡張子を自動展開。
        rd = parse_shutuba(_read_html_file(html_file))
        if odds_html_file:
            rd.trifecta = parse_trifecta(_read_html_file(odds_html_file))
        else:
            console.print("[yellow]--html だけ指定 (オッズなし)。--odds-html も渡してください[/yellow]")
    else:
        console.print(f"[dim]fetching {url}...[/dim]")
        try:
            rd = fetch_and_parse(
                url,
                with_exacta=with_exacta,
                with_trio=with_trio,
            )  # type: ignore[arg-type]
        except NetkeibaBlocked as ex:
            console.print(
                f"[bold red]netkeiba から空 HTML が返りました (CloudFront 400)。"
                f"IP 規制中の可能性。analyze 不能。[/bold red]\n"
                f"[dim]{ex}[/dim]"
            )
            raise typer.Exit(2)

    race_id = f"{rd.race.cup_id}-{rd.race.schedule_index}-{rd.race.race_number}"

    if url and not no_cache:
        rid = extract_race_id(url) or race_id
        # 出馬表 HTML だけ簡易キャッシュ (オッズはサイズが大きいので保存しない)
        try:
            sh_html = fetch_html(shutuba_url(rid), settle_ms=1000)
            out = cache_html(sh_html, rid, ROOT, suffix="-shutuba")
            console.print(f"[dim]cached: {out}[/dim]")
        except Exception:
            pass

    feats = build_features(rd)
    aptitudes = compute_aptitudes(rd, feats=feats)
    apt_top = _aptitude_top_horses(aptitudes, n=aptitude_top)
    market_signals = compute_market_signals(rd)

    # 2段パイプライン score ステージ: Claude に各馬の強さ指数を出させてキャッシュし即終了。
    # (estimate_probs / 束 / snapshot / enqueue はしない。bet ステージが指数を読む)
    if phase == "score":
        _print_race_header(rd)
        best_times_for_score = _serialize_best_times(rd, feats) if feats else []
        _run_score_stage(
            race_id, rd, aptitudes=aptitudes, market_signals=market_signals,
            horse_best_times=best_times_for_score, model=llm_model,
        )
        return

    # bet ステージ: score ステージのキャッシュ指数を読んで estimate_probs に合成する。
    llm_index, llm_scored_at = _load_llm_scores(race_id)

    lgbm_info = ev_mod.lgbm_status()
    if lgbm_info.get("available"):
        n_feat = lgbm_info["n_features"]
        # FeatureVec の新フィールドが model 学習時に無かった場合は warning
        from dataclasses import fields as _fields
        fv_field_names = {f.name for f in _fields(__import__("src.features", fromlist=["FeatureVec"]).FeatureVec)}
        missing = fv_field_names - set(lgbm_info["feature_cols"]) - {"number", "absent", "win_odds", "style_score", "pace_fit", "same_going_count", "same_going_show_rate", "going_versatility", "best_time_at_target", "best_time_runs"}
        console.print(
            f"[dim]✓ LightGBM 学習済モデル使用 ({n_feat} features, "
            f"trained {lgbm_info.get('trained_at', '?')})[/dim]"
        )
    else:
        err = lgbm_info.get("load_error", "model files missing")
        console.print(f"[yellow]⚠ LightGBM 不可 (linear softmax fallback): {err}[/yellow]")
    # Phase 18 以降、全 Plan は単一の β=BLEND_DEFAULT=0.78 で動作。
    # かつて Plan H1/H2 を β=0、Plan G を β=1.0 で動かしていた path は
    # CV / sliding-window で overfit と判明し Phase 22/23 で revert (詳細
    # CLAUDE.md)。BLEND_HIT_PURE / BLEND_APTITUDE_GATE 定数は実験用に ev.py
    # に残置、CLI の --market-blend で 1 回限り試せる。
    probs = ev_mod.estimate_probs(
        rd, market_blend=market_blend, market_floor=market_floor,
        llm_win_index=llm_index, llm_blend=llm_blend,
    )
    probs = ev_mod.load_probs(str(probs_file) if probs_file else None, probs)

    _print_race_header(rd)
    _print_horse_table(rd)
    _print_aptitudes(rd, aptitudes)
    _print_market_signals(rd, market_signals)
    _print_weather(rd)
    _print_predictions(rd)
    _print_interviews(rd)

    rows = ev_mod.build_table(rd, probs)
    bet_tables = ev_mod.build_all_bet_tables(rd, probs)

    min_prob_dec = min_prob / 100.0 if min_prob is not None else None
    plan_rows = ev_mod.apply_caps(rows, ev_max=ev_max, min_prob=min_prob_dec)

    if not no_cache:
        _save_prediction_snapshot(
            race_id, rd, rows, plan_rows, aptitudes, bet_tables, apt_top, market_signals,
            feats=feats, lgbm_info=lgbm_info, hit_points=hit_points, probs=probs,
            llm_win_index=llm_index, llm_blend=llm_blend, llm_scored_at=llm_scored_at,
        )
    if ev_max is not None or min_prob is not None:
        kept = len(plan_rows)
        total = len(rows)
        cap_desc = []
        if ev_max is not None:
            cap_desc.append(f"P×O ≤ {ev_max:.2f}")
        if min_prob is not None:
            cap_desc.append(f"当選率 ≥ {min_prob:.2f}%")
        console.print(
            f"[dim]Plan キャップ適用: {', '.join(cap_desc)} → {kept}/{total} 件残存[/dim]"
        )

    _print_top(rows, n=show)
    _print_top_by_prob(rows, n=show_prob, pxo_floor=prob_floor)
    _print_low_band(rows, low=51, high=150, limit=show_low)
    _print_plans(
        plan_rows,
        hit_points=hit_points,
        hit_budget_ratio=hit_budget_ratio,
        aptitude_top_horses=apt_top,
    )
    _print_bet_tables(bet_tables, aptitude_top_horses=apt_top)
    _print_judgment_notes(rd, rows)

    initial_eval = ""   # refresh の context 用に空のまま渡す
    # 2段パイプライン (指数ステップ一本化): bet ステージは Claude の picks/cuts 選定を呼ばない。
    # 買い目は score ステージの指数を合成した probs から build_bundle (joint Kelly + トリガミ防止)
    # が決める。Claude の考察は estimate_probs に既に入っている (llm_win_index)。
    if llm_index is not None:
        console.print(f"[cyan]Claude 指数を合成済 (llm_blend={llm_blend}, {len(llm_index)} 頭)[/cyan]")
    elif not no_llm:
        console.print("[yellow]Claude 指数キャッシュ無し — モデルのみで束生成 (score ステージ未完?)[/yellow]")

    if refresh:
        if not url:
            console.print("[yellow]--refresh は URL 指定時のみ有効。スキップ。[/yellow]")
        else:
            _refresh_and_reevaluate(
                url=url,
                rd_old=rd,
                rows_old=rows,
                initial_eval=initial_eval,
                minutes_before=refresh_min,
                model=llm_model,
                no_llm=no_llm,
                no_cache=no_cache,
                show=show,
                show_prob=show_prob,
                prob_floor=prob_floor,
                show_low=show_low,
                ev_max=ev_max,
                min_prob=min_prob,
                market_blend=market_blend,
                market_floor=market_floor,
                hit_points=hit_points,
                hit_budget_ratio=hit_budget_ratio,
                aptitude_top=aptitude_top,
                with_exacta=with_exacta,
                with_trio=with_trio,
            )


def _print_evidence_adjusted(
    plan_rows,
    evidence: dict,
    hit_points: int = 3,
    hit_budget_ratio: float = 0.2,
    aptitude_top_horses: list[int] | None = None,
) -> None:
    evidence_by_key = evidence.get("evidence_by_key") or {}
    cuts = evidence.get("cuts") or []
    if not evidence_by_key and not cuts:
        return
    adjusted = ev_mod.apply_evidence(plan_rows, evidence_by_key, cuts)
    console.rule("[bold magenta]検索補強適用後の Plan[/bold magenta]")
    console.print(
        f"[dim]補強根拠で {len(evidence_by_key)} 件評価、cuts {len(cuts)} 件除外 → "
        f"{len(adjusted)}/{len(plan_rows)} 件残存[/dim]"
    )
    if cuts:
        console.print(f"[red]cuts:[/red] {', '.join(cuts)}")
    if adjusted:
        tbl = Table(title="補強反映後の P×O 上位 10", show_lines=False)
        tbl.add_column("#", justify="right", style="dim")
        tbl.add_column("買い目", style="bold")
        tbl.add_column("補強")
        tbl.add_column("オッズ", justify="right")
        tbl.add_column("補正P", justify="right")
        tbl.add_column("補正P×O", justify="right")
        tbl.add_column("帯")
        for i, r in enumerate(adjusted[:10], 1):
            key_str = f"{r.key[0]}-{r.key[1]}-{r.key[2]}"
            info = evidence_by_key.get(key_str, {})
            count = info.get("count", 0)
            badge = f"{count}件" if count else "-"
            tbl.add_row(
                str(i),
                key_str,
                badge,
                f"{r.odds:.1f}",
                f"{r.prob * 100:.2f}%",
                _color_pxo(r.px_o),
                _tier_jp(r.tier),
            )
        console.print(tbl)
    _print_plans(
        adjusted,
        hit_points=hit_points,
        hit_budget_ratio=hit_budget_ratio,
        aptitude_top_horses=aptitude_top_horses,
    )


def _save_evidence_to_snapshot(
    race_id: str,
    plan_rows,
    evidence: dict,
    aptitude_top_horses: list[int] | None = None,
    hit_points: int = 3,
) -> None:
    snap_path = ROOT / "data" / "predictions" / f"{race_id}.json"
    if not snap_path.exists():
        return
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    # 新スキーマ (2026-05-29 後半): Plan A/B 自体は廃止。evidence は LLM 補強後の 3連単 rows と
    # cuts のみを保存する。bundle 再生成は `_validate_and_update_bundle` で別途処理。
    _ = aptitude_top_horses
    _ = hit_points
    evidence_by_key = evidence.get("evidence_by_key") or {}
    cuts = evidence.get("cuts") or []
    adjusted = ev_mod.apply_evidence(plan_rows, evidence_by_key, cuts)
    snap["evidence"] = evidence
    snap["evidence_rows"] = [
        {
            "key": list(r.key),
            "odds": r.odds,
            "popularity": r.popularity,
            "prob": r.prob,
            "px_o": r.px_o,
            "tier": r.tier,
        }
        for r in adjusted
    ]
    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")


def _serialize_bet_tables(bet_tables: dict[str, list]) -> dict[str, list[dict]]:
    """bet_type → 上位 30 行を JSON 化。長すぎる full table は CLI/Snapshot を圧迫するので絞る。"""
    out: dict[str, list[dict]] = {}
    for bt, rows in bet_tables.items():
        out[bt] = [
            {
                "key": list(r.key),
                "odds": r.odds,
                "popularity": r.popularity,
                "prob": r.prob,
                "px_o": r.px_o,
                "tier": r.tier,
            }
            for r in rows[:30]
        ]
    return out


def _serialize_bet_tables_g(
    bet_tables: dict[str, list],
    aptitude_top_horses: list[int],
) -> dict[str, list[dict]]:
    """各 bet type の「適性ゲート→EV足切り」picks を JSON 化。"""
    out: dict[str, list[dict]] = {}
    for bt, rows in bet_tables.items():
        picks = ev_mod.plan_aptitude_ev_bet(rows, aptitude_top_horses)
        if not picks:
            continue
        out[bt] = [
            {
                "key": list(r.key),
                "odds": r.odds,
                "popularity": r.popularity,
                "prob": r.prob,
                "px_o": r.px_o,
                "tier": r.tier,
            }
            for r in picks
        ]
    return out


def _serialize_best_times(rd, feats: dict) -> list[dict]:
    """各馬の 持ち時計 (venue × distance ± 100m × surface での best own_time_sec)。

    best_time_at_target が 0 (未経験) の馬は除外。秒数 + 元になった経験数を出力。
    """
    name_by_n = {h.number: h.name for h in rd.race.horses}
    items = []
    for n, fv in feats.items():
        if fv.best_time_at_target <= 0:
            continue
        items.append({
            "number": n,
            "name": name_by_n.get(n, ""),
            "best_time_sec": round(fv.best_time_at_target, 2),
            "runs": fv.best_time_runs,
        })
    # 速い順 (秒数が小さい順)
    items.sort(key=lambda x: x["best_time_sec"])
    return items


def _serialize_market_signals(rd, signals: dict[int, MarketSignal]) -> list[dict]:
    """snapshot JSON 用に MarketSignal を直列化。"""
    name_by_n = {h.number: h.name for h in rd.race.horses}
    items = []
    for n in sorted(signals):
        s = signals[n]
        items.append({
            "number": n,
            "name": name_by_n.get(n, ""),
            "win_odds": round(s.win_odds, 1),
            "place_odds_min": round(s.place_odds_min, 1),
            "win_implied": round(s.win_implied, 4),
            "place_implied": round(s.place_implied, 4),
            "place_to_win_ratio": round(s.place_to_win_ratio, 2),
            "interpretation": s.interpretation,
        })
    return items


def _serialize_aptitudes(rd, aptitudes: dict[int, AptitudeIndex]) -> list[dict]:
    """snapshot JSON 用に AptitudeIndex を直列化。総合降順で配列化。"""
    name_by_n = {h.number: h.name for h in rd.race.horses}
    items = []
    for n, ai in sorted(aptitudes.items(), key=lambda kv: kv[1].total, reverse=True):
        items.append({
            "number": n,
            "name": name_by_n.get(n, ""),
            "total": round(ai.total, 1),
            "ability": round(ai.ability, 1),
            "distance_fit": round(ai.distance_fit, 1),
            "last3f": round(ai.last3f, 1),
            "surface_fit": round(ai.surface_fit, 1),
            "going_fit": round(ai.going_fit, 1),
            "condition": round(ai.condition, 1),
            "jockey_fit": round(ai.jockey_fit, 1),
            "pace_fit": round(ai.pace_fit, 1),
            "graded_record": round(ai.graded_record, 1),
            "graded_text": ai.graded_text,
            "reasons": ai.reasons,
        })
    return items


def _validate_and_update_bundle(
    race_id: str,
    rd,
    probs,
    rows,
    bet_tables,
    *,
    aptitudes=None,
    market_signals=None,
    horse_best_times=None,
    model: str = "opus",
    prioritize: str = "yield",
) -> None:
    """claude -p (web 検索付き) にモデル出力を全部見せて最終「買い目」を選定させ、
    その picks で recommended_bundle (回収優先) または recommended_bundle_hit (的中優先) を
    再構築して snapshot 更新。

    `prioritize="yield"` (default): 回収優先 — joint Kelly EV 最適束、`recommended_bundle` を更新。
    `prioritize="hit"`             : 的中優先 — prob 降順 pool で Kelly、`recommended_bundle_hit` を更新。

    検証不能/見送り時は no-op (モデル束を維持)。
    """
    cands = pf_mod.candidates_from_ev_rows(rows, bet_tables)
    bundle = pf_mod.build_bundle(cands, probs, prioritize=prioritize)
    if not bundle.get("legs"):
        return  # 見送り (+EV 束なし) — 選定対象なし
    if not llm_mod.is_available():
        return

    mode_jp = "的中優先" if prioritize == "hit" else "回収優先"
    console.rule(f"[bold]Claude 総合オススメ 選定 ({mode_jp}, claude -p, web 検索で per-leg 補強)[/bold]")
    chunks: list[str] = []
    saw_error = False
    saw_result = False
    tool_count = 0
    try:
        for etype, payload in llm_mod.select_bundle_stream(
            rd, bundle, cands, model=model,
            aptitudes=aptitudes, market_signals=market_signals,
            horse_best_times=horse_best_times,
            prioritize=prioritize,
        ):
            if etype == "text":
                chunks.append(payload)
                console.print(payload, end="")
            elif etype == "tool_use":
                # 検索 (Brave/Tavily/WebFetch) の query をリアルタイム表示。silent run 防止。
                tool_count += 1
                name = (payload or {}).get("name", "?")
                inp = (payload or {}).get("input") or {}
                q = inp.get("query") or inp.get("q") or inp.get("url") or ""
                label = name.replace("mcp__", "").replace("__", "/")
                if q:
                    qshort = q if len(q) <= 70 else q[:67] + "..."
                    console.print(f"[dim]  🔍 {label}: {qshort}[/dim]")
                else:
                    console.print(f"[dim]  ⚙ {label}[/dim]")
            elif etype == "result":
                saw_result = True
                if payload:
                    chunks.append(payload)
            elif etype == "error":
                saw_error = True
                console.print(f"[red]bundle 選定エラー: {payload}[/red]")
    except Exception as ex:  # noqa: BLE001
        console.print(f"[yellow]bundle 選定失敗: {ex}[/yellow]")
        return

    if tool_count:
        console.print(f"[dim](補強のための検索/ツール呼び出し計 {tool_count} 回)[/dim]")
    if saw_error or not saw_result:
        console.print("[yellow]選定が完了しなかったため未選定のまま (モデル束を維持)[/yellow]")
        return

    review = llm_mod.parse_bundle_review("".join(chunks))
    bundle, applied = _decide_selection_bundle(review, cands, probs, bundle, prioritize=prioritize)
    if not applied:
        # picks/cuts を適用できなかった (picks が候補 id に一致しない / 旧形式で picks も
        # cuts も無い)。モデル束を維持し validated バッジは付けない (未検証扱い)。
        console.print("[yellow]claude 選定を適用できず — モデル束を維持 (未検証)[/yellow]")
        return
    n = len(bundle.get("legs", []))
    console.print(f"[cyan]claude 選定適用 → 束 {n} 脚" + (" (見送り)" if n == 0 else "") + "[/cyan]")
    bundle["llm_review"] = {
        "validated": True, "mode": "selection", "prioritize": prioritize, **review,
    }
    _update_snapshot_bundle(race_id, bundle, prioritize=prioritize)


def _decide_selection_bundle(
    review: dict, cands: list, probs, model_bundle: dict, *, prioritize: str = "yield"
) -> tuple[dict, bool]:
    """claude の選定 (review) から最終束を決める。戻り値 (bundle, applied)。

    - picks 明示 (list):
        - 空 `[]` = 明示的な見送り → 空束 (applied=True、claude が「賭けない」と判断)
        - 有効 id あり → その脚で joint Kelly 再構築 (applied=True)
        - 全て不正 id (selected 空) → モデル束維持 (applied=False、未検証扱い)
    - picks 不在 (None) + cuts あり → 後方互換: cuts を除いた脚で再構築 (applied=True)
    - picks も cuts も無し → モデル束維持 (applied=False)

    `if picks:` だと `[]` (見送り) が falsy で素通りし、claude が賭けない判断をした
    のにモデルの全束が validated 扱いで投入され得たため、`picks is not None` で分岐する。
    """
    from . import llm as llm_mod
    from . import portfolio as pf_mod
    picks = review.get("picks")
    if picks is not None:
        pick_set = set(picks)
        selected = [c for c in cands
                    if llm_mod.leg_id({"bet_type": c["bet_type"], "key": c["key"]}) in pick_set]
        if not picks:                       # 明示的な見送り (賭けない)
            return pf_mod.build_bundle([], probs, prioritize=prioritize), True
        if selected:                        # 有効な picks → その脚で再構築
            return pf_mod.build_bundle(selected, probs, prioritize=prioritize), True
        return model_bundle, False          # picks が全て候補 id に不一致 → 適用不可
    if review.get("cuts"):                  # 後方互換: cuts のみ
        cut_set = set(review["cuts"])
        kept = [c for c in cands
                if llm_mod.leg_id({"bet_type": c["bet_type"], "key": c["key"]}) not in cut_set]
        return pf_mod.build_bundle(kept, probs, prioritize=prioritize), True
    return model_bundle, False


def _update_snapshot_bundle(race_id: str, bundle: dict, *, prioritize: str = "yield") -> None:
    """既存 snapshot JSON の recommended_bundle / recommended_bundle_hit を差し替える。

    prioritize="yield" → recommended_bundle (回収優先, 実弾で買う)
    prioritize="hit"   → recommended_bundle_hit (的中優先, おまけ計測)
    """
    path = ROOT / "data" / "predictions" / f"{race_id}.json"
    if not path.exists():
        return
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    field = "recommended_bundle_hit" if prioritize == "hit" else "recommended_bundle"
    snap[field] = bundle
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    label = "recommended_bundle_hit" if prioritize == "hit" else "recommended_bundle"
    console.print(f"[dim]{label} 更新 (LLM 検証反映)[/dim]")


def _llm_scores_path(race_id: str) -> Path:
    return ROOT / "data" / "predictions" / f"{race_id}.llm.json"


def _save_llm_scores(race_id: str, parsed: dict, *, model: str) -> None:
    """score ステージの Claude 指数を `<race_id>.llm.json` に保存 (bet ステージが読む)。"""
    out = _llm_scores_path(race_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "race_id": race_id,
        "scored_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "scores": {str(k): v for k, v in (parsed.get("scores") or {}).items()},
        "notes": parsed.get("notes") or {},
        "summary": parsed.get("summary", ""),
        "confidence": parsed.get("confidence", ""),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[dim]llm scores: {out.relative_to(ROOT)} ({len(payload['scores'])} 頭)[/dim]")


def _load_llm_scores(race_id: str, *, max_age_sec: int = 1800):
    """`<race_id>.llm.json` を読み (scores: dict[int,float], scored_at) を返す。

    無い / 壊れている / 古すぎる (max_age_sec 超過) なら (None, None) を返し、bet ステージは
    モデルのみにフォールバックする。stale な race_id 衝突を弾くため scored_at の鮮度も見る。
    """
    p = _llm_scores_path(race_id)
    if not p.exists():
        return None, None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    scored_at = d.get("scored_at")
    if scored_at:
        try:
            age = (dt.datetime.now() - dt.datetime.fromisoformat(scored_at)).total_seconds()
            if age > max_age_sec:
                return None, scored_at
        except ValueError:
            pass
    scores = {}
    for k, v in (d.get("scores") or {}).items():
        try:
            scores[int(k)] = float(v)
        except (ValueError, TypeError):
            continue
    return (scores or None), scored_at


def _run_score_stage(
    race_id: str, rd, *, aptitudes=None, market_signals=None,
    horse_best_times=None, model: str = "opus",
) -> dict | None:
    """score ステージ: Claude に各馬の強さ指数を出させて `<race_id>.llm.json` にキャッシュ。

    3 経路 (netkeiba analyze / scrape_jra / scrape_keibago) から共通で呼ぶ。返り値は
    parse_horse_scores の dict (scores 空なら保存しない)。Claude 不可/未完了なら None。
    """
    if not llm_mod.is_available():
        console.print("[yellow]claude CLI 不可 — score ステージ skip[/yellow]")
        return None
    console.rule(f"[bold]Claude 考察: 各馬の強さ指数 (score ステージ, web 検索補強)[/bold]")
    chunks: list[str] = []
    saw_result = False
    tool_count = 0
    try:
        for etype, payload in llm_mod.score_horses_stream(
            rd, model=model, aptitudes=aptitudes,
            market_signals=market_signals, horse_best_times=horse_best_times,
        ):
            if etype == "text":
                chunks.append(payload)
                console.print(payload, end="")
            elif etype == "tool_use":
                tool_count += 1
                name = (payload or {}).get("name", "?")
                inp = (payload or {}).get("input") or {}
                q = inp.get("query") or inp.get("q") or inp.get("url") or ""
                label = name.replace("mcp__", "").replace("__", "/")
                if q:
                    qshort = q if len(q) <= 70 else q[:67] + "..."
                    console.print(f"[dim]  🔍 {label}: {qshort}[/dim]")
                else:
                    console.print(f"[dim]  ⚙ {label}[/dim]")
            elif etype == "result":
                saw_result = True
                if payload:
                    chunks.append(payload)
            elif etype == "error":
                console.print(f"[red]score エラー: {payload}[/red]")
    except Exception as ex:  # noqa: BLE001
        console.print(f"[yellow]score ステージ失敗: {ex}[/yellow]")
        return None
    if tool_count:
        console.print(f"[dim](検索/ツール呼び出し計 {tool_count} 回)[/dim]")
    if not saw_result:
        console.print("[yellow]score 未完了 — 指数キャッシュせず (bet はモデルのみ)[/yellow]")
        return None
    parsed = llm_mod.parse_horse_scores("".join(chunks))
    if parsed.get("scores"):
        _save_llm_scores(race_id, parsed, model=model)
        console.print(f"[cyan]score 完了 → {len(parsed['scores'])} 頭に指数付与[/cyan]")
    else:
        console.print("[yellow]score 出力に scores 無し — キャッシュせず[/yellow]")
        return None
    return parsed


def _market_win_index(rd) -> dict[int, float]:
    """単勝オッズの de-vig 暗黙勝率を 0-100 の「市場指数」にした dict を返す。

    Claude 指数とは **独立** な指数。市場1番人気を 100 とする自前スケール (Claude の値には
    一切依存せずアンカーしない) で、`市場指数 = 100 − T_LLM·log(1番人気勝率 / 当該馬勝率)`。
    対数勝率スケール (温度 T_LLM) を使うのは Claude 指数と曲率を揃えて並べやすくするためで、
    値そのものは Claude と独立。両指数の最終的な統合は estimate_probs の市場ブレンドで行う
    (この表示はあくまで独立な2指標の併記)。
    """
    import math
    from .ev import T_LLM, power_method_overround

    horses = [h for h in rd.race.horses
              if not h.absent and getattr(h, "win_odds", 0) and h.win_odds > 0]
    if not horses:
        return {}
    raw = {h.number: 1.0 / float(h.win_odds) for h in horses}
    s = sum(raw.values())
    if s <= 0:
        return {}
    raw = {k: v / s for k, v in raw.items()}
    market = power_method_overround(raw)
    ms = sum(market.values())
    if ms <= 0:
        return {}
    market = {k: v / ms for k, v in market.items()}
    m_max = max(market.values())
    if m_max <= 0:
        return {}
    out: dict[int, float] = {}
    for k, p in market.items():
        idx = 100.0 - T_LLM * math.log(m_max / max(p, 1e-9))
        out[k] = round(max(0.0, min(100.0, idx)), 1)
    return out


def _build_index_compare(rd, llm_win_index: dict[int, float] | None) -> list[dict]:
    """Claude 指数 × 市場指数 を per-horse で並べた配列 (frontend 表示用)。両指数は独立。
    Claude 指数降順 (無ければ市場指数降順)。どちらか一方しか無い馬も含める。"""
    market = _market_win_index(rd)
    claude = llm_win_index or {}
    names = {h.number: (h.name or "") for h in rd.race.horses if not h.absent}
    nums = (set(claude) | set(market)) & set(names)
    rows_out: list[dict] = []
    for n in nums:
        c = claude.get(n)
        mk = market.get(n)
        rows_out.append({
            "number": n,
            "name": names.get(n, ""),
            "claude_index": (round(float(c), 1) if c is not None else None),
            "market_index": (mk if mk is not None else None),
            "diff": (round(float(c) - mk, 1) if (c is not None and mk is not None) else None),
        })
    rows_out.sort(
        key=lambda r: (
            r["claude_index"] if r["claude_index"] is not None
            else (r["market_index"] if r["market_index"] is not None else -1.0)
        ),
        reverse=True,
    )
    return rows_out


def _save_prediction_snapshot(
    race_id: str,
    rd,
    rows,
    plan_rows,
    aptitudes: dict[int, AptitudeIndex] | None = None,
    bet_tables: dict[str, list] | None = None,
    aptitude_top_horses: list[int] | None = None,
    market_signals: dict[int, MarketSignal] | None = None,
    feats: dict | None = None,
    lgbm_info: dict | None = None,
    hit_points: int = 3,
    probs=None,
    llm_win_index: dict[int, float] | None = None,
    llm_blend: float | None = None,
    llm_scored_at: str | None = None,
) -> None:
    # 回収優先 (joint Kelly, EV 最適) の recommended_bundle のみ計算 (実弾で買う対象)。
    # 的中優先 (recommended_bundle_hit / bet_tables_hit) は廃止。
    _ = hit_points   # 旧 Plan B 用 (現スキーマでは未使用)
    recommended_bundle = None
    if probs is not None:
        try:
            cands = pf_mod.candidates_from_ev_rows(rows, bet_tables)
            recommended_bundle = pf_mod.build_bundle(cands, probs, prioritize="yield")
        except Exception as ex:  # noqa: BLE001
            console.print(f"[yellow]recommended_bundle 計算失敗: {ex}[/yellow]")
    # 回収優先 bet_tables に 3連単 (rows = P×O 降順 top 30) を含める
    bet_tables_serial = _serialize_bet_tables(bet_tables) if bet_tables else {}
    if rows:
        bet_tables_serial["trifecta"] = [
            {"key": list(r.key), "odds": r.odds, "popularity": r.popularity,
             "prob": r.prob, "px_o": r.px_o, "tier": r.tier}
            for r in rows[:30]
        ]
    snapshot = {
        "race_id": race_id,
        "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        "venue_name": rd.race.venue_name,
        "race_class": rd.race.race_class,
        "schedule_index": rd.race.schedule_index,
        "race_number": rd.race.race_number,
        "start_at": rd.race.start_at,
        "close_at": rd.race.close_at,
        "odds_updated_at": rd.race.odds_updated_at,
        "distance": rd.race.distance,
        "surface": rd.race.surface,
        "rows": [
            {
                "key": list(r.key),
                "odds": r.odds,
                "popularity": r.popularity,
                "prob": r.prob,
                "px_o": r.px_o,
                "tier": r.tier,
            }
            for r in rows
        ],
        "horse_aptitude": _serialize_aptitudes(rd, aptitudes) if aptitudes else [],
        "horse_best_times": _serialize_best_times(rd, feats) if feats else [],
        "market_signals": _serialize_market_signals(rd, market_signals) if market_signals else [],
        "model_info": {
            "available": bool(lgbm_info and lgbm_info.get("available")),
            "n_features": (lgbm_info or {}).get("n_features", 0),
            "trained_at": (lgbm_info or {}).get("trained_at"),
            "engine": "lgbm" if (lgbm_info and lgbm_info.get("available")) else "linear-fallback",
        } if lgbm_info is not None else {"engine": "unknown"},
        # 回収優先 bet_tables (3連単 を含む全 7 券種、各 P×O 降順 top 30)
        "bet_tables": bet_tables_serial,
        "bet_tables_g": (
            _serialize_bet_tables_g(bet_tables, aptitude_top_horses)
            if bet_tables and aptitude_top_horses else {}
        ),
        "aptitude_top_horses": list(aptitude_top_horses or []),
        # 「Claude 総合オススメ」= 全 bet type 横断の joint Kelly 最適まとめ買い束 (回収優先, 実弾)。
        "recommended_bundle": recommended_bundle,
        # 2段パイプライン: Claude 考察由来の各馬指数を model fundamental に合成した痕跡。
        # llm_win_index=null は score 未完了/未実施 (= モデルのみ) のフォールバックを意味する。
        "llm_win_index": ({str(k): v for k, v in llm_win_index.items()}
                          if llm_win_index else None),
        "llm_blend": llm_blend,
        "llm_scored_at": llm_scored_at,
        "llm_fallback": llm_win_index is None,
        # 市場指数 (単勝オッズ de-vig → 0-100、Claude 独立・市場1番人気=100)。
        "market_win_index": ({str(k): v for k, v in _market_win_index(rd).items()}
                             or None),
        # Claude 指数 × 市場指数 を per-horse で併記した表 (差 = Claude − 市場)。
        "index_compare": _build_index_compare(rd, llm_win_index),
    }
    out = ROOT / "data" / "predictions" / f"{race_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[dim]prediction snapshot: {out.relative_to(ROOT)}[/dim]")


def _print_race_header(rd) -> None:
    r = rd.race
    surface = f" {r.surface}" if r.surface else ""
    direction = f"({r.direction})" if r.direction else ""
    console.rule(
        f"[bold cyan]{r.venue_name} {r.schedule_index}日目 {r.race_number}R "
        f"({r.race_class},{surface}{r.distance}m{direction}) — cupId={r.cup_id}[/bold cyan]"
    )
    console.print(
        f"[dim]オッズ更新: unix={r.odds_updated_at}, 出走: {r.entries_number}頭[/dim]"
    )


def _print_horse_table(rd) -> None:
    tbl = Table(
        title="出走馬 (純2着 = 連対率−1着率, 純3着 = 3連対率−連対率)",
        show_lines=False,
    )
    tbl.add_column("馬", justify="right", style="bold")
    tbl.add_column("枠", justify="right")
    tbl.add_column("馬名")
    tbl.add_column("性齢", justify="center")
    tbl.add_column("斤量", justify="right")
    tbl.add_column("馬体", justify="right")
    tbl.add_column("騎手")
    tbl.add_column("レート", justify="right")
    tbl.add_column("1着率", justify="right")
    tbl.add_column("連対率", justify="right")
    tbl.add_column("3連対率", justify="right")
    tbl.add_column("純2着", justify="right")
    tbl.add_column("純3着", justify="right")
    tbl.add_column("単勝", justify="right")

    for row in ev_mod.horse_table(rd):
        mark = ""
        if row["pure2"] >= 25:
            mark += " [yellow]2着★[/yellow]"
        if row["pure3"] >= 18:
            mark += " [yellow]3着★[/yellow]"
        if row["tr"] >= 40 and not mark:
            mark = " [cyan]3連対★[/cyan]"
        body = (
            f"{row['body_weight']}({row['body_weight_diff']:+d})"
            if row["body_weight"] else "-"
        )
        absent = " [red](取消)[/red]" if row["absent"] else ""
        tbl.add_row(
            str(row["no"]),
            str(row["bracket"]) if row["bracket"] else "-",
            f"{row['name']}{mark}{absent}",
            row["sex_age"] or "-",
            f"{row['weight']:.1f}" if row["weight"] else "-",
            body,
            row["jockey"] or "-",
            f"{row['rating']:.1f}" if row["rating"] else "-",
            f"{row['win']:.1f}%",
            f"{row['qn']:.1f}%",
            f"{row['tr']:.1f}%",
            f"{row['pure2']:.1f}%",
            f"{row['pure3']:.1f}%",
            f"{row['win_odds']:.1f}" if row["win_odds"] else "-",
        )
    console.print(tbl)


_BET_TYPE_JP = {
    "win": "単勝",
    "place": "複勝",
    "quinella": "馬連",
    "wide": "ワイド",
    "exacta": "馬単",
    "trio": "3連複",
}

# CLI / UI で表示する bet type の順序 (リスク低 → 高)
_BET_TYPE_ORDER = ("win", "place", "quinella", "wide", "exacta", "trio")


def _aptitude_top_horses(
    aptitudes: dict[int, AptitudeIndex],
    n: int = 6,
) -> list[int]:
    """適性総合上位 N 頭の馬番リストを返す (total 降順)。"""
    if not aptitudes:
        return []
    return [
        num for num, _ in sorted(
            aptitudes.items(), key=lambda kv: kv[1].total, reverse=True
        )[:n]
    ]


def _print_bet_tables(
    bet_tables: dict[str, list],
    aptitude_top_horses: list[int] | None = None,
) -> None:
    """単勝/複勝/馬連/ワイド/馬単/3連複 等の bet type 横断 EV top 表を表示。

    bet type ごとに別表で出す (各 top 10 / P×O 降順)。
    aptitude_top_horses が渡されていれば、各 bet type の「適性ゲート→EV足切り」picks も表示。
    表示順は _BET_TYPE_ORDER (リスク低 → 高)。
    """
    if not bet_tables:
        return
    ordered_bts = [bt for bt in _BET_TYPE_ORDER if bt in bet_tables]
    # _BET_TYPE_ORDER に無い未知の bet type も末尾に追加
    ordered_bts.extend(bt for bt in bet_tables if bt not in _BET_TYPE_ORDER)
    for bt in ordered_bts:
        rows = bet_tables[bt]
        if not rows:
            continue
        name = _BET_TYPE_JP.get(bt, bt)
        tbl = Table(
            title=f"{name} ({bt}) P×O 上位 10",
            show_lines=False,
        )
        tbl.add_column("#", justify="right", style="dim")
        tbl.add_column("買い目", style="bold")
        tbl.add_column("人気", justify="right")
        tbl.add_column("オッズ", justify="right")
        tbl.add_column("推定 P", justify="right")
        tbl.add_column("P×O", justify="right")
        tbl.add_column("帯")
        tbl.add_column("適性G")
        g_picks: list = []
        if aptitude_top_horses:
            g_picks = ev_mod.plan_aptitude_ev_bet(rows, aptitude_top_horses)
        g_picks_set = {tuple(p.key) for p in g_picks}
        for i, r in enumerate(rows[:10], 1):
            in_g = tuple(r.key) in g_picks_set
            tbl.add_row(
                str(i),
                r.label,
                str(r.popularity) if r.popularity else "-",
                f"{r.odds:.1f}",
                f"{r.prob * 100:.2f}%",
                _color_pxo(r.px_o),
                _tier_jp(r.tier),
                "[bold magenta]G[/bold magenta]" if in_g else "-",
            )
        console.print(tbl)
        if g_picks:
            joined = ", ".join(f"{p.label}({p.px_o:.2f})" for p in g_picks)
            console.print(
                f"  [magenta]→ {name} Plan G (適性 top {len(aptitude_top_horses)} 頭 → P×O≥{ev_mod.PXO_FLOOR:.2f}): "
                f"{len(g_picks)}点: {joined}[/magenta]"
            )


def _print_market_signals(rd, signals: dict[int, MarketSignal]) -> None:
    """市場乖離 (1 着型 / 3 着型 / 標準) を horse 順に表示。3 着型のみ強調。"""
    if not signals:
        return
    # 3 着型 or 1 着型に分類された馬がある場合のみ表示 (標準だらけならノイズ)
    interesting = [s for s in signals.values() if s.interpretation in ("3着型", "1着型", "極端")]
    if not interesting:
        return
    tbl = Table(
        title="市場乖離 (単勝 vs 複勝オッズの implied prob 比率)",
        show_lines=False,
    )
    tbl.add_column("馬", justify="right", style="bold")
    tbl.add_column("馬名")
    tbl.add_column("単勝", justify="right")
    tbl.add_column("複(下限)", justify="right")
    tbl.add_column("win%", justify="right")
    tbl.add_column("place%", justify="right")
    tbl.add_column("ratio", justify="right")
    tbl.add_column("解釈")
    name_by_n = {h.number: h.name for h in rd.race.horses}
    # 解釈順: 3 着型 → 1 着型 → 極端 → (標準は省略)
    order = {"3着型": 0, "1着型": 1, "極端": 2}
    for s in sorted(interesting, key=lambda x: (order.get(x.interpretation, 9), -x.place_to_win_ratio)):
        if s.interpretation == "3着型":
            mark = "[bold magenta]3着型[/bold magenta]"
        elif s.interpretation == "1着型":
            mark = "[bold cyan]1着型[/bold cyan]"
        else:
            mark = f"[red]{s.interpretation}[/red]"
        tbl.add_row(
            str(s.number),
            name_by_n.get(s.number, "-"),
            f"{s.win_odds:.1f}" if s.win_odds else "-",
            f"{s.place_odds_min:.1f}" if s.place_odds_min else "-",
            f"{s.win_implied*100:.2f}",
            f"{s.place_implied*100:.2f}",
            f"{s.place_to_win_ratio:.2f}",
            mark,
        )
    console.print(tbl)
    console.print(
        "[dim]3 着型 = 市場が「3 着までは堅いが 1 着は薄い」と見る馬 (= Plan G の 2/3 着スロット候補)。"
        "1 着型 = 市場が「1 着取らないと終わり」と見る馬 (= Plan G の 1 着スロット候補)。[/dim]"
    )


def _print_aptitudes(rd, aptitudes: dict[int, AptitudeIndex]) -> None:
    """各馬の適性指数 (0-100 / レース内相対) + 因子内訳 + 主要根拠を表示。"""
    if not aptitudes:
        return
    tbl = Table(
        title="適性指数 (0-100, 同レース相対 / 重み付け総合)",
        show_lines=False,
    )
    tbl.add_column("馬", justify="right", style="bold")
    tbl.add_column("馬名")
    tbl.add_column("総合", justify="right", style="bold magenta")
    tbl.add_column("能力", justify="right")
    tbl.add_column("距離", justify="right")
    tbl.add_column("末脚", justify="right")
    tbl.add_column("コース", justify="right")
    tbl.add_column("馬場", justify="right")
    tbl.add_column("状態", justify="right")
    tbl.add_column("騎手", justify="right")
    tbl.add_column("ペース", justify="right")
    tbl.add_column("重賞", justify="right")
    tbl.add_column("主要根拠")

    name_by_n = {h.number: h.name for h in rd.race.horses}
    # 総合降順で表示 (見やすさ優先)
    sorted_items = sorted(aptitudes.items(), key=lambda kv: kv[1].total, reverse=True)
    for n, ai in sorted_items:
        tbl.add_row(
            str(n),
            name_by_n.get(n, "-"),
            f"{ai.total:5.1f}",
            f"{ai.ability:4.0f}",
            f"{ai.distance_fit:4.0f}",
            f"{ai.last3f:4.0f}",
            f"{ai.surface_fit:4.0f}",
            f"{ai.going_fit:4.0f}",
            f"{ai.condition:4.0f}",
            f"{ai.jockey_fit:4.0f}",
            f"{ai.pace_fit:4.0f}",
            f"{ai.graded_record:4.0f}",
            ", ".join(ai.reasons) if ai.reasons else "-",
        )
    console.print(tbl)


_WEATHER_LABEL = {
    100: "晴", 200: "曇", 300: "雨", 400: "雪", 500: "霧",
}
_WIND_DIR = [
    "無", "北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
    "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西",
]


def _print_weather(rd) -> None:
    w = rd.race.weather
    if not w:
        if rd.race.weather_text:
            console.print(f"[bold]天候/馬場:[/bold] {rd.race.weather_text}")
        return
    label = _WEATHER_LABEL.get(w.code, f"code={w.code}")
    wd = _WIND_DIR[w.wind_direction] if 0 <= w.wind_direction < len(_WIND_DIR) else f"dir={w.wind_direction}"
    rain = f", 降水 {w.precipitation:.1f}mm/h" if w.precipitation > 0 else ""
    track = f", 馬場 {w.track_condition}" if w.track_condition else ""
    console.print(
        f"[bold]天候:[/bold] {label}, 気温 {w.temperature:.1f}℃, "
        f"風 {wd} {w.wind_speed:.1f}m/s{rain}{track}"
    )


def _print_predictions(rd) -> None:
    preds = rd.race.predictions
    if not preds:
        return
    console.print("[bold]netkeiba 予想:[/bold]")
    for p in preds:
        tag = "[cyan]AI[/cyan]" if p.is_ai else "[yellow]人[/yellow]"
        rate = f"勝率{p.winning_rate}% ({p.winning}/{p.total})" if p.total else ""
        keys_str = ", ".join(f"{k[0]}-{k[1]}-{k[2]}" for k in p.trifecta_keys[:8])
        if len(p.trifecta_keys) > 8:
            keys_str += f", +{len(p.trifecta_keys)-8}件"
        elif not keys_str:
            keys_str = "(推奨3連単なし)"
        console.print(f"  {tag} {p.name} {rate}: {keys_str}")
        if p.comment:
            console.print(f"    [dim]{p.comment}[/dim]")


def _print_interviews(rd) -> None:
    horses_with_comment = [h for h in rd.race.horses if h.interview_comment]
    if not horses_with_comment:
        return
    console.print("[bold]関係者コメント:[/bold]")
    for h in horses_with_comment:
        c = h.interview_comment
        if len(c) > 200:
            c = c[:200] + "..."
        console.print(f"  [bold]{h.number}[/bold] {h.name}: [dim]{c}[/dim]")


def _print_top(rows, n: int) -> None:
    tbl = Table(title=f"P×O ランキング 上位 {n} 件", show_lines=False)
    tbl.add_column("#", justify="right", style="dim")
    tbl.add_column("買い目", style="bold")
    tbl.add_column("オッズ", justify="right")
    tbl.add_column("人気", justify="right")
    tbl.add_column("推定P", justify="right")
    tbl.add_column("市場率", justify="right")
    tbl.add_column("P×O", justify="right")
    tbl.add_column("評価")
    tbl.add_column("帯")
    for i, r in enumerate(rows[:n], 1):
        tbl.add_row(
            str(i),
            f"{r.key[0]}-{r.key[1]}-{r.key[2]}",
            f"{r.odds:.1f}",
            str(r.popularity),
            f"{r.prob * 100:.2f}%",
            _market_rate_str(r.odds),
            _color_pxo(r.px_o),
            _eval_label(r.px_o),
            _tier_jp(r.tier),
        )
    console.print(tbl)


def _print_top_by_prob(rows, n: int, pxo_floor: float = 0.0) -> None:
    if not rows or n <= 0:
        return
    if pxo_floor > 0:
        filtered = [r for r in rows if r.px_o >= pxo_floor]
        if not filtered:
            console.print(f"[dim]推定当選率ランキング: P×O ≥ {pxo_floor:.2f} を満たす目なし[/dim]")
            return
        subtitle = f" (P×O ≥ {pxo_floor:.2f} で絞込)"
    else:
        filtered = list(rows)
        subtitle = " (EV 制約なし)"
    sorted_rows = sorted(filtered, key=lambda r: r.prob, reverse=True)
    tbl = Table(title=f"推定当選率ランキング 上位 {n} 件{subtitle}", show_lines=False)
    tbl.add_column("#", justify="right", style="dim")
    tbl.add_column("買い目", style="bold")
    tbl.add_column("推定P", justify="right")
    tbl.add_column("市場率", justify="right")
    tbl.add_column("オッズ", justify="right")
    tbl.add_column("人気", justify="right")
    tbl.add_column("P×O", justify="right")
    tbl.add_column("評価")
    tbl.add_column("帯")
    for i, r in enumerate(sorted_rows[:n], 1):
        tbl.add_row(
            str(i),
            f"{r.key[0]}-{r.key[1]}-{r.key[2]}",
            f"{r.prob * 100:.2f}%",
            _market_rate_str(r.odds),
            f"{r.odds:.1f}",
            str(r.popularity),
            _color_pxo(r.px_o),
            _eval_label(r.px_o),
            _tier_jp(r.tier),
        )
    console.print(tbl)


def _print_low_band(rows, low: int, high: int, limit: int) -> None:
    band = [r for r in rows if low <= r.popularity <= high and r.px_o >= PXO_FLOOR]
    if not band:
        console.print(f"[dim]人気 {low}-{high} 位帯に +EV なし[/dim]")
        return
    tbl = Table(title=f"穴帯 +EV (人気 {low}-{high} 位)", show_lines=False)
    tbl.add_column("人気", justify="right", style="dim")
    tbl.add_column("買い目", style="bold")
    tbl.add_column("オッズ", justify="right")
    tbl.add_column("推定P", justify="right")
    tbl.add_column("P×O", justify="right")
    tbl.add_column("帯")
    for r in band[:limit]:
        tbl.add_row(
            str(r.popularity),
            f"{r.key[0]}-{r.key[1]}-{r.key[2]}",
            f"{r.odds:.1f}",
            f"{r.prob * 100:.2f}%",
            _color_pxo(r.px_o),
            _tier_jp(r.tier),
        )
    console.print(tbl)


def _print_plans(
    rows,
    hit_points: int = 3,
    hit_budget_ratio: float = 0.2,
    total_budget: int = 10_000,
    aptitude_top_horses: list[int] | None = None,
) -> None:
    """3連単 解析結果 (回収優先) を 1 点単位で参考表示するだけ (的中優先は廃止)。
    実際の購入は recommended_bundle (回収優先 joint Kelly) で行われる。
    """
    _ = (aptitude_top_horses, hit_budget_ratio, hit_points)   # 旧互換 (CLI 引数受領)
    yield_picks = [r for r in rows if r.px_o >= PXO_FLOOR][:5]
    for title, picks in (
        ("3連単・回収優先 (P×O 降順 top 5)", yield_picks),
    ):
        if not picks:
            console.print(f"[bold]{title}:[/bold] [red]対象なし、スキップ推奨[/red]")
            continue
        joined = ", ".join(f"{p.key[0]}-{p.key[1]}-{p.key[2]}({p.px_o:.2f})" for p in picks)
        total_p = sum(p.prob for p in picks)
        avg_o = sum(p.odds for p in picks) / len(picks)
        ev = sum(p.px_o for p in picks) / len(picks)
        per_point = total_budget // len(picks) if picks else 0
        console.print(
            f"[bold]{title}:[/bold] {joined}\n"
            f"  [dim]点数={len(picks)} 1点 ¥{per_point:,} (枠 ¥{total_budget:,}) "
            f"合計的中率={total_p*100:.2f}% 平均オッズ={avg_o:.1f} EV={ev:.2f}[/dim]"
        )


def _print_judgment_notes(rd, rows) -> None:
    notes: list[str] = []
    top = rows[0] if rows else None
    if top:
        if top.px_o < PXO_FLOOR:
            notes.append(f"最高 P×O が {PXO_FLOOR:.2f} 未満。市場が効率的、スキップ推奨。")
        elif top.px_o < 1.2:
            notes.append("最高 P×O が控えめ。オッズ変動で容易に EV<1 に転落するため締切直前のオッズ再確認推奨。")
    if rd.race.odds_updated_at == 0:
        notes.append("オッズ未確定。発走直前まで P×O が変動する。")
    notes.append(
        "デフォルト確率は (1着率 × レーティング) ベースの粗い推定。"
        "騎手・馬場・距離適性を見て YAML (--probs) で上書きするのが本来の運用。"
    )
    console.print("[bold]重要判断ポイント:[/bold]")
    for n in notes:
        console.print(f"  - {n}")


def _print_llm_evaluation(
    rd,
    rows,
    *,
    model: str,
    ev_max: Optional[float] = None,
    min_prob: Optional[float] = None,
    probs=None,
    aptitudes: dict | None = None,
    aptitude_top_horses: list[int] | None = None,
    market_signals: dict | None = None,
    horse_best_times: list | None = None,
) -> str:
    if not llm_mod.is_available():
        console.print("[yellow]claude CLI が見つかりません。--no-llm でスキップ可。[/yellow]")
        return ""
    console.rule(f"[bold magenta]Claude による評価 (model={model})[/bold magenta]")

    final_text = ""
    text_buf: list[str] = []
    saw_init = False
    tool_count = 0

    for etype, payload in llm_mod.evaluate_stream(
        rd, rows, model=model, ev_max=ev_max, min_prob=min_prob, probs=probs,
        aptitudes=aptitudes, aptitude_top_horses=aptitude_top_horses,
        market_signals=market_signals, horse_best_times=horse_best_times,
    ):
        if etype == "init":
            saw_init = True
            mcps = payload.get("mcp_servers") or []
            ok = [m for m in mcps if m.get("status") in ("connected", "ready", "ok") or not m.get("status")]
            ng = [m for m in mcps if m.get("status") not in ("connected", "ready", "ok") and m.get("status")]
            if mcps:
                ok_names = ", ".join(m.get("name", "?") for m in ok) or "(なし)"
                console.print(f"[dim]✓ MCP 起動: {ok_names}[/dim]")
                if ng:
                    ng_lines = ", ".join(f"{m.get('name')}({m.get('status')})" for m in ng)
                    console.print(f"[yellow]⚠ MCP 接続失敗: {ng_lines}[/yellow]")
            else:
                console.print("[yellow]⚠ MCP サーバが 1 つも認識されませんでした[/yellow]")
        elif etype == "tool_use":
            tool_count += 1
            name = payload.get("name", "?")
            inp = payload.get("input") or {}
            q = inp.get("query") or inp.get("q") or inp.get("url") or ""
            label = name.replace("mcp__", "").replace("__", "/")
            if q:
                qshort = q if len(q) <= 70 else q[:67] + "..."
                console.print(f"[dim]  🔍 {label}: {qshort}[/dim]")
            else:
                console.print(f"[dim]  ⚙ {label}[/dim]")
        elif etype == "text":
            text_buf.append(payload)
        elif etype == "result":
            final_text = payload
        elif etype == "error":
            console.print(f"[red]✗ {payload}[/red]")

    if not saw_init:
        console.print("[yellow](stream-json イベントが届きませんでした)[/yellow]")

    out = final_text or "\n".join(text_buf)
    if out.strip():
        console.print()
        console.print(out.strip())
    else:
        console.print("[yellow]評価が空でした[/yellow]")
    if tool_count:
        console.print(f"[dim](検索/ツール呼び出し計 {tool_count} 回)[/dim]")
    return out.strip()


def _refresh_and_reevaluate(
    *,
    url: str,
    rd_old,
    rows_old,
    initial_eval: str,
    minutes_before: int,
    model: str,
    no_llm: bool,
    no_cache: bool,
    show: int,
    show_prob: int,
    prob_floor: float,
    show_low: int,
    ev_max: Optional[float] = None,
    min_prob: Optional[float] = None,
    market_blend: float = 0.78,
    market_floor: float = 0.01,
    hit_points: int = 3,
    hit_budget_ratio: float = 0.2,
    aptitude_top: int = 6,
    with_exacta: bool = False,
    with_trio: bool = False,
) -> None:
    close_at = rd_old.race.close_at
    if not close_at:
        console.print("[yellow]closeAt 不明のため refresh スキップ[/yellow]")
        return

    now = int(time.time())
    target = close_at - minutes_before * 60

    if now >= close_at:
        console.print(f"[red]既に締切を過ぎています (close={_fmt_ts(close_at)})。refresh スキップ[/red]")
        return

    console.rule(f"[bold yellow]Refresh モード: 締切 {minutes_before} 分前まで待機[/bold yellow]")
    console.print(
        f"発走: {_fmt_ts(rd_old.race.start_at)} / 締切: {_fmt_ts(close_at)} / "
        f"再取得予定: {_fmt_ts(target)}"
    )

    if now < target:
        _countdown(target)
    else:
        console.print("[dim]既に締切 N 分前を過ぎているので即時 refresh[/dim]")

    console.print(f"[dim]fetching {url} (refresh)...[/dim]")
    try:
        rd2 = fetch_and_parse(url, with_exacta=with_exacta, with_trio=with_trio)
    except NetkeibaBlocked as ex:
        console.print(
            f"[bold red]refresh 時に netkeiba block 検出。初回 evaluation を維持して終了。[/bold red]\n"
            f"[dim]{ex}[/dim]"
        )
        return
    race_id = f"{rd2.race.cup_id}-{rd2.race.schedule_index}-{rd2.race.race_number}"
    if not no_cache:
        rid2 = extract_race_id(url) or race_id
        try:
            sh_html2 = fetch_html(shutuba_url(rid2), settle_ms=1000)
            out = cache_html(sh_html2, rid2, ROOT, suffix="-refresh")
            console.print(f"[dim]cached: {out}[/dim]")
        except Exception:
            pass

    feats2 = build_features(rd2)
    aptitudes2 = compute_aptitudes(rd2, feats=feats2)
    apt_top2 = _aptitude_top_horses(aptitudes2, n=aptitude_top)
    market_signals2 = compute_market_signals(rd2)
    lgbm_info2 = ev_mod.lgbm_status()
    probs2 = ev_mod.estimate_probs(rd2, market_blend=market_blend, market_floor=market_floor)
    probs2 = ev_mod.load_probs(None, probs2)
    rows2 = ev_mod.build_table(rd2, probs2)
    bet_tables2 = ev_mod.build_all_bet_tables(rd2, probs2)

    console.print(
        f"[dim]オッズ更新: 初回 unix={rd_old.race.odds_updated_at} → "
        f"再取得 unix={rd2.race.odds_updated_at}[/dim]"
    )

    min_prob_dec = min_prob / 100.0 if min_prob is not None else None
    plan_rows2 = ev_mod.apply_caps(rows2, ev_max=ev_max, min_prob=min_prob_dec)

    if not no_cache:
        _save_prediction_snapshot(
            race_id, rd2, rows2, plan_rows2, aptitudes2, bet_tables2, apt_top2, market_signals2,
            feats=feats2, lgbm_info=lgbm_info2, hit_points=hit_points, probs=probs2,
        )

    _print_top(rows2, n=show)
    _print_top_by_prob(rows2, n=show_prob, pxo_floor=prob_floor)
    _print_diff(rows_old, rows2)
    _print_low_band(rows2, low=51, high=150, limit=show_low)
    _print_plans(
        plan_rows2,
        hit_points=hit_points,
        hit_budget_ratio=hit_budget_ratio,
        aptitude_top_horses=apt_top2,
    )
    _print_bet_tables(bet_tables2, aptitude_top_horses=apt_top2)
    _print_judgment_notes(rd2, rows2)

    if not no_llm and not no_cache:
        # refresh 時も「総合オススメ束への web 検索補強」だけを行う。
        # 旧 _print_llm_refresh_evaluation の 3連単 evidence は廃止。
        best_times2_for_llm = _serialize_best_times(rd2, feats2) if feats2 else []
        _validate_and_update_bundle(
            race_id, rd2, probs2, rows2, bet_tables2,
            aptitudes=aptitudes2, market_signals=market_signals2,
            horse_best_times=best_times2_for_llm, model=model,
        )


def _countdown(target: int) -> None:
    target_str = _fmt_ts(target)
    if console.is_terminal:
        with console.status(f"[dim]{target_str} まで待機中...[/dim]") as status:
            while True:
                now = int(time.time())
                remaining = target - now
                if remaining <= 0:
                    break
                hrs, rem = divmod(remaining, 3600)
                mins, secs = divmod(rem, 60)
                if hrs:
                    msg = f"[dim]{target_str} まで残り {hrs:d}:{mins:02d}:{secs:02d}[/dim]"
                else:
                    msg = f"[dim]{target_str} まで残り {mins:02d}:{secs:02d}[/dim]"
                status.update(msg)
                time.sleep(min(remaining, 5))
    else:
        console.print(f"[dim]{target_str} まで待機開始[/dim]")
        last_print = 0.0
        while True:
            now = time.time()
            remaining = int(target - now)
            if remaining <= 0:
                break
            if now - last_print >= 30:
                hrs, rem = divmod(remaining, 3600)
                mins, secs = divmod(rem, 60)
                if hrs:
                    console.print(f"[dim]{target_str} まで残り {hrs:d}:{mins:02d}:{secs:02d}[/dim]")
                else:
                    console.print(f"[dim]{target_str} まで残り {mins:02d}:{secs:02d}[/dim]")
                last_print = now
            time.sleep(min(remaining, 5))


def _fmt_ts(unix: int) -> str:
    if not unix:
        return "?"
    return dt.datetime.fromtimestamp(unix).strftime("%H:%M:%S")


def _print_diff(rows_old, rows_new) -> None:
    by_old = {r.key: r for r in rows_old}
    by_new = {r.key: r for r in rows_new}
    up: list[tuple] = []
    down: list[tuple] = []
    for key in by_new.keys() | by_old.keys():
        old = by_old.get(key)
        new = by_new.get(key)
        if old is None or new is None:
            continue
        delta = new.px_o - old.px_o
        if delta >= 0.3:
            up.append((new, old, delta))
        elif delta <= -0.3:
            down.append((new, old, delta))
    up.sort(key=lambda x: -x[2])
    down.sort(key=lambda x: x[2])

    if up:
        tbl = Table(title="P×O 上昇 (買い増し候補)")
        tbl.add_column("買い目", style="bold")
        tbl.add_column("人気→", justify="right")
        tbl.add_column("初回 P×O", justify="right")
        tbl.add_column("最新 P×O", justify="right")
        tbl.add_column("Δ", justify="right", style="green")
        for r, old, d in up[:10]:
            tbl.add_row(
                f"{r.key[0]}-{r.key[1]}-{r.key[2]}",
                f"{old.popularity}→{r.popularity}",
                f"{old.px_o:.2f}",
                f"{r.px_o:.2f}",
                f"+{d:.2f}",
            )
        console.print(tbl)

    if down:
        tbl = Table(title="P×O 下降 (売り候補)")
        tbl.add_column("買い目", style="bold")
        tbl.add_column("人気→", justify="right")
        tbl.add_column("初回 P×O", justify="right")
        tbl.add_column("最新 P×O", justify="right")
        tbl.add_column("Δ", justify="right", style="red")
        for r, old, d in down[:10]:
            tbl.add_row(
                f"{r.key[0]}-{r.key[1]}-{r.key[2]}",
                f"{old.popularity}→{r.popularity}",
                f"{old.px_o:.2f}",
                f"{r.px_o:.2f}",
                f"{d:.2f}",
            )
        console.print(tbl)

    if not up and not down:
        console.print("[dim]P×O 有意変動なし (|Δ| < 0.3)[/dim]")


def _print_llm_refresh_evaluation(
    rd,
    rows,
    rows_old,
    initial_eval: str,
    *,
    model: str,
    ev_max: Optional[float] = None,
    min_prob: Optional[float] = None,
    probs=None,
    race_id: str = "",
    no_cache: bool = False,
    hit_points: int = 3,
    hit_budget_ratio: float = 0.2,
    aptitudes: dict | None = None,
    aptitude_top_horses: list[int] | None = None,
) -> None:
    if not llm_mod.is_available():
        return
    console.rule(f"[bold magenta]Claude 締切前再評価 (model={model})[/bold magenta]")

    final_text = ""
    text_buf: list[str] = []
    tool_count = 0

    for etype, payload in llm_mod.evaluate_refresh_stream(
        rd, rows, rows_old, initial_eval, model=model, ev_max=ev_max, min_prob=min_prob, probs=probs,
        aptitudes=aptitudes, aptitude_top_horses=aptitude_top_horses,
    ):
        if etype == "init":
            mcps = payload.get("mcp_servers") or []
            ok_names = ", ".join(m.get("name", "?") for m in mcps) or "(なし)"
            console.print(f"[dim]✓ MCP 起動: {ok_names}[/dim]")
        elif etype == "tool_use":
            tool_count += 1
            name = payload.get("name", "?")
            inp = payload.get("input") or {}
            q = inp.get("query") or inp.get("q") or inp.get("url") or ""
            label = name.replace("mcp__", "").replace("__", "/")
            if q:
                qshort = q if len(q) <= 70 else q[:67] + "..."
                console.print(f"[dim]  🔍 {label}: {qshort}[/dim]")
            else:
                console.print(f"[dim]  ⚙ {label}[/dim]")
        elif etype == "text":
            text_buf.append(payload)
        elif etype == "result":
            final_text = payload
        elif etype == "error":
            console.print(f"[red]✗ {payload}[/red]")

    out = final_text or "\n".join(text_buf)
    if out.strip():
        console.print()
        console.print(out.strip())
    else:
        console.print("[yellow]再評価が空でした[/yellow]")
    if tool_count:
        console.print(f"[dim](再評価の検索/ツール呼び出し計 {tool_count} 回)[/dim]")

    evidence = llm_mod.parse_evidence(out)
    if evidence:
        _print_evidence_adjusted(
            rows, evidence,
            hit_points=hit_points, hit_budget_ratio=hit_budget_ratio,
            aptitude_top_horses=aptitude_top_horses,
        )
        if race_id and not no_cache:
            _save_evidence_to_snapshot(
                race_id, rows, evidence, aptitude_top_horses, hit_points=hit_points,
            )


def _market_rate_str(odds: float) -> str:
    if odds <= 0:
        return "[dim]-[/dim]"
    return f"{100.0 / odds:.2f}%"


def _eval_label(pxo: float) -> str:
    if pxo >= 1.40:
        return f"[bold green]過小 ×{pxo:.2f}[/bold green]"
    if pxo >= PXO_FLOOR:
        return f"[green]過小 ×{pxo:.2f}[/green]"
    if pxo >= 0.95:
        return f"[dim]適正 ×{pxo:.2f}[/dim]"
    if pxo >= 0.70:
        return f"[yellow]過大 ×{pxo:.2f}[/yellow]"
    return f"[red]過大 ×{pxo:.2f}[/red]"


def _color_pxo(v: float) -> str:
    if v >= 3.0:
        return f"[bold magenta]{v:.2f}[/bold magenta]"
    if v >= 1.5:
        return f"[bold green]{v:.2f}[/bold green]"
    if v >= PXO_FLOOR:
        return f"[green]{v:.2f}[/green]"
    return f"[dim]{v:.2f}[/dim]"


def _tier_jp(t: str) -> str:
    return {"honsen": "本線", "chuana": "中穴", "oana": "大穴", "minus": "−EV"}.get(t, t)


if __name__ == "__main__":
    app()
