"""CLI: URL or キャッシュ済み state を渡して EV 表を出力 (競馬版)。"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
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
from rich.markup import escape as _mk_escape
from rich.table import Table

from . import ev as ev_mod
from . import llm as llm_mod
from . import odds_timeline as odds_tl_mod
from . import portfolio as pf_mod
from . import speed_chart as _speed_chart_mod
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

# 市場指数 (表示用) のべき乗温度。市場指数 = 100·(1/odds)^(1/T)。1.0倍は常に 100 (圧倒的)、
# T>1 で強い人気馬を 100 寄りに上げつつ下位も 0-100 に分布させる。T=1 は素の 100/オッズ。
MARKET_INDEX_T = 1.5


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
    market_blend: float = typer.Option(ev_mod.MARKET_BLEND_LIVE, "--market-blend", help="市場暗黙1着率とモデルの混合比。既定=MARKET_BLEND_LIVE(0=市場無視, 実験戦略)。0.78 等で市場寄せ"),
    market_floor: float = typer.Option(0.01, "--market-floor", help="市場確率のフロア"),
    hit_points: int = typer.Option(3, "--hit-points", help="Plan H の点数"),
    hit_budget_ratio: float = typer.Option(0.2, "--hit-budget-ratio", help="当て枠の予算比率"),
    aptitude_top: int = typer.Option(6, "--aptitude-top", help="Plan G の適性 top N (頭数)"),
    with_exacta: bool = typer.Option(False, "--with-exacta", help="馬単 (b5) も fetch (jiku iteration で重い)"),
    with_trio: bool = typer.Option(False, "--with-trio", help="3 連複 (b6) も fetch (jiku iteration で重い)"),
    phase: str = typer.Option("bet", "--phase", help="score = Claude 考察で各馬指数を出しキャッシュ / bet = 指数+市場でP→束→snapshot (既定)"),
    llm_blend: float = typer.Option(ev_mod.LLM_BLEND_DEFAULT, "--llm-blend", help="市場ブレンド後の Claude 指数合成重み (0=合成なし, 1=Claudeのみ。既定 0.75 = 支持馬の最終P Claude≈75%/市場≈19.5%)"),
    speed_v2_blend: float = typer.Option(ev_mod.SPEED_V2_BLEND_LIVE, "--speed-v2-blend", help="v2速度図表(実データpar+pace+trip)を LightGBM fundamental と並列合成する重み (0=図表使わず, 0.5=幾何平均)。既定=SPEED_V2_BLEND_LIVE"),
    trifecta_head_max: int = typer.Option(2, "--t-head-max", help="3連単的中モード: 1着列の最大頭数 (絞る)。指数top2が接戦なら2頭"),
    trifecta_head_gap: float = typer.Option(0.12, "--t-head-gap", help="3連単的中モード: 指数top2の相対差がこれ以下なら1着を2頭に (開き判定)"),
    trifecta_mid: int = typer.Option(4, "--t-mid", help="3連単的中モード: 2着列の頭数 (中くらい)"),
    trifecta_tail: int = typer.Option(7, "--t-tail", help="3連単的中モード: 3着列の頭数 (広げる)"),
    trifecta_no_torigami: bool = typer.Option(False, "--t-no-torigami", help="3連単束のトリガミ防止を無効化 (既定: 防止する)"),
    trifecta_bankroll: int = typer.Option(
        10_000, "--t-bankroll", envvar="KEIBA_TRIFECTA_BANKROLL",
        help="3連単の1レース購入予算 (円)。束の合計購入額をこの予算内に収める (Claude選定・モデルとも)。"
             "env KEIBA_TRIFECTA_BANKROLL でも指定可 (watch-auto/Web UI 経由)"),
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

    # 2段パイプライン score ステージ: Claude に各馬の強さ指数を出させてキャッシュ。
    # その後 fall-through して指数つき snapshot を保存し履歴に出す (ユーザ指示: 指数をつけた
    # 段階で履歴を作ってよい)。**投票 (enqueue) は auto_watch の bet phase のみ**が行うので、
    # ここで snapshot を作っても実弾は飛ばない。bet ステージは締切直前に fresh odds で再計算・上書き。
    if phase == "score":
        best_times_for_score = _serialize_best_times(rd, feats) if feats else []
        _run_score_stage(
            race_id, rd, aptitudes=aptitudes, market_signals=market_signals,
            horse_best_times=best_times_for_score, model=llm_model,
            no_llm=no_llm,
        )
        # fall through → 下の共通解析 + snapshot 保存へ

    # bet/score 共通: キャッシュ指数を読んで estimate_probs に合成する。
    llm_index, llm_support, llm_scale, llm_scored_at, llm_alerts, llm_evidence = _load_llm_scores(race_id)

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
        speed_v2_blend=speed_v2_blend,
        llm_win_index=llm_index, llm_blend=llm_blend,
        llm_support=llm_support, llm_scale=llm_scale,
    )
    probs = ev_mod.load_probs(str(probs_file) if probs_file else None, probs)

    # 3連単束専用の **market-free** probs (市場無視を保証)。market_blend>0 (例: make bet の 0.78)
    # でも3連単束は市場をランキングに使わないため、market_blend=0 の model-only probs を別途用意する。
    # market_blend==0 のときは同一なので再計算しない (no-op コスト回避)。
    if market_blend == 0:
        probs_t = probs
    else:
        probs_t = ev_mod.estimate_probs(
            rd, market_blend=0.0, market_floor=market_floor,
            speed_v2_blend=speed_v2_blend,
            llm_win_index=llm_index, llm_blend=llm_blend,
            llm_support=llm_support, llm_scale=llm_scale,
        )

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
            llm_support=llm_support, llm_scale=llm_scale, llm_alerts=llm_alerts,
            llm_evidence=llm_evidence,
            speed_v2_blend=speed_v2_blend, probs_t=probs_t,
            trifecta_head_max=trifecta_head_max, trifecta_head_gap=trifecta_head_gap,
            trifecta_mid=trifecta_mid, trifecta_tail=trifecta_tail,
            trifecta_no_torigami=trifecta_no_torigami, trifecta_bankroll=trifecta_bankroll,
            # bet 段のみ 3連単買い目選定を Claude に任せる (score 段はキャッシュ作りなので機械)。
            claude_trifecta_select=(phase == "bet" and not no_llm),
            llm_select_model=llm_model,
            stage=phase,
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
                speed_v2_blend=speed_v2_blend,
                hit_points=hit_points,
                hit_budget_ratio=hit_budget_ratio,
                aptitude_top=aptitude_top,
                with_exacta=with_exacta,
                with_trio=with_trio,
                llm_blend=llm_blend,
                probs_file=probs_file,
                phase=phase,
            )


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


# 旧「回収優先AI」(claude -p による EV束 picks/cuts 選定 = _validate_and_update_bundle /
# _decide_selection_bundle / _update_snapshot_bundle) は撤去 (ユーザ指示 2026-06-06)。
# Claude の役割は score ステージ (各馬指数) + 3連単買い目選定 (_claude_select_trifecta) に特化。
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
        # scale="strength": scores は 0-100 強さ指数 (温度パス)。"prob": 推定勝率 % (後方互換)。
        "scale": parsed.get("scale", "strength"),
        "scores": {str(k): v for k, v in (parsed.get("scores") or {}).items()},
        "support": {str(k): v for k, v in (parsed.get("support") or {}).items()},
        # alerts: 各馬の直前/軟情報フラグ配列 (例 {"3": ["取消", "馬体重-12kg"]})。記録/表示用。
        "alerts": {str(k): list(v) for k, v in (parsed.get("alerts") or {}).items()},
        # evidence: 各馬の補強根拠の詳細配列 (上限なし・あるだけ全部)。記録/表示用。
        "evidence": {str(k): list(v) for k, v in (parsed.get("evidence") or {}).items()},
        "notes": parsed.get("notes") or {},
        "summary": parsed.get("summary", ""),
        "confidence": parsed.get("confidence", ""),
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)   # アトミック (並行 read の torn JSON 防止)
    console.print(f"[dim]llm scores: {out.relative_to(ROOT)} ({len(payload['scores'])} 頭)[/dim]")


def _load_llm_scores(race_id: str, *, max_age_sec: int = 1800):
    """`<race_id>.llm.json` を読み (scores, support, scale, scored_at, alerts, evidence) を返す。

    scores=dict[int,float] (scale="strength" なら 0-100 指数、"prob" なら推定勝率 %)、
    support=dict[int,int] (補強根拠件数)、alerts=dict[int,list[str]] (直前/軟情報フラグ、表示用)、
    evidence=dict[int,list[str]] (補強根拠の詳細配列、表示用)。
    無い / 壊れている / 古すぎる (max_age_sec 超過) なら (None, None, "strength", scored_at, None, None)
    を返し、bet ステージはモデルのみにフォールバックする。alerts/evidence は確率には使わず snapshot 表示用。

    env `KEIBA_LLM_SCORE_MAX_AGE_SEC` があれば age gate を上書きする。予測履歴の「オッズ更新」
    (最新オッズのみ・Claude 呼ばない) で、古いキャッシュでも既存の Claude 指数を保持したまま
    オッズ起因フィールドだけ更新するために refresh-odds Job が大きな値を渡す。
    """
    _env_age = (os.environ.get("KEIBA_LLM_SCORE_MAX_AGE_SEC") or "").strip()
    if _env_age:
        try:
            max_age_sec = int(_env_age)
        except ValueError:
            pass
    p = _llm_scores_path(race_id)
    if not p.exists():
        return None, None, "strength", None, None, None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, "strength", None, None, None
    scored_at = d.get("scored_at")
    if scored_at:
        try:
            age = (dt.datetime.now() - dt.datetime.fromisoformat(scored_at)).total_seconds()
            if age > max_age_sec:
                return None, None, "strength", scored_at, None, None
        except ValueError:
            pass
    # isinstance ガード: `or {}` は falsy しか救済せず、非 dict (list 等) だと .items() で
    # AttributeError → bet dispatch ごと死ぬ (llm.parse_horse_scores と同じ防御, 2026-06-10)。
    scores = {}
    src_scores = d.get("scores")
    if isinstance(src_scores, dict):
        for k, v in src_scores.items():
            try:
                scores[int(k)] = float(v)
            except (ValueError, TypeError):
                continue
    support = {}
    src_support = d.get("support")
    if isinstance(src_support, dict):
        for k, v in src_support.items():
            try:
                support[int(k)] = max(0, int(float(v)))
            except (ValueError, TypeError):
                continue
    scale = d.get("scale") or "strength"
    alerts = llm_mod._normalize_alerts(d.get("alerts"))
    evidence = llm_mod._normalize_evidence(d.get("evidence"))
    return (scores or None), (support or None), scale, scored_at, (alerts or None), (evidence or None)


# score ステージ timeout の見積り基準。effort=max の web 検索は 1 ラウンド ~30-50s かかり
# sequential なので、小頭数でも十分な時間を確保しないと kill されて指数キャッシュが作れない
# (実機: 7頭立てで旧 floor 300s に張り付き 11 回検索の途中で timeout)。
# **timeout は常に 15 分 (900s) 固定** (ユーザ指示 2026-06-03)。以前は runway (締切までの残り) で
# 頭打ちしていたが廃止。15 分を使い切りたければ score 帯を締切の ~18 分以上前に始めること
# (--score-window / --score-tolerance)。env KEIBA_SCORE_TIMEOUT があれば絶対上書き。
SCORE_TIMEOUT_SEC = 900            # 常に 15 分


def _score_timeout(rd, n_run: int) -> int:
    """score ステージの timeout。**常に 15 分 (SCORE_TIMEOUT_SEC) 固定**。

    env `KEIBA_SCORE_TIMEOUT` (秒) があれば絶対上書き (運用での手動チューニング用)。
    runway での頭打ちはしない (rd / n_run は呼び出し互換のため受けるが未使用)。
    """
    env = (os.environ.get("KEIBA_SCORE_TIMEOUT") or "").strip()
    if env:
        try:
            v = int(float(env))
            if v > 0:
                return v
        except ValueError:
            pass
    return SCORE_TIMEOUT_SEC


def _run_score_stage(
    race_id: str, rd, *, aptitudes=None, market_signals=None,
    horse_best_times=None, model: str = "opus", no_llm: bool = False,
) -> dict | None:
    """score ステージ: Claude に各馬の強さ指数を出させて `<race_id>.llm.json` にキャッシュ。

    4 経路 (netkeiba analyze / scrape_jra / scrape_keibago / scrape_oddspark) から共通で
    呼ぶ。返り値は parse_horse_scores の dict (scores 空なら保存しない)。Claude 不可/
    未完了/no_llm なら None。no_llm でもオッズ時系列キャプチャは行う (LLM と独立) ので、
    呼び出し側は --no-llm でもこの関数を呼んでよい (2026-06-11 第5R: 旧 netkeiba 経路は
    no_llm を無視して Claude を起動、alt 経路は逆に capture ごと skip していた)。
    """
    # オッズ変動キャプチャ (Step 1, 追加 fetch ゼロ): score 段で取得済みの fresh odds を
    # 時系列に記録。LLM 可否に関わらず取る (is_available チェックより前)。
    odds_tl_mod.capture(race_id, rd, "score")
    if no_llm:
        console.print("[dim]--no-llm — score ステージ (Claude 指数) skip[/dim]")
        return None
    # 既存の当日キャッシュ指数を再利用 (web 検索を再実行しない, 2026-06-24)。同一レースの
    # Claude 指数は一日を通して不変 (市場非依存・各馬の適性/状態ベース) なので、オッズ発売前に
    # 先行生成 → 発売後の再スキャンで snapshot を作る時は再検索せずキャッシュを使う
    # (morning pre-cache → evening snapshot を安価にする)。取消は absent 昇格で別途処理されるため
    # 指数の reuse は安全。KEIBA_SCORE_FORCE_RESCORE=1 で強制再検索。
    if not (os.environ.get("KEIBA_SCORE_FORCE_RESCORE") or "").strip():
        c_scores, c_support, c_scale, c_at, c_alerts, c_evidence = _load_llm_scores(race_id, max_age_sec=10**9)
        if c_scores:
            console.print(f"[cyan]score: 既存 Claude 指数を再利用 ({len(c_scores)} 頭, "
                          f"{c_at}) — web 検索 skip[/cyan]")
            return {"scores": c_scores, "support": c_support or {}, "scale": c_scale,
                    "alerts": c_alerts or {}, "evidence": c_evidence or {},
                    "summary": "", "notes": {}, "confidence": ""}
    if not llm_mod.is_available():
        console.print("[yellow]claude CLI 不可 — score ステージ skip[/yellow]")
        return None
    console.rule(f"[bold]Claude 考察: 各馬の強さ指数 (score ステージ, 全馬 web 検索補強)[/bold]")
    chunks: list[str] = []
    saw_result = False
    tool_count = 0
    # timeout は「締切までの runway」と「頭数の必要量」から決める (env KEIBA_SCORE_TIMEOUT で上書き可)。
    n_run = len([h for h in rd.race.horses if not h.absent])
    score_timeout = _score_timeout(rd, n_run)
    console.print(f"[dim]score timeout = {score_timeout}s ({n_run}頭, "
                  f"締切={_fmt_ts(rd.race.close_at) if rd.race.close_at else '不明'})[/dim]")
    try:
        # score_horses = dispatcher: KEIBA_SCORE_PARALLEL で並列 score (検索大幅増)、
        # それ以外/失敗時は従来の単一セッション score_horses_stream にフォールバック。
        for etype, payload in llm_mod.score_horses(
            rd, model=model, aptitudes=aptitudes,
            market_signals=market_signals, horse_best_times=horse_best_times,
            timeout=score_timeout,
        ):
            if etype == "text":
                chunks.append(payload)
                # markup=False 必須: LLM テキストの `[/...]` は rich MarkupError を raise し、
                # except 側の {ex} print (タグ文字列を含む) で連鎖再発して score 結果ごと
                # 失われる。`[word](url)` 形の markdown リンクも tag 扱いで黙って欠落する
                # (2026-06-11 bughunt 第5R)。
                console.print(payload, end="", markup=False, highlight=False)
            elif etype == "tool_use":
                tool_count += 1
                name = (payload or {}).get("name", "?")
                inp = (payload or {}).get("input") or {}
                q = inp.get("query") or inp.get("q") or inp.get("url") or ""
                label = name.replace("mcp__", "").replace("__", "/")
                # クエリは全文をログに出す (ユーザ指示: クエリもログに出す・truncate しない)。
                # style="dim"+markup=False で q 内の角括弧による rich MarkupError を回避し、
                # soft_wrap=True で 80 桁折返しを抑止 (captured pipe でも 1 行 = shobu が拾える)。
                if q:
                    console.print(f"  🔍 {label}: {q}", style="dim",
                                  markup=False, highlight=False, soft_wrap=True)
                else:
                    console.print(f"  ⚙ {label}", style="dim",
                                  markup=False, highlight=False, soft_wrap=True)
            elif etype == "result":
                saw_result = True
                if payload:
                    chunks.append(payload)
            elif etype == "error":
                console.print(f"[red]score エラー: {_mk_escape(str(payload))}[/red]")
    except Exception as ex:  # noqa: BLE001
        console.print(f"[yellow]score ステージ失敗: {_mk_escape(str(ex))}[/yellow]")
        return None
    if tool_count:
        console.print(f"[dim](検索/ツール呼び出し計 {tool_count} 回)[/dim]")
    if not saw_result:
        console.print("[yellow]score 未完了 — 指数キャッシュせず (bet はモデルのみ)[/yellow]")
        return None
    # parse〜保存も try で包む: parse_horse_scores は raise しない契約だが、想定外の
    # 出力形で例外が漏れると dispatch subprocess ごと rc≠0 で死に、bet 予約まで
    # 失われる (2026-06-10 bughunt)。score は縮退可能 (モデルのみで bet) なので
    # ここで握って None を返す。
    try:
        parsed = llm_mod.parse_horse_scores("".join(chunks))
        if parsed.get("scores"):
            _save_llm_scores(race_id, parsed, model=model)
            console.print(f"[cyan]score 完了 → {len(parsed['scores'])} 頭に指数付与[/cyan]")
        else:
            console.print("[yellow]score 出力に scores 無し — キャッシュせず[/yellow]")
            return None
    except Exception as ex:  # noqa: BLE001
        console.print(f"[yellow]score 出力の parse/保存に失敗: {ex} — キャッシュせず (bet はモデルのみ)[/yellow]")
        return None
    return parsed


# 締切直前 3連単選定の timeout 既定 (秒)。env KEIBA_TRIFECTA_SELECT_TIMEOUT で上書き。
# 検索なしの純粋推論なので短くてよい。runway (締切までの残り) でさらに頭打ちする。
TRIFECTA_SELECT_TIMEOUT_DEFAULT = 75

# 3連単の1レース購入予算 (円) の既定。
TRIFECTA_BANKROLL_DEFAULT = 10_000


EV_BANKROLL_DEFAULT = 10_000


def _ev_bankroll(explicit: int | None = None) -> int:
    """EV束 (recommended_bundle) の1レース予算を解決する。

    明示 → env KEIBA_EV_BANKROLL → 既定 ¥10,000 (旧来の build_bundle 既定と同値 =
    snapshot の歴史的互換)。½Kelly + シェード込み +EV ゲートにより実投入は通常
    予算の 10-30% 程度に収まる。Web UI 経由は env で全 dispatch subprocess に伝播。
    """
    if explicit is not None and explicit > 0:
        return int(explicit)
    env = (os.environ.get("KEIBA_EV_BANKROLL") or "").strip()
    if env:
        try:
            v = int(float(env))
            if v > 0:
                return v
        except ValueError:
            pass
    return EV_BANKROLL_DEFAULT


def _trifecta_bankroll(explicit: int | None = None) -> int:
    """3連単の1レース購入予算を解決する。

    明示指定 (CLI --t-bankroll) があればそれを、無ければ env KEIBA_TRIFECTA_BANKROLL、
    それも無ければ既定 ¥10,000。watch-auto/scraper/Web UI 経路は env で渡るので、明示が
    無くても env を尊重する (全 dispatch 経路で同一予算になる)。
    """
    if explicit is not None and explicit > 0:
        return int(explicit)
    # 旧 env 名 KEIBA_PLAN_T_BANKROLL も互換で読む (旧シェル/ループからの移行期対策)。
    env = (os.environ.get("KEIBA_TRIFECTA_BANKROLL")
           or os.environ.get("KEIBA_PLAN_T_BANKROLL") or "").strip()
    if env:
        try:
            v = int(float(env))
            if v > 0:
                return v
        except ValueError:
            pass
    return TRIFECTA_BANKROLL_DEFAULT


def _claude_select_trifecta(rd, probs_for_t, llm_win_index, aptitudes, *,
                            model: str = "opus", avoid_torigami: bool = True,
                            bankroll: int = 10_000, max_points: int = 48) -> dict | None:
    """締切直前に Claude を起動し 3連単買い目を選定 → トリガミ防止つき束を返す (失敗時 None)。

    指示: 指数出力後、3連単の買い目選定まで Claude に任せる / 残り≈1分で起動・**検索なし高速**。
    keys が空 / 未完了 / 締切間際で時間が無い場合は None を返し、呼び出し側が機械フォーメーション
    (build_trifecta_hitmax) にフォールバックする。
    """
    if not llm_mod.is_available() or not llm_win_index:
        return None
    # 締切までの runway で timeout を頭打ち (締切を跨いで走らない)。
    deadline = rd.race.close_at or ((rd.race.start_at - 120) if rd.race.start_at else 0)
    base_t = TRIFECTA_SELECT_TIMEOUT_DEFAULT
    env_t = (os.environ.get("KEIBA_TRIFECTA_SELECT_TIMEOUT") or "").strip()
    if env_t:
        try:
            base_t = max(10, int(float(env_t)))
        except ValueError:
            pass
    if deadline:
        runway = int(deadline - time.time() - 10)
        if runway <= 15:
            console.print("[yellow]3連単 Claude 買い目選定 skip: 締切間際で時間が無い → 機械フォーメーション[/yellow]")
            return None
        timeout = max(15, min(base_t, runway))
    else:
        timeout = base_t
    # 市場無視: 単勝オッズはプロンプトに渡さない (Claude を市場に引きずらせない)。
    console.rule("[bold]Claude 3連単 買い目選定 (全力的中・締切直前・検索なし高速・市場無視)[/bold]")
    chunks: list[str] = []
    saw_result = False
    try:
        for etype, payload in llm_mod.select_trifecta_stream(
            rd, llm_index=llm_win_index, aptitudes=aptitudes, bankroll=bankroll,
            max_points=max_points, model=model, timeout=timeout,
        ):
            if etype == "text":
                chunks.append(payload)
                # markup=False 必須 (score ステージと同根, 2026-06-11 第5R)
                console.print(payload, end="", markup=False, highlight=False)
            elif etype == "result":
                saw_result = True
                if payload:
                    chunks.append(payload)
            elif etype == "error":
                console.print(f"[red]3連単買い目選定エラー: {_mk_escape(str(payload))}[/red]")
    except Exception as ex:  # noqa: BLE001
        console.print(f"[yellow]3連単 Claude 買い目選定失敗: {_mk_escape(str(ex))}[/yellow]")
        return None
    if not saw_result:
        console.print("[yellow]3連単 Claude 買い目選定 未完了 → 機械フォーメーションにフォールバック[/yellow]")
        return None
    sel = llm_mod.parse_trifecta_selection("".join(chunks))
    keys = sel.get("keys") or []
    if not keys:
        console.print("[yellow]3連単 Claude 買い目選定: keys 空 → 機械フォーメーション[/yellow]")
        return None
    bundle = pf_mod.build_trifecta_from_keys(
        probs_for_t, rd.trifecta, keys,
        bankroll=bankroll, avoid_torigami=avoid_torigami, max_points=max_points)
    if bundle and bundle.get("legs"):
        bundle["llm_select"] = {"summary": sel.get("summary", ""),
                                "confidence": sel.get("confidence", ""),
                                "n_keys": len(keys)}
        console.print(f"[magenta]3連単 Claude 買い目選定: {len(keys)} 買い目 → 束 "
                      f"{bundle['n_points']}点 / ¥{bundle['total_stake']:,}[/magenta]")
        return bundle
    console.print("[yellow]3連単 Claude 買い目選定: 買える目が無く束が空 → 機械フォーメーション[/yellow]")
    return None


def _market_win_index(rd) -> dict[int, float]:
    """単勝オッズ由来の市場指数 (0-100) を per-horse で返す。

    Claude 指数とは **独立** な指標。オッズの素の暗黙率 p=1/オッズ を温度付きべき乗で 0-100
    化する: `市場指数 = 100 · p^(1/MARKET_INDEX_T)`。**単勝 1.0 倍 (p=1.0) で必ず 100** (圧倒的)、
    T>1 で強い人気馬を 100 寄りに持ち上げつつ下位も 0-100 に適度に分布させる (T=1 は素の
    100/オッズ)。de-vig やアンカーはしない。最終的な統合は estimate_probs の市場ブレンドで
    別途行う (この表示はあくまで独立な2指標の併記)。
    """
    exp = 1.0 / MARKET_INDEX_T
    out: dict[int, float] = {}
    for h in rd.race.horses:
        if h.absent:
            continue
        wo = getattr(h, "win_odds", 0)
        if not wo or float(wo) <= 0:
            continue
        p = 1.0 / float(wo)
        out[h.number] = round(max(0.0, min(100.0, 100.0 * (p ** exp))), 1)
    return out


def _build_index_compare(
    rd, llm_win_index: dict[int, float] | None,
    llm_support: dict[int, int] | None = None,
    llm_alerts: dict[int, list[str]] | None = None,
    llm_evidence: dict[int, list[str]] | None = None,
) -> list[dict]:
    """Claude 指数 × 市場指数 を per-horse で並べた配列 (frontend 表示用)。両指数は独立。
    Claude 値降順 (無ければ市場指数降順)。どちらか一方しか無い馬も含める。support は補強根拠件数、
    alerts は直前/軟情報フラグ配列 (無ければ空)、evidence は補強根拠の詳細配列 (無ければ空)。"""
    market = _market_win_index(rd)
    claude = llm_win_index or {}
    support = llm_support or {}
    alerts = llm_alerts or {}
    evidence = llm_evidence or {}
    names = {h.number: (h.name or "") for h in rd.race.horses if not h.absent}
    # alerts/evidence は Claude/市場いずれの指数も無い取消馬でも出したいので nums に含める。
    nums = (set(claude) | set(market) | set(alerts) | set(evidence)) & set(names)
    rows_out: list[dict] = []
    for n in nums:
        c = claude.get(n)
        mk = market.get(n)
        ev_n = list(evidence[n]) if n in evidence and evidence[n] else []
        # evidence がある馬は「根」(support) を **evidence 件数そのもの** にする (UI の根バッジと
        # 展開リストの件数を一致させる)。LLM が support を多めに返しても展開行と食い違わせない。
        # evidence が無い馬は LLM の support (alerts のみ等) をそのまま使う。
        sup_n = len(ev_n) if ev_n else (support[n] if n in support else None)
        rows_out.append({
            "number": n,
            "name": names.get(n, ""),
            "claude_index": (round(float(c), 1) if c is not None else None),
            "market_index": (mk if mk is not None else None),
            "diff": (round(float(c) - mk, 1) if (c is not None and mk is not None) else None),
            "support": (int(sup_n) if sup_n is not None else None),
            "alerts": (list(alerts[n]) if n in alerts and alerts[n] else []),
            "evidence": ev_n,
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
    llm_support: dict[int, int] | None = None,
    llm_scale: str = "strength",
    llm_alerts: dict[int, list[str]] | None = None,
    llm_evidence: dict[int, list[str]] | None = None,
    speed_v2_blend: float | None = None,
    probs_t=None,                       # 3連単束用 market-free probs (無ければ probs を使う)
    trifecta_head_max: int = 2,
    trifecta_head_gap: float = 0.12,
    trifecta_mid: int = 4,
    trifecta_tail: int = 7,
    trifecta_no_torigami: bool = False,
    trifecta_bankroll: int | None = None,     # 3連単の1レース購入予算 (円)。None で env/既定 ¥10,000
    claude_trifecta_select: bool = False,   # bet 段: 3連単買い目選定を Claude に任せる
    llm_select_model: str = "opus",
    stage: str = "bet",                     # "score"=指数出力時の暫定プレビュー / "bet"=締切直前の確定
) -> None:
    # オッズ変動キャプチャ (Step 1): snapshot 保存時 (= bet 段の fresh odds) を時系列に記録。
    # netkeiba 経路の score phase は score 段 capture 後に同じ rd でここへ fall-through するが、
    # odds_hash dedup で重複行にはならない。stage="score" は capture 側も "score" 扱い。
    odds_tl_mod.capture(race_id, rd, stage if stage == "score" else "bet")
    # late-money momentum (paper 計測のみ — 確率/束には一切使わない。arXiv:2509.14645)。
    # score 段 (締切5-7分前) の単勝オッズに対する現在 (bet 段) の比 r = bet/score を馬番別に
    # snapshot へ残す。r<1 = 直前に売れた (informed money の痕跡候補)。score 行が無ければ
    # poll 行 (odds_capture daemon) で代用。基準行は stage 指定で取る (末尾行は今書いた bet 行)。
    # stage="score" (指数出力時の暫定プレビュー) では現在オッズ自体が score 時点なので
    # score→bet 比は無意味 (≈1)。bet 段でのみ ref を取り計測する (それ以外は late_money=None)。
    late_money = None
    try:
        ref = ((odds_tl_mod.latest_row(race_id, "score")
                or odds_tl_mod.latest_row(race_id, "poll")) if stage == "bet" else None)
        cur_win = {str(b.key[0]): b.odds for b in (rd.other_bets or {}).get("win", [])
                   if b.odds and b.odds > 0 and not b.absent}
        ref_win = ((ref or {}).get("odds") or {}).get("win") or {}
        if ref and cur_win:
            ratio = {h: round(cur_win[h] / ref_win[h], 4)
                     for h in cur_win if ref_win.get(h, 0) > 0}
            if ratio:
                gap_min = (dt.datetime.now()
                           - dt.datetime.fromisoformat(ref["captured_at"])
                           ).total_seconds() / 60.0
                # 経路混在 (例: score=netkeiba 3券種 / bet=keibago 6券種) は単勝こそ比較可能
                # だが <5% の変動はノイズ扱いすべき — 券種集合の不一致で検出して flag を残す。
                cur_types = {bt for bt, bets in (rd.other_bets or {}).items() if bets}
                if rd.trifecta:
                    cur_types.add("trifecta")
                late_money = {
                    "score_stage": ref.get("stage"),                 # "score" | "poll"
                    "score_captured_at": ref.get("captured_at"),
                    "gap_min": round(gap_min, 1),
                    "ratio": ratio,                                   # {馬番str: bet/score 比}
                    "source_mix": set((ref.get("odds") or {}).keys()) != cur_types,
                }
    except Exception:  # noqa: BLE001
        late_money = None   # paper 計測の失敗で live betting を止めない
    # EV束 (joint Kelly, モデルのみの参考値 — 実弾投票には使わない)。
    # 的中優先 (recommended_bundle_hit / bet_tables_hit) は廃止。
    _ = hit_points   # 旧 Plan B 用 (現スキーマでは未使用)
    recommended_bundle = None
    # 無情報ガード: fundamental が一様縮退 (past_runs 無し等) のときは EV束を組まない。
    # 一様 fundamental × 市場ブレンドは「市場の平坦化 = 大穴の偽 +EV」を生む
    # (β=0 時代は EV=odds/n で最長オッズを自動購入していた事故の根)。
    model_no_info = True
    if probs is not None:
        try:
            model_no_info = ev_mod.fundamental_no_info(rd)
        except Exception:  # noqa: BLE001
            model_no_info = False   # 判定不能なら従来動作 (組む) に倒す
        if model_no_info:
            console.print("[yellow]EV束: fundamental 無情報 (一様縮退) → 見送り[/yellow]")
        else:
            try:
                cands = pf_mod.candidates_from_ev_rows(rows, bet_tables)
                # kelly_fraction=0.5 (½Kelly): full Kelly は確率の楽観誤差に対して配分が
                # 過大化し成長率が負になり得る (実測: 予測的中率45.8% vs 実測20.8%)。
                # bankroll は env KEIBA_EV_BANKROLL (Web UI から伝播) → 既定 ¥10,000。
                recommended_bundle = pf_mod.build_bundle(
                    cands, probs, prioritize="yield", kelly_fraction=0.5,
                    bankroll=_ev_bankroll())
            except Exception as ex:  # noqa: BLE001
                console.print(f"[yellow]recommended_bundle 計算失敗: {ex}[/yellow]")
    # 3連単的中モード (全力フォーメーション): Claude 指数ドリブンの3連単フォーメーション・市場無視・トリガミ防止あり。
    # recommended_bundle (EV駆動) とは別物として併走計測する (実弾購入は別フラグ判断)。
    # 市場無視を保証するため probs_t (market_blend=0 の model-only) を使い、ランキングは Claude 指数。
    recommended_bundle_t = None
    probs_for_t = probs_t if probs_t is not None else probs
    if probs_t is None:
        # probs_t 未指定 = market-free 保証なし。live 既定 β=0.78 では probs は市場ブレンド済み
        # なので、呼び出し元が probs_t を渡し忘れると実弾束の配分・トリガミ判定が市場汚染される
        # (2026-06-10 review で keibago/jra/oddspark 経路の regression として実発生 → 修正済)。
        console.print("[yellow]⚠ probs_t 未指定 — 3連単束の market-free 保証なし (blended probs にフォールバック)[/yellow]")
    trifecta_bankroll = _trifecta_bankroll(trifecta_bankroll)   # 明示 → env → 既定 ¥10,000
    if probs_for_t is not None:
        # bet 段 (締切直前) は **Claude に 3連単買い目を選定させる** (指数上位から自由構築・検索なし)。
        # 失敗 / timeout / keys 空 / 締切間際 → 従来の機械フォーメーション (build_trifecta_hitmax)。
        if (claude_trifecta_select and llm_win_index
                and not os.environ.get("KEIBA_NO_CLAUDE_TRIFECTA_SELECT")):
            try:
                recommended_bundle_t = _claude_select_trifecta(
                    rd, probs_for_t, llm_win_index, aptitudes,
                    model=llm_select_model, avoid_torigami=(not trifecta_no_torigami),
                    bankroll=trifecta_bankroll)
            except Exception as ex:  # noqa: BLE001
                console.print(f"[yellow]3連単 Claude 買い目選定で例外: {ex} → 機械フォーメーション[/yellow]")
                recommended_bundle_t = None
        if not (recommended_bundle_t and recommended_bundle_t.get("legs")):
            try:
                recommended_bundle_t = pf_mod.build_trifecta_hitmax(
                    probs_for_t, rd.trifecta, rank_index=llm_win_index,
                    head_max=trifecta_head_max, head_gap=trifecta_head_gap,
                    mid_count=trifecta_mid, tail_count=trifecta_tail,
                    avoid_torigami=(not trifecta_no_torigami), bankroll=trifecta_bankroll,
                )
            except Exception as ex:  # noqa: BLE001
                console.print(f"[yellow]3連単束 (recommended_bundle_t) 計算失敗: {ex}[/yellow]")
    if recommended_bundle_t and recommended_bundle_t.get("legs"):
        rt = recommended_bundle_t
        osum = rt.get("odds_summary") or {}
        console.print(
            f"[magenta]3連単的中モード ({rt.get('rank_source')}指数 {rt.get('formation')}): "
            f"3連単 {rt['n_points']}点 / 理論的中率 {rt['covered_prob'] * 100:.1f}% (model基準・過信禁物) / "
            f"¥{rt['total_stake']:,} / 当たれば払戻 ¥{osum.get('min_payout', 0):,}〜¥{osum.get('max_payout', 0):,}"
            f"{' / トリガミ防止' if not trifecta_no_torigami else ''}[/magenta]"
        )
    # 市場アンカー型クロスプール EV (Dr.Z 系, ペーパー計測のみ — 投票には使わない)。
    # 単勝 pool は最も効率的 (live N=324 MLE: α≈0, β≈0.95 = モデルの上乗せ無し) なので、
    # 単勝アンカーの確率で他 pool (複勝/ワイド/3連単等) の歪みをスキャンし、shade 込み
    # px_o ≥ 1.0 の行を snapshot に記録する。蓄積後に仮想 ROI を検証して arm を判断する。
    market_anchor_ev = None
    try:
        probs_m = ev_mod.market_anchor_probs(rd)
        if probs_m is not None:
            ma_tables = dict(ev_mod.build_all_bet_tables(rd, probs_m) or {})
            rows_m = ev_mod.build_table(rd, probs_m)
            if rows_m:
                ma_tables["trifecta"] = ev_mod.trifecta_to_bet_evrow(rows_m)
            ma: dict[str, list] = {}
            for bt, brows in ma_tables.items():
                if bt == "win":
                    continue   # 単勝はアンカー自身 (常に px_o≈払戻率) なので対象外
                sh = pf_mod.DRIFT_SHADE.get(bt, 0.90)
                pos = [
                    {"key": list(r.key), "odds": r.odds, "prob": r.prob,
                     "px_o": r.px_o, "px_o_shaded": round(r.px_o * sh, 4)}
                    for r in brows if r.px_o * sh >= 1.0
                ]
                pos.sort(key=lambda x: x["px_o_shaded"], reverse=True)
                if pos:
                    ma[bt] = pos[:10]
            market_anchor_ev = ma or None
            if market_anchor_ev:
                n_pos = sum(len(v) for v in market_anchor_ev.values())
                console.print(f"[dim]市場アンカー クロスプール候補 (paper): "
                              f"{n_pos} 点 ({', '.join(market_anchor_ev)})[/dim]")
    except Exception as ex:  # noqa: BLE001
        console.print(f"[dim]market_anchor_ev 計算失敗: {ex}[/dim]")
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
        # "score" = Claude 指数出力時の暫定プレビュー (締切前に履歴へ早出し)。
        # "bet"   = 締切直前に fresh odds で再計算した確定版 (score を上書き)。
        "stage": stage,
        "venue_name": rd.race.venue_name,
        "race_class": rd.race.race_class,
        "schedule_index": rd.race.schedule_index,
        "race_number": rd.race.race_number,
        "start_at": rd.race.start_at,
        "close_at": rd.race.close_at,
        "odds_updated_at": rd.race.odds_updated_at,
        "distance": rd.race.distance,
        "surface": rd.race.surface,
        # 出走頭数 (取消除く) — 複勝の頭数ルール (7頭以下=2着まで/4頭以下=発売なし) 判定の
        # 権威値。読む側の推定 (bet_tables.win 長 = odds≤0 馬を欠く) より正確 (2026-06-11 第5R)。
        "n_runners": len([h for h in rd.race.horses if not h.absent]),
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
        # fundamental が一様縮退 (past_runs 無し等) で EV束を見送ったか (無情報ガードの痕跡)。
        "model_no_info": model_no_info,
        # 市場フリーの model 確率 (= probs_t.win, Claude 指数込み・市場ブレンド無し)。
        # scripts/fit_blend_mle.py が Benter 2-step (α,β 自由) の MLE 推定に使う。
        # β=0.78 でブレンド済みの bet_tables.win からは fundamental を復元できないため
        # ここに生値を残す (2026-06-10〜)。
        "win_probs_model": ({str(k): float(v) for k, v in probs_t.win.items()}
                            if probs_t is not None and getattr(probs_t, "win", None) else None),
        # 市場アンカー型クロスプール EV 候補 (Dr.Z 系ペーパー計測, shade込み px_o≥1.0 のみ)。
        "market_anchor_ev": market_anchor_ev,
        # late-money momentum (paper 計測のみ): score/poll 段 → 現在の単勝オッズ比 r=bet/score。
        # 検証は scripts/backtest_momentum.py。null = timeline に基準行なし。
        "late_money": late_money,
        # 回収優先 bet_tables (3連単 を含む全 7 券種、各 P×O 降順 top 30)
        "bet_tables": bet_tables_serial,
        "bet_tables_g": (
            _serialize_bet_tables_g(bet_tables, aptitude_top_horses)
            if bet_tables and aptitude_top_horses else {}
        ),
        "aptitude_top_horses": list(aptitude_top_horses or []),
        # 「Claude 総合オススメ」= 全 bet type 横断の joint Kelly 最適まとめ買い束 (回収優先, 実弾)。
        "recommended_bundle": recommended_bundle,
        # 3連単的中モード (全力フォーメーション)= 3連単のみ・市場無視・EV/トリガミ無しの model 的中確率 top-K 束。
        # recommended_bundle (EV駆動) と完全分離。covered_prob=理論的中率(model基準・過信禁物)。
        "recommended_bundle_t": recommended_bundle_t,
        "trifecta_keys": ([l["key"] for l in recommended_bundle_t["legs"]]
                        if recommended_bundle_t else []),
        "trifecta_params": {
            "head_max": trifecta_head_max, "head_gap": trifecta_head_gap,
            "mid": trifecta_mid, "tail": trifecta_tail,
            "avoid_torigami": (not trifecta_no_torigami), "bankroll": trifecta_bankroll,
            # 市場無視を保証: 3連単束は market_blend=0 の model-only probs を使用 (review 指摘対応)。
            # probs_t 未指定のフォールバック時は保証が無いので null を記録 (虚偽痕跡の防止)。
            "market_blend": (0.0 if probs_t is not None else None), "rank_by": "claude_index",
            "mode": "hit",
        },
        # 2段パイプライン: Claude 考察由来の各馬指数を model fundamental に合成した痕跡。
        # llm_win_index=null は score 未完了/未実施 (= モデルのみ) のフォールバックを意味する。
        # llm_win_index: scale="strength" なら 0-100 指数、"prob" なら推定勝率 %。
        "llm_win_index": ({str(k): v for k, v in llm_win_index.items()}
                          if llm_win_index else None),
        "llm_support": ({str(k): v for k, v in llm_support.items()}
                        if llm_support else None),
        # 各馬の直前/軟情報フラグ (取消/馬体重増減/前走不利/厩舎勝負気配 等)。記録/表示用 (確率には未使用)。
        "llm_alerts": ({str(k): list(v) for k, v in llm_alerts.items()}
                       if llm_alerts else None),
        "llm_scale": llm_scale,
        "llm_blend": llm_blend,
        "llm_scored_at": llm_scored_at,
        "llm_fallback": llm_win_index is None,
        # v2 速度図表 (実データ par+pace+trip) を LightGBM fundamental と並列合成した重みと、
        # 各馬の図表値 (best/wavg/pace/trip/n_runs)。speed_v2_blend=0/None なら未使用。
        "speed_v2_blend": speed_v2_blend,
        "speed_v2_chart": (
            {str(k): v for k, v in _speed_chart_mod.horse_charts(rd.race.horses).items()}
            if speed_v2_blend else None
        ),
        # 市場指数 (= 100 / 単勝オッズ、1.0倍で100、Claude 独立)。
        "market_win_index": ({str(k): v for k, v in _market_win_index(rd).items()}
                             or None),
        # Claude 指数 × 市場指数 を per-horse で併記した表 (差 = Claude − 市場、support=補強件数、
        # alerts=直前/軟情報フラグ)。
        "index_compare": _build_index_compare(rd, llm_win_index, llm_support, llm_alerts, llm_evidence),
    }
    out = ROOT / "data" / "predictions" / f"{race_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # tmp+replace のアトミック書込: snapshot は投票 daemon (_legs_from_snapshot) と
    # 並行 dispatch (bet_scheduler × watch tick) から同時に読み書きされ得る。
    # 非アトミック write_text だと torn JSON を読んだ daemon が当該レースを
    # terminal (.done) にして賭け逃しになる (2026-06-10 bughunt)。
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
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


def _refresh_and_reevaluate(
    *,
    url: str,
    rd_old,
    rows_old,
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
    market_blend: float = ev_mod.MARKET_BLEND_LIVE,
    market_floor: float = 0.01,
    speed_v2_blend: float = ev_mod.SPEED_V2_BLEND_LIVE,
    hit_points: int = 3,
    hit_budget_ratio: float = 0.2,
    aptitude_top: int = 6,
    with_exacta: bool = False,
    with_trio: bool = False,
    llm_blend: float = ev_mod.LLM_BLEND_DEFAULT,
    probs_file: Optional[Path] = None,
    phase: str = "bet",
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
    # Claude 指数キャッシュ + --probs YAML を本経路と同様に合成する (2026-06-11 第5R:
    # 旧実装はどちらも読まず、最終 (締切直前) の再評価が指数なし・rank_source=model に
    # 退化した snapshot で上書きしていた)。
    llm_index2, llm_support2, llm_scale2, llm_scored_at2, llm_alerts2, llm_evidence2 = _load_llm_scores(race_id)
    probs2 = ev_mod.estimate_probs(rd2, market_blend=market_blend, market_floor=market_floor,
                                   speed_v2_blend=speed_v2_blend,
                                   llm_win_index=llm_index2, llm_blend=llm_blend,
                                   llm_support=llm_support2, llm_scale=llm_scale2)
    probs2 = ev_mod.load_probs(str(probs_file) if probs_file else None, probs2)
    # 3連単束は市場無視を保証するため market-free probs を別途用意 (market_blend>0 時のみ再計算)。
    probs2_t = probs2 if market_blend == 0 else ev_mod.estimate_probs(
        rd2, market_blend=0.0, market_floor=market_floor, speed_v2_blend=speed_v2_blend,
        llm_win_index=llm_index2, llm_blend=llm_blend,
        llm_support=llm_support2, llm_scale=llm_scale2)
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
            llm_win_index=llm_index2, llm_blend=llm_blend, llm_scored_at=llm_scored_at2,
            llm_support=llm_support2, llm_scale=llm_scale2, llm_alerts=llm_alerts2,
            llm_evidence=llm_evidence2,
            speed_v2_blend=speed_v2_blend, probs_t=probs2_t,
            # phase=score の refresh は暫定 (stage="score") のまま保存 → 計測対象外を維持。
            # phase=bet (CLI フル analyze) は従来どおり stage="bet" 確定で上書き
            # (claude_trifecta_select は既定 False のまま = 従来の refresh 挙動を変えない)。
            stage=phase,
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
    # 旧: refresh 時の「総合オススメ束への web 検索補強」(回収優先AI) は撤去 (2026-06-06)。
    # Claude は score ステージ指数 + 3連単買い目選定に特化 (買い目は snapshot 保存時に決まる)。
    _ = (no_llm, model)


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
