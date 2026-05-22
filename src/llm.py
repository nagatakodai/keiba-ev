"""claude CLI を spawn して P×O 表を framework に照らして評価させる (競馬版)。

stream-json でイベントを受け取り、ツール呼び出しや進捗をリアルタイム表示できる。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

from .ev import PXO_FLOOR, plan_balanced, plan_max_ev, plan_wide
from .models import EvRow, Probabilities, RaceData

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_TOOLS = [
    "mcp__brave-search__brave_web_search",
    "mcp__brave-search__brave_local_search",
    "mcp__tavily__tavily_search",
    "mcp__tavily__tavily-search",
    "mcp__tavily__tavily_extract",
    "mcp__tavily__tavily-extract",
    "WebFetch",
]
DISALLOWED_TOOLS = "Bash,Edit,Write,Glob,Grep,Agent,TaskCreate,TaskUpdate,TaskList,NotebookEdit"


def is_available() -> bool:
    return shutil.which("claude") is not None


def parse_evidence(text: str) -> dict:
    """LLM 出力末尾の ```json ... ``` ブロックから evidence dict を抽出。"""
    import re
    if not text:
        return {}
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not matches:
        return {}
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return {}


def evaluate_stream(
    rd: RaceData,
    rows: list[EvRow],
    *,
    model: str = "opus",
    timeout: int = 420,
    ev_max: float | None = None,
    min_prob: float | None = None,
    probs: Probabilities | None = None,
    aptitudes: dict[int, Any] | None = None,
    aptitude_top_horses: list[int] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
) -> Iterator[tuple[str, Any]]:
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return

    prompt = build_prompt(
        rd, rows,
        ev_max=ev_max, min_prob=min_prob, probs=probs,
        aptitudes=aptitudes, aptitude_top_horses=aptitude_top_horses,
        market_signals=market_signals, horse_best_times=horse_best_times,
    )
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", DISALLOWED_TOOLS,
    ]

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None

    final_text = ""
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "system" and ev.get("subtype") == "init":
                yield ("init", {
                    "mcp_servers": ev.get("mcp_servers", []),
                    "tools": ev.get("tools", []),
                })
            elif etype == "assistant":
                for block in ev.get("message", {}).get("content", []) or []:
                    bt = block.get("type")
                    if bt == "tool_use":
                        yield ("tool_use", {
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })
                    elif bt == "text" and block.get("text"):
                        yield ("text", block["text"])
            elif etype == "result":
                final_text = ev.get("result", "") or ""
                yield ("result", final_text)
            elif etype == "error":
                yield ("error", ev.get("message", "unknown error"))
    except Exception as e:
        yield ("error", f"stream parse error: {e}")
    finally:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            yield ("error", "claude timeout")
        if proc.returncode not in (0, None):
            err = (proc.stderr.read() if proc.stderr else "")[:600]
            if err:
                yield ("error", f"claude exit {proc.returncode}: {err}")


def evaluate(
    rd: RaceData,
    rows: list[EvRow],
    *,
    model: str = "opus",
    timeout: int = 420,
    ev_max: float | None = None,
    min_prob: float | None = None,
    probs: Probabilities | None = None,
    aptitudes: dict[int, Any] | None = None,
    aptitude_top_horses: list[int] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
) -> str:
    final = ""
    for etype, payload in evaluate_stream(
        rd, rows, model=model, timeout=timeout,
        ev_max=ev_max, min_prob=min_prob, probs=probs,
        aptitudes=aptitudes, aptitude_top_horses=aptitude_top_horses,
        market_signals=market_signals, horse_best_times=horse_best_times,
    ):
        if etype == "result":
            final = payload
        elif etype == "error":
            return f"[error] {payload}"
    return final.strip()


def build_refresh_prompt(
    rd: RaceData,
    rows: list[EvRow],
    rows_old: list[EvRow],
    initial_summary: str,
    *,
    ev_max: float | None = None,
    min_prob: float | None = None,
    probs: Probabilities | None = None,
    aptitudes: dict[int, Any] | None = None,
    aptitude_top_horses: list[int] | None = None,
) -> str:
    """締切直前用の短い再評価プロンプト。"""
    r = rd.race

    by_old = {r_.key: r_ for r_ in rows_old}
    diff_lines: list[str] = []
    for r_ in rows[:30]:
        old = by_old.get(r_.key)
        if old and abs(r_.px_o - old.px_o) >= 0.3:
            sign = "+" if r_.px_o > old.px_o else ""
            diff_lines.append(
                f"  {r_.key[0]}-{r_.key[1]}-{r_.key[2]}: "
                f"O {old.odds:.1f}→{r_.odds:.1f}, P×O {old.px_o:.2f}→{r_.px_o:.2f} ({sign}{r_.px_o - old.px_o:.2f})"
            )
    diff_block = "\n".join(diff_lines) if diff_lines else "  (有意な変動なし)"

    top_lines = []
    for r_ in rows[:20]:
        top_lines.append(
            f"  {r_.key[0]}-{r_.key[1]}-{r_.key[2]}: "
            f"O={r_.odds:.1f} 人気{r_.popularity} P×O={r_.px_o:.2f}"
        )
    top_block = "\n".join(top_lines)

    initial_short = (initial_summary or "").strip()[:1800] or "(初回 Claude 評価は実行されませんでした)"
    caps_block = _caps_block(ev_max, min_prob)
    aptitude_block = _aptitude_block(rd, aptitudes) if aptitudes else ""
    aptitude_section = (
        f"\n## 各馬の適性指数 (refresh 時点)\n{aptitude_block}\n"
        + (
            f"\n**Plan G の適性ゲート集合: {', '.join(str(n) for n in aptitude_top_horses)}**\n"
            if aptitude_top_horses else ""
        )
        if aptitude_block else ""
    )
    index_block = _horse_index_block(rd, probs) if probs else ""
    index_section = f"\n## 各馬のツール推定指数 (0-100 / 同レース内相対)\n{index_block}\n" if index_block else ""
    weather_section = _weather_block(rd)
    predictions_section = _predictions_block(rd)

    return f"""**締切 5 分前の最終確認** ({r.venue_name} {r.schedule_index}日目 {r.race_number}R)

オッズが更新されました。初回分析からの変動を踏まえ、**最終 Plan を確定**してください。
{weather_section}{caps_block}{aptitude_section}{index_section}{predictions_section}
## 最新 P×O 上位 20 件
{top_block}

## 初回からの主な変動 (|Δ P×O| ≥ 0.3)
{diff_block}

## 初回の Plan/評価 (参考、抜粋)
{initial_short}

## 締切直前の依頼

時間が切迫しています。手早く以下を出してください:

1. **検索は最大 3 クエリのみ**。優先順位:
   - 取消・除外・乗り替わりの最終確認 ("<開催名> {r.race_number}R 出走取消")
   - 馬場状態の最新 (既に補強済みなら省略)
   - パドック気配など直前情報が必要な馬だけ
2. **各馬の最終指数 (短縮版)**: 初回評価から変化があった馬のみ、`馬番: 1着/2着/3着/総合 (Δ説明)` の形式で 1 行ずつ。
3. **初回 Plan から外す目** と **追加すべき目** を理由 1 行で。オッズ低下で EV<1 になった目は必ず外す。
4. **最終 Plan (表)**: 車券 / 最新 P×O / 補強根拠 / 配分点数 / 配分額 (¥10,000 を割り振る)。
5. EV ≤ 1 ならスキップを明示。
6. 短く。前置き不要。

### 構造化出力 (必須)

最後に必ず以下の JSON を ```json ... ``` で出力。

```json
{{
  "evidence_by_key": {{"1-7-4": {{"count": 3, "reasons": ["..."]}}}},
  "cuts": [],
  "final_plan": {{"a": ["1-7-4"], "b": [...], "c": [...]}}
}}
```
"""


def evaluate_refresh_stream(
    rd: RaceData,
    rows: list[EvRow],
    rows_old: list[EvRow],
    initial_summary: str,
    *,
    model: str = "opus",
    timeout: int = 240,
    ev_max: float | None = None,
    min_prob: float | None = None,
    probs: Probabilities | None = None,
    aptitudes: dict[int, Any] | None = None,
    aptitude_top_horses: list[int] | None = None,
) -> Iterator[tuple[str, Any]]:
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return

    prompt = build_refresh_prompt(
        rd, rows, rows_old, initial_summary,
        ev_max=ev_max, min_prob=min_prob, probs=probs,
        aptitudes=aptitudes, aptitude_top_horses=aptitude_top_horses,
    )
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", DISALLOWED_TOOLS,
    ]

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None

    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "system" and ev.get("subtype") == "init":
                yield ("init", {
                    "mcp_servers": ev.get("mcp_servers", []),
                    "tools": ev.get("tools", []),
                })
            elif etype == "assistant":
                for block in ev.get("message", {}).get("content", []) or []:
                    bt = block.get("type")
                    if bt == "tool_use":
                        yield ("tool_use", {
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })
                    elif bt == "text" and block.get("text"):
                        yield ("text", block["text"])
            elif etype == "result":
                yield ("result", ev.get("result", "") or "")
            elif etype == "error":
                yield ("error", ev.get("message", "unknown error"))
    finally:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            yield ("error", "claude timeout")


_WEATHER_LABEL = {100: "晴", 200: "曇", 300: "雨", 400: "雪", 500: "霧"}
_WIND_DIR = [
    "無", "北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
    "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西",
]


def _weather_block(rd: RaceData) -> str:
    w = rd.race.weather
    if not w:
        if rd.race.weather_text:
            return f"- 天候/馬場: {rd.race.weather_text}\n"
        return ""
    label = _WEATHER_LABEL.get(w.code, f"code={w.code}")
    wd = _WIND_DIR[w.wind_direction] if 0 <= w.wind_direction < len(_WIND_DIR) else f"dir={w.wind_direction}"
    rain = f", 降水 {w.precipitation:.1f}mm/h" if w.precipitation > 0 else ""
    track = f", 馬場 {w.track_condition}" if w.track_condition else ""
    return (
        f"- 天候(netkeiba): {label}, 気温 {w.temperature:.1f}℃, "
        f"風 {wd} {w.wind_speed:.1f}m/s{rain}{track}\n"
    )


def _interviews_block(rd: RaceData) -> str:
    rows = [h for h in rd.race.horses if h.interview_comment]
    if not rows:
        return ""
    lines = ["\n## 関係者コメント (netkeiba)"]
    for h in rows:
        c = h.interview_comment
        if len(c) > 300:
            c = c[:300] + "..."
        lines.append(f"- **{h.number} {h.name}**: {c}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _predictions_block(rd: RaceData) -> str:
    preds = rd.race.predictions
    if not preds:
        return ""
    lines = ["\n## netkeiba 予想 (市場参考)"]
    for p in preds:
        tag = "AI" if p.is_ai else "人"
        rate = f" 累計勝率{p.winning_rate}%({p.winning}/{p.total})" if p.total else ""
        keys = ", ".join(f"{k[0]}-{k[1]}-{k[2]}" for k in p.trifecta_keys[:12])
        more = f" +{len(p.trifecta_keys)-12}件" if len(p.trifecta_keys) > 12 else ""
        lines.append(f"- [{tag}] {p.name}{rate}")
        lines.append(f"  推奨3連単: {keys}{more}" if keys else "  推奨3連単: (なし)")
        if p.comment:
            lines.append(f"  コメント: {p.comment}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _market_signal_block(market_signals: dict[int, Any] | None) -> str:
    """市場乖離 (1 着型 / 3 着型) のみ Markdown で抜粋。標準・不明は省く。"""
    if not market_signals:
        return ""
    interesting = [
        s for s in market_signals.values()
        if s.interpretation in ("3着型", "1着型", "極端")
    ]
    if not interesting:
        return ""
    lines = [
        "| 馬 | 解釈 | 単勝 | 複(下限) | ratio (place/win) |",
        "|---|---|---|---|---|",
    ]
    for s in sorted(interesting, key=lambda x: (x.interpretation != "3着型", -x.place_to_win_ratio)):
        lines.append(
            f"| {s.number} | **{s.interpretation}** | "
            f"{s.win_odds:.1f} | {s.place_odds_min:.1f} | {s.place_to_win_ratio:.2f} |"
        )
    return "\n".join(lines)


def _best_times_block(horse_best_times: list[dict] | None) -> str:
    """持ち時計 (venue × distance での past best time) を Markdown 表で。"""
    if not horse_best_times:
        return ""
    lines = [
        "| 馬 | 馬名 | 持ち時計 (秒) | 経験 |",
        "|---|---|---|---|",
    ]
    for x in horse_best_times[:10]:
        lines.append(
            f"| {x['number']} | {x['name']} | {x['best_time_sec']:.1f} | {x['runs']} 走 |"
        )
    return "\n".join(lines)


def _aptitude_block(rd: RaceData, aptitudes: dict[int, Any]) -> str:
    """各馬の適性指数 (0-100 / 9 因子内訳 + 主要根拠) を Markdown 表で。

    aptitudes は AptitudeIndex dict[int, AptitudeIndex]。features.py の Layer 1 + 重賞実績
    を集約した「事前」適性指数で、検索 MCP の補強根拠を加味する前のベースライン。
    """
    if not aptitudes:
        return ""
    name_by_n = {h.number: h.name for h in rd.race.horses}
    sorted_items = sorted(aptitudes.items(), key=lambda kv: kv[1].total, reverse=True)
    lines = [
        "| 馬 | 馬名 | 総合 | 能力 | 距離 | 末脚 | コース | 馬場 | 状態 | 騎手 | ペース | 重賞 | 主要根拠 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n, ai in sorted_items:
        reasons = ", ".join(ai.reasons) if ai.reasons else "-"
        lines.append(
            f"| {n} | {name_by_n.get(n, '-')} | {ai.total:.1f} | "
            f"{ai.ability:.0f} | {ai.distance_fit:.0f} | {ai.last3f:.0f} | "
            f"{ai.surface_fit:.0f} | {ai.going_fit:.0f} | {ai.condition:.0f} | "
            f"{ai.jockey_fit:.0f} | {ai.pace_fit:.0f} | {ai.graded_record:.0f} | {reasons} |"
        )
    return "\n".join(lines)


def _horse_index_block(rd: RaceData, probs: Probabilities) -> str:
    """フレームワーク確率から各馬の 1/2/3 着指数 (0-100 / 同レース相対) を算出して表化。"""
    if not probs.win:
        return ""
    win_max = max(probs.win.values(), default=1.0) or 1.0
    p2_max = max(probs.place2.values(), default=1.0) or 1.0
    p3_max = max(probs.place3.values(), default=1.0) or 1.0

    lines = [
        "| 馬 | 馬名 | 騎手 | 性齢 | 1着指数 | 2着指数 | 3着指数 | 総合指数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in rd.race.horses:
        if h.absent:
            continue
        w = probs.win.get(h.number, 0.0)
        p2 = probs.place2.get(h.number, 0.0)
        p3 = probs.place3.get(h.number, 0.0)
        idx1 = round(w / win_max * 100) if win_max > 0 else 0
        idx2 = round(p2 / p2_max * 100) if p2_max > 0 else 0
        idx3 = round(p3 / p3_max * 100) if p3_max > 0 else 0
        idx_total = round((idx1 * 1.2 + idx2 + idx3 * 0.8) / 3)
        lines.append(
            f"| {h.number} | {h.name} | {h.jockey_name or '-'} | {h.sex_age or '-'} | "
            f"{idx1} | {idx2} | {idx3} | {idx_total} |"
        )
    return "\n".join(lines)


def _caps_block(ev_max: float | None, min_prob: float | None) -> str:
    if ev_max is None and min_prob is None:
        return ""
    bits: list[str] = []
    if ev_max is not None:
        bits.append(f"P×O ≤ {ev_max:.2f}")
    if min_prob is not None:
        bits.append(f"当選率 ≥ {min_prob:.2f}%")
    return (
        "\n## ユーザー指定のフィルタ (Plan 入り条件)\n"
        f"- {' かつ '.join(bits)} を満たす目だけを採用。\n"
        "- 既にローカル側でこの条件で絞り込んだ rows を渡している。これ以外の目を Plan に推奨しない。\n"
    )


def build_prompt(
    rd: RaceData,
    rows: list[EvRow],
    *,
    ev_max: float | None = None,
    min_prob: float | None = None,
    probs: Probabilities | None = None,
    aptitudes: dict[int, Any] | None = None,
    aptitude_top_horses: list[int] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
) -> str:
    r = rd.race

    horses_lines = []
    for h in r.horses:
        body = f" 体{h.body_weight}({h.body_weight_diff:+d}kg)" if h.body_weight else ""
        absent = " ★取消" if h.absent else ""
        horses_lines.append(
            f"  {h.number} {h.name} (枠{h.bracket} {h.sex_age or '?'} 斤{h.weight_kg:.1f} {h.jockey_name or '?'}{body} "
            f"R{h.rating:.1f} 勝{h.win_rate:.1f}% 連{h.quinella_rate:.1f}% 3連{h.trio_rate:.1f}% "
            f"純2着{h.pure_second:.1f}% 純3着{h.pure_third:.1f}% 単{h.win_odds:.1f}){absent}"
        )
    horses_block = "\n".join(horses_lines)

    top_lines = []
    for r_ in rows[:25]:
        top_lines.append(
            f"  {r_.key[0]}-{r_.key[1]}-{r_.key[2]}: "
            f"O={r_.odds:.1f} 人気{r_.popularity} P={r_.prob*100:.2f}% P×O={r_.px_o:.2f}"
        )
    top_block = "\n".join(top_lines)

    band = [x for x in rows if 51 <= x.popularity <= 150 and x.px_o >= PXO_FLOOR][:15]
    band_lines = [
        f"  {x.key[0]}-{x.key[1]}-{x.key[2]}: O={x.odds:.1f} 人気{x.popularity} P×O={x.px_o:.2f}"
        for x in band
    ]
    band_block = "\n".join(band_lines) if band_lines else "  (該当なし)"

    def _fmt_plan(picks: list[EvRow]) -> str:
        if not picks:
            return "(該当なし)"
        return ", ".join(f"{p.key[0]}-{p.key[1]}-{p.key[2]}({p.px_o:.2f})" for p in picks)

    plan_a = _fmt_plan(plan_balanced(rows))
    plan_b = _fmt_plan(plan_max_ev(rows))
    plan_c = _fmt_plan(plan_wide(rows))
    # Plan G (適性ゲート→EV足切り): aptitude_top_horses が無ければ空。
    plan_g_picks = []
    if aptitude_top_horses:
        from .ev import plan_aptitude_ev
        plan_g_picks = plan_aptitude_ev(rows, aptitude_top_horses)
    plan_g = _fmt_plan(plan_g_picks)

    caps_block = _caps_block(ev_max, min_prob)
    market_block = _market_signal_block(market_signals)
    market_section = (
        f"\n## 市場乖離 (1着型 / 3着型 のみ)\n{market_block}\n"
        "\n(ratio = 複勝 implied prob / 単勝 implied prob。**3着型 = Plan G の 2/3 着スロット候補**。"
        "1着型 = Plan G の 1 着スロット候補。検索でこの解釈を支持/反証する根拠を探してください。)\n"
        if market_block else ""
    )
    best_times_block = _best_times_block(horse_best_times)
    best_times_section = (
        f"\n## 持ち時計 (同 venue × 同距離 ±100m × 同 surface での best own_time_sec)\n{best_times_block}\n"
        "\n(同条件経験者の絶対値ベンチマーク。秒数が小さい = 速い。"
        "speed_idx と独立した「この特定の舞台での実績」シグナル。)\n"
        if best_times_block else ""
    )
    aptitude_block = _aptitude_block(rd, aptitudes) if aptitudes else ""
    aptitude_section = (
        f"\n## 各馬の適性指数 (0-100 / 同レース内相対 / 9 因子内訳)\n{aptitude_block}\n"
        "\n(因子: 能力=speed_idx / 距離=距離適性 / 末脚=上がり3F標準化 / "
        "馬場=同コース経験+show率 / 状態=間隔+馬体変動 / 騎手=継続/乗替 / "
        "ペース=脚質×想定ペース / 重賞=G1=10×finish倍率,G2=5,G3=3,L=2,OP=1。"
        "総合は重み付け平均。検索 MCP の補強根拠を加味する前の事前値です。)\n"
        + (
            f"\n**Plan G の適性ゲート集合 (top {len(aptitude_top_horses)} 頭): "
            f"{', '.join(str(n) for n in aptitude_top_horses)}**\n"
            if aptitude_top_horses else ""
        )
        if aptitude_block else ""
    )
    index_block = _horse_index_block(rd, probs) if probs else ""
    index_section = (
        f"\n## 各馬のツール推定指数 (確率モデル由来 / 0-100 相対)\n{index_block}\n"
        "\n(算出: 1着=win_prob正規化, 2着=place2正規化, 3着=place3正規化, "
        "総合=1着×1.2+2着+3着×0.8 を 3 で割る。"
        "適性指数とは独立 — こちらは Plackett-Luce 確率モデル経由の指数。)\n"
        if index_block else ""
    )
    weather_section = _weather_block(rd)
    predictions_section = _predictions_block(rd)
    interviews_section = _interviews_block(rd)

    surface = f"{r.surface}" if r.surface else ""
    direction = f"({r.direction})" if r.direction else ""

    return f"""あなたは中央競馬 (JRA) 3 連単 EV 分析のレビュアーです。CLAUDE.md の分析フレームワークに必ず従って評価してください。
{caps_block}
## レース
- {r.venue_name} {r.schedule_index}日目 {r.race_number}R / {r.race_class} {surface}{r.distance}m{direction}
- オッズ更新 unix: {r.odds_updated_at}
{weather_section}
## 出走馬
{horses_block}
{interviews_section}{aptitude_section}{best_times_section}{market_section}{index_section}{predictions_section}
## P×O 上位 25 件 (デフォルト推定)
{top_block}

## 穴帯 (人気 51-150) +EV
{band_block}

## ツール側の推奨
- Plan A (5点バランス / EV-first): {plan_a}
- Plan B (最高EV 1-3点 / EV-first): {plan_b}
- Plan C (広め / EV-first): {plan_c}
- **Plan G (適性ゲート → EV ≥ {PXO_FLOOR:.2f} 足切り / 競馬独自の当て方優先)**: {plan_g}

## あなたへの依頼

ツールは 2 系統の指数を提示しています:
1. **適性指数** (上記表): 競馬独自の 9 因子 (能力 / 距離 / 末脚 / コース / 馬場状態 / 状態 / 騎手 / ペース / 重賞) を集約した「事前」指数。**Plan G の根拠**。
2. **ツール推定指数** (Plackett-Luce 由来): 確率モデルから出た 1着 / 2着 / 3着 / 総合の指数。Plan A/B/C/H の根拠。

CLAUDE.md の **「検索 MCP の運用ルール」** に従って **適性指数と推定指数を検証 / 補正** し、Plan A/B/C/G を比較して最終提案を出してください。

### 必須手順

1. **検索フェーズ (最大 6 クエリ)**: P×O ≥ 2.0 の上位 8 候補に絡む馬 **および 適性 top 6 頭** について、以下を Brave / Tavily で調査:
   - 各馬の直近 5 走の着順詳細 (距離・馬場適性・コース実績)
   - 騎手相性 (主戦騎手 vs 乗り替わり、当該コース成績)
   - 当日の馬場状態 (高速 / 重 / 渋り) と当該馬の馬場適性
   - 厩舎調整 / パドック気配 / 馬体重変化の所感
   - 取消・除外・体調不安の有無 (絡む目を全カットする根拠)
   - **適性 top 6 のうち補強根拠が薄い馬は減点候補。逆に top 外でも補強根拠が厚ければ追加候補**

2. **各馬の最終指数を算出して提示**: ツール推定指数を基準値に、検索の補強・減点を加味して **最終 1着 / 2着 / 3着 / 総合指数 (0-100)** を全馬について出力。表形式必須:

   | 馬 | 馬名 | ツール総合 | 補正Δ | 最終1着 | 最終2着 | 最終3着 | 最終総合 | 主要根拠 (補強n件) |

   補正の指針:
   - 距離 / コース / 馬場適性が良い → 1着指数 +5〜10
   - 直近 5 走で 2-3 着率突出が確認 → 2着 / 3着指数 +10
   - 騎手が当該コース得意 → 全指数 +5
   - 馬体重大幅減 (-10kg 超) / 大幅増 (+10kg 超) → 全指数 -5
   - 取消 / 除外 / 体調不安 → 全指数 0 にしてカット明示
   - 根拠なしで指数を動かさない。

3. **補強根拠の集計**: 各買い目について、検索結果に基づき **補強根拠を数える**。

4. **Plan の構築**:
   - **コア** (補強 3 件以上) → 必ず Plan A/B に含める、点数厚め
   - **採用** (補強 2 件以上) → Plan A 候補
   - **保留** (補強 1 件のみ) → Plan C 候補
   - **却下** (補強 0 件 or 致命的マイナス) → Plan から外す
   - **Plan G の補強**: ツールの Plan G picks (適性ゲート集合内 + P×O ≥ 1.02) を見て、検索補強で支持できる目を `final_plan.g` に列挙。EV-first の Plan A/B/C と並列に「適性で選んで EV で確認」の長期戦略として位置付ける。

5. **シナリオ別の的中目**: 2-3 ケース。各シナリオで的中する車券を 1-2-3 形式で。

6. **重要判断ポイント**: オッズ変動・取消・締切前の最終チェック。

7. EV ≤ 1 ならスキップを明示。

### 出力形式

検索結果は短くまとめ、根拠は引用 URL 付きで。**最終指数テーブル** と **Plan 表** の 2 つは必ず表形式で出す。前置き不要。

### 構造化出力 (必須・ツール側で自動パースされる)

出力の **最後** に以下の JSON ブロックを必ず出してください (```json ... ``` で囲む)。
これがツール側で読まれ、`prob` への補強乗数 (3件以上=1.2x / 2件=1.1x / 1件=1.0x / 0件=0.85x) と
`cuts` での全除外に反映されます。

```json
{{
  "evidence_by_key": {{
    "1-7-4": {{"count": 3, "reasons": ["距離適性◎", "騎手当該コース得意", "馬場適性合致"]}},
    "5-7-4": {{"count": 2, "reasons": ["..."]}}
  }},
  "cuts": ["7-1-4"],
  "final_plan": {{
    "a": ["1-7-4", "5-7-4"],
    "b": ["1-7-4"],
    "c": ["1-7-4", "5-7-4", "3-2-6"],
    "g": ["1-7-4", "5-7-4"]
  }}
}}
```

- `evidence_by_key`: 検索評価した買い目それぞれの補強根拠数 (count) と根拠 (reasons)。
- `cuts`: 致命的マイナス (取消・大幅減量・体調不安) で完全除外する目。
- `final_plan.a/b/c/g`: あなたが推奨する最終 Plan A/B/C/G の車券。`g` はツール側の Plan G picks を検索補強で再評価した結果。
- 車券キーは必ず `"{{a}}-{{b}}-{{c}}"` 形式 (1-7-4 など、馬番ハイフン区切り)。
"""
