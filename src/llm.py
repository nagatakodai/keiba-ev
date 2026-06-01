"""claude CLI を spawn して P×O 表を framework に照らして評価させる (競馬版)。

stream-json でイベントを受け取り、ツール呼び出しや進捗をリアルタイム表示できる。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

from .ev import PXO_FLOOR, plan_balanced, plan_max_ev, plan_wide
from .models import EvRow, Probabilities, RaceData

ROOT = Path(__file__).resolve().parents[1]


def _start_kill_timer(proc: "subprocess.Popen", timeout: int):
    """timeout 秒で proc.kill() する watchdog を起動。

    `for line in proc.stdout` は claude がストール (出力も exit もしない) すると
    無限ブロックし、finally の proc.wait(timeout) に到達しない (= analyze/watch-auto が
    永久ハング)。timer で強制 kill して stdout に EOF を出し、loop を抜けさせる。
    返り値 (timer, timed_out)。timed_out[0] は timeout 発火フラグ。
    """
    timed_out = [False]

    def _kill() -> None:
        timed_out[0] = True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    return timer, timed_out


def _finalize_claude_proc(proc, timer, timed_out) -> list[tuple[str, Any]]:
    """read loop 後の後始末 (watchdog 解除 + reap)。yield すべき error 一覧を返す。"""
    timer.cancel()
    errs: list[tuple[str, Any]] = []
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if timed_out[0]:
        errs.append(("error", "claude timeout"))
    elif proc.returncode not in (0, None):
        err = (proc.stderr.read() if proc.stderr else "")[:600]
        if err:
            errs.append(("error", f"claude exit {proc.returncode}: {err}"))
    return errs


def _balanced_json(text: str, start: int) -> dict | None:
    """start 以降で最初に json.loads できる brace-balanced {...} を返す (無ければ None)。

    ```json フェンスが閉じていない / 閉じ } の後に散文がある ケースでも拾えるよう、
    文字列リテラル内の波括弧を無視して深さ 0 に戻る位置まで balance する。
    """
    i = text.find("{", start)
    while i != -1:
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except json.JSONDecodeError:
                        break  # この { からは無効 → 次の { を試す
        i = text.find("{", i + 1)
    return None

ALLOWED_TOOLS = [
    "mcp__brave-search__brave_web_search",
    "mcp__brave-search__brave_local_search",
    "mcp__tavily__tavily_search",
    "mcp__tavily__tavily-search",
    "mcp__tavily__tavily_extract",
    "mcp__tavily__tavily-extract",
    "WebFetch",
    # 学習データ / モデルメタ / 過去 snapshot を読めるように Read を許可
    # (data/datasets, data/models, data/predictions, data/results, etc.)
    "Read",
]
DISALLOWED_TOOLS = "Bash,Edit,Write,Glob,Grep,Agent,TaskCreate,TaskUpdate,TaskList,NotebookEdit"


def is_available() -> bool:
    return shutil.which("claude") is not None


def _claude_env() -> dict[str, str]:
    """claude CLI subprocess 用の env。ANTHROPIC_API_KEY を強制除外して
    Claude Pro/Max subscription 経由認証に倒す。
    .env / OS env に API key が残っていても claude CLI が課金 API に走らないようにする。
    """
    import os
    env = os.environ.copy()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


def parse_evidence(text: str) -> dict:
    """LLM 出力の ```json ... ``` ブロック (または JSON オブジェクト) を robust に抽出。"""
    import re
    if not text:
        return {}
    # 1) 正規の ```json {...} ``` を後ろ優先で
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    for m in reversed(matches):
        try:
            return json.loads(m)
        except json.JSONDecodeError:
            continue
    # 2) フォールバック: ```json フェンス以降 (閉じ欠落 / 散文混在) を brace-balance。
    #    フェンスが無ければ全文の最初に成立する {...} を拾う。
    fence = re.search(r"```json", text, re.IGNORECASE)
    obj = _balanced_json(text, fence.end() if fence else 0)
    return obj if obj is not None else {}


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
        # effort=max: claude CLI の推論深度を最大化 (`claude --help` で実在確認:
        # --effort <low|medium|high|xhigh|max>)。-p (print) モードでも有効。
        "--effort", "max",
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
        env=_claude_env(),
    )
    assert proc.stdout is not None
    timer, timed_out = _start_kill_timer(proc, timeout)

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
        for ev in _finalize_claude_proc(proc, timer, timed_out):
            yield ev


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


def leg_id(leg: dict) -> str:
    """bundle leg を一意識別する文字列 'bet_type:key' (例 'wide:3-7')。"""
    return f"{leg['bet_type']}:{'-'.join(str(k) for k in leg['key'])}"


def build_bundle_selection_prompt(
    rd: RaceData,
    bundle: dict,
    candidates: list[dict],
    *,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    max_candidates: int = 40,
    prioritize: str = "yield",
) -> str:
    """モデル出力 (joint Kelly 束 + 全 bet type の +EV 候補) を提示し、**web 検索で
    補強根拠を集めて**最終「買い目 (picks)」を選ばせる prompt。

    CLAUDE.md の検索 MCP 運用ルール (1レース最大6クエリ・補強根拠 3+/2/1/0 の加減点・
    Plan 入りの最終ルール) を統合。3連単単独の補強は廃止し、全 bet type 横断の総合
    オススメ束のみを検索で補強する。出力 picks (= 買う leg id 配列) + per-leg notes
    (補強根拠数+内容) + cuts + summary + confidence。
    """
    r = rd.race
    legs = bundle.get("legs", [])
    horse_name = {h.number: h.name for h in r.horses}
    is_hit_mode = (prioritize == "hit")
    # 候補の並び: 回収優先=P×O 降順 (+EV 効率) / 的中優先=想定P 降順 (EV 関係なく当て率)。
    if is_hit_mode:
        pool = sorted(candidates, key=lambda c: c.get("prob", 0.0), reverse=True)[:max_candidates]
    else:
        pool = sorted(candidates, key=lambda c: c.get("px_o", 0.0), reverse=True)[:max_candidates]
    bundle_ids = {leg_id(leg) for leg in legs}
    # 束採用 leg (★) が pool に含まれない (joint Kelly が低P×O脚を組み入れるケース) ことが
    # あり、その場合 claude に「★ で示されたが表に無い leg」が見え不整合になる。
    # candidates 中で束採用しているが pool に漏れた leg を必ず追補して整合させる。
    pool_ids = {leg_id(c) for c in pool}
    pool += [c for c in candidates if leg_id(c) in bundle_ids and leg_id(c) not in pool_ids]
    # 検索対象は ① 束(★) の全脚 ② 束外の高 P×O 候補上位 (合算で <=8 程度を目安)
    bundle_ids_list = [leg_id(leg) for leg in legs]

    # 開催日: start_at(unix) から JST 日付に変換 (検索クエリの「馬場状態 YYYYMMDD」用)。
    import datetime as _dt
    if r.start_at:
        kaisai_date = _dt.datetime.fromtimestamp(r.start_at).strftime("%Y-%m-%d")
    else:
        kaisai_date = "当日"
    mode_label = "**的中優先**" if is_hit_mode else "**回収優先**"
    mode_explain = (
        "**EV (回収率) は一切見ず**、想定P (当たりやすさ) が高い目だけを選ぶ戦略。"
        "P×O が 1 未満 (-EV) でも当て率が高ければ採用します。配分は Kelly を使わず想定P比例 "
        "(ただしトリガミ防止で収支プラスは確保)。**実弾は買いません (おまけ計測)**。"
        if is_hit_mode else
        "joint Kelly で E[log(資金)] を最大化する EV 最適 (回収率最大化) 戦略。**実弾で買う対象**。"
    )
    lines = [
        f"# レース: {r.venue_name} {r.race_number}R ({r.race_class}) "
        f"{r.surface}{r.distance}m {r.weather_text}",
        f"開催日: {kaisai_date}",
        f"選定モード: {mode_label} — {mode_explain}",
        "",
        "あなたは確率モデルの出力に **web 検索の補強根拠** を重ねて最終買い目を決める"
        f"馬券選定者です。モデルが出した「{mode_label} 束」を尊重しつつ、"
        "**Brave Search / Tavily** で各脚の馬・騎手・コース適性・取消/体調を検証し、"
        "補強根拠の量と質で picks (買う) / cuts (外す) を判断してください。"
        + (
            "\n**的中優先モードでは:** EV (P×O) は一切判断材料にしないでください。"
            "想定P (当たりやすさ) が高い脚だけを残し、補強根拠で危ない目 (取消/体調/不適性) を cuts に。"
            "P×O が低くても (=-EV でも) P が高ければ採用します。"
            if is_hit_mode else ""
        ),
        "",
        f"## モデルの{'想定P比例' if is_hit_mode else 'joint Kelly'}束 ({mode_label}) ★が束採用",
        f"投資総額 ¥{bundle.get('total_stake', 0):,} / 束の的中率(1点以上) "
        f"{bundle.get('bundle_hit_prob', 0) * 100:.1f}% / min払戻比 "
        f"{bundle.get('min_payout_ratio', 0):.2f}",
        f"束採用 leg ids: {bundle_ids_list}",
        "",
        ("## モデルの候補 (想定P 降順, 全 bet type 横断 — EV 不問)"
         if is_hit_mode else
         "## モデルの +EV 候補 (P×O 降順, 全 bet type 横断)"),
        "| id | 種別 | 買い目 | オッズ | 推定P | P×O | Kelly | 束 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    apt = aptitudes or {}
    for c in pool:
        key = list(c["key"])
        names = " / ".join(horse_name.get(n, f"#{n}") for n in key)
        kelly = (c["px_o"] - 1.0) / (c["odds"] - 1.0) if c["odds"] > 1 else 0.0
        lines.append(
            f"| {leg_id(c)} | {c['bet_type']} | {'-'.join(map(str, key))} ({names}) "
            f"| {c['odds']:.1f} | {c['prob']*100:.1f}% | {c['px_o']:.2f} | "
            f"{kelly*100:.1f}% | {'★' if leg_id(c) in bundle_ids else ''} |"
        )

    lines += ["", "## 出走馬の適性 (参考)"]
    for h in r.horses:
        a = apt.get(h.number)
        if a is not None:
            lines.append(f"- {h.number} {h.name or '?'}: 適性総合{getattr(a, 'total', 0):.0f}")

    lines += [
        "",
        "## 検索 MCP の運用ルール (CLAUDE.md 準拠)",
        "**検索対象の優先**: 束採用 leg(★) の登場馬 + 束外の高 P×O 候補(P×O≥2.0 等)。",
        "**検索すべき情報**(優先度順):",
        "  1. 馬の直近5走の着順詳細・距離適性・コース実績",
        "  2. 騎手の当該コース成績 / 主戦騎手 vs 乗り替わり",
        "  3. 当日の馬場状態 (高速 / 重 / 渋り) と当該馬の馬場適性",
        "  4. 厩舎調整 / パドック気配 / 馬体重変化",
        "  5. 取消・除外・体調不安 (絡む目を全カットする根拠)",
        "**検索すべきでないこと**: 既に上表に出ている数値 (オッズ/人気/推定P)、競馬の基本ルール、"
        "1か月以上前の汎用情報。",
        "**検索予算**: 1 レースあたり**最大 6 クエリ** (Brave + Tavily 合算)。クエリ前に必ず"
        "「この検索で何が決まるか」を 1 行で説明する。",
        "**クエリのテンプレ**: \"<馬名>\" 直近5走 / \"<馬名>\" <距離>m <芝|ダ> / "
        "\"<騎手名>\" <競馬場> 成績 / <場名> 馬場状態 <YYYYMMDD> / \"<馬名>\" 取消 OR 体調",
        "",
        "## 補強根拠による加点・減点ルール",
        "| 検索で見つかった根拠 | アクション |",
        "|---|---|",
        "| 距離 / コース / 馬場適性が良い | **+補強根拠 1** |",
        "| 直近5走で 2-3 着率突出 | **+補強根拠 1** |",
        "| 騎手が当該コース得意 | **+補強根拠 1** |",
        "| 馬体重大幅減 (-10kg超) / 大幅増 (+10kg超) | **−補強根拠 1** |",
        "| 取消 / 除外 / 体調不安 | **絡む目を全カット (必ず cuts に入れる)** |",
        "",
        "## picks/cuts の最終ルール",
        "- **コア** (補強3件以上)        → picks に必ず入れる、可能なら厚め",
        "- **採用** (補強2件)            → picks 候補 (Kelly/P×O が許せば入れる)",
        "- **保留** (補強1件のみ)        → picks 外す (cuts へ) — 確率モデルの楽観バイアス警戒",
        "- **却下** (補強0件 or 否定根拠) → cuts に入れる",
        "- **絶対却下** (取消/致命的マイナス) → 関連する全 leg を cuts に",
        "",
        "## 指示",
        "1. まず束採用 leg(★)を中心に検索で補強。検索クエリは出し惜しまず、ただし 6 本以内。",
        "2. **picks** = 実際に買う leg id 配列。**cuts** = モデル候補のうち外す leg id 配列。",
        "3. picks/cuts ともに上表の id をそのまま使う(`win:7` / `wide:2-11` / `trifecta:1-2-3` 形式)。",
        "4. picks=[] (空配列) は「明示的な見送り(賭けない)」を意味する。**確証が無い時はためらわず []**。",
        "5. notes は picks 各 leg について補強根拠の **件数 + 内容**(検索で何がわかったか)を 1-2 行で。",
        "6. 検索で決定的な否定根拠(取消等)を見つけたら必ず cuts に入れる。",
        "7. トリガミ防止の最終配分はモデルが再計算するので、stake は決めなくてよい。",
        "",
        "最後に必ず以下の JSON を ```json ... ``` で出力:",
        "```json",
        '{"picks": ["win:7", "wide:2-11"],'
        ' "cuts": ["trifecta:9-4-2"],'
        ' "notes": {"win:7": "補強3件: ①距離適性◎ ②騎手当該場勝率18% ③直近3走連対"},'
        ' "summary": "選定の総評", "confidence": "high|mid|low"}',
        "```",
    ]
    return "\n".join(lines)


def select_bundle_stream(
    rd: RaceData,
    bundle: dict,
    candidates: list[dict],
    *,
    model: str = "opus",
    timeout: int = 420,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    prioritize: str = "yield",
) -> Iterator[tuple[str, Any]]:
    """モデル出力から最終買い目を選ばせる stream-json。**web 検索を使った補強有り**
    (Brave/Tavily/WebFetch/Read を許可)。CLAUDE.md の検索ルールに従って per-leg の
    補強根拠を集め、picks/cuts/notes を返す。evaluate_stream と同じ event 形式を yield。
    """
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return
    if not bundle.get("legs"):
        yield ("result", "")  # 候補なし (見送り)
        return
    prompt = build_bundle_selection_prompt(
        rd, bundle, candidates, aptitudes=aptitudes,
        market_signals=market_signals, horse_best_times=horse_best_times,
        prioritize=prioritize,
    )
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        # effort=max: claude CLI の推論深度を最大化 (`claude --help` で実在確認:
        # --effort <low|medium|high|xhigh|max>)。-p (print) モードでも有効。
        "--effort", "max",
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        # web 検索系 (Brave/Tavily/WebFetch) + 過去 snapshot 読み込み用 Read を許可。
        # 検索ルール (1レース最大6クエリ・補強根拠 3+/2/1/0 加減点) は prompt 側で制約。
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", DISALLOWED_TOOLS,
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=_claude_env(),
    )
    assert proc.stdout is not None
    timer, timed_out = _start_kill_timer(proc, timeout)
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
            if etype == "assistant":
                for block in ev.get("message", {}).get("content", []) or []:
                    if block.get("type") == "tool_use":
                        yield ("tool_use", {"name": block.get("name", ""), "input": block.get("input", {})})
                    elif block.get("type") == "text" and block.get("text"):
                        yield ("text", block["text"])
            elif etype == "result":
                yield ("result", ev.get("result", "") or "")
            elif etype == "error":
                yield ("error", ev.get("message", "unknown error"))
    except Exception as e:  # noqa: BLE001
        yield ("error", f"stream parse error: {e}")
    finally:
        for ev in _finalize_claude_proc(proc, timer, timed_out):
            yield ev


def build_horse_score_prompt(
    rd: RaceData,
    *,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
) -> str:
    """各馬に **0-100 の強さ指数** (高い=強い=1位,2位…) を付けさせる prompt。

    2段パイプラインの score ステージで使う。web 検索で各馬の適性/状態/取消を調べ、
    確率モデルに合成する「Claude 考察由来の強さ指数」を出力させる。picks/cuts は出さない
    (買い目はモデル+joint Kelly が決める)。検索ルールは CLAUDE.md 準拠。
    """
    import datetime as _dt
    r = rd.race
    apt = aptitudes or {}
    horses = [h for h in r.horses if not h.absent]
    if r.start_at:
        kaisai_date = _dt.datetime.fromtimestamp(r.start_at).strftime("%Y-%m-%d")
    else:
        kaisai_date = "当日"
    lines = [
        f"# レース: {r.venue_name} {r.race_number}R ({r.race_class}) "
        f"{r.surface}{r.distance}m {r.weather_text}",
        f"開催日: {kaisai_date}",
        "",
        "あなたは競馬の考察家です。下の出走馬について **web 検索 (Brave/Tavily)** で "
        "各馬を調べ、**各馬の「強さ指数」 (0-100, 高いほど強い = 1着に近い)** を付けてください。"
        "これは市場とは独立した相対評価で、確率モデルの fundamental に合成され、最終的に市場オッズと"
        "ブレンドされます。**買い目 (picks) は決めません** — あなたの仕事は各馬を相対評価して"
        "数値化することだけです。",
        "",
        "## ⚠ あなたの edge = 市場に直交する情報 (最重要)",
        "**「公開プレビューを読んで強い馬を当てる」のは無価値**です — そういう情報は既に単勝オッズに"
        "織り込まれており、市場の鏡を作るだけで edge になりません。あなたが価値を生むのは"
        "**市場がまだ完全に織り込んでいない情報**です。指数を動かす根拠は、**市場が織込済の"
        "人気/オッズ**より、次の①②を最優先に重みづけしてください:",
        "",
        "**① 直前情報** (締切~1分前に確定し、市場が完全に織り込む前のもの):",
        "  - 取消・除外・競走除外・出走取消 (確認できたら指数 0)",
        "  - **当日馬体重とその増減** (大幅増減 ±10kg超 はマイナス材料。発表は直前)",
        "  - 馬場状態の急変・公式発表 (急な雨/開催中の悪化/高速化/含水率) と当該馬の馬場適性",
        "  - パドック気配・返し馬の所感 (落ち着き/イレ込み/歩様/毛艶)",
        "  - 厩舎・騎手の直前コメント、乗り替わり・主戦回避の確定",
        "**② 軟情報の構造化** (数値モデルが取りこぼす、言葉で書かれた情報):",
        "  - **前走の不利メモ** (出遅れ/包まれた/詰まった/不利を受けた/直線で進路無し → 着順以上に強い)",
        "  - **厩舎の勝負気配** (遠征・輸送・叩き2走目で上昇・乗替りで強化・調教抜群・前哨戦から本番)",
        "  - **展開の言語的予想** (ハナ争い/逃げ馬複数で速くなる/隊列/極端なハイ or スローペース) と"
        " 各馬の脚質がその展開に噛み合うか",
        "",
        "数値モデル (適性総合・LightGBM) と市場 (オッズ) は別途ブレンドされるので、**あなたは"
        "それらに無い ①② の上乗せ/差し引きに集中**してください。①② で確たる根拠が掴めない馬は、"
        "適性総合・オッズの常識水準に留め、無理に動かさない (= 楽観バイアスを避ける)。",
        "",
        "## 出走馬 (馬番 / 馬名 / 性齢 / 騎手 / 馬体重(増減) / 適性総合 / 単勝オッズ)",
        "| 馬番 | 馬名 | 性齢 | 騎手 | 馬体重 | 適性 | 単勝 |",
        "|---|---|---|---|---|---|---|",
    ]
    for h in horses:
        a = apt.get(h.number)
        atot = f"{getattr(a, 'total', 0):.0f}" if a is not None else "-"
        wo = f"{h.win_odds:.1f}" if getattr(h, "win_odds", 0) else "-"
        sa = getattr(h, "sex_age", "") or "-"
        jk = getattr(h, "jockey_name", "") or "-"
        bw = getattr(h, "body_weight", 0)
        bwd = getattr(h, "body_weight_diff", 0)
        bws = f"{bw}({bwd:+d})" if bw else "-"
        lines.append(f"| {h.number} | {h.name or '?'} | {sa} | {jk} | {bws} | {atot} | {wo} |")
    lines += [
        "",
        "## 検索 MCP の運用ルール (CLAUDE.md 準拠)",
        "**検索すべき情報 (優先度順 — 上の ①直前情報 / ②軟情報 を最優先)**: ",
        "  1. 取消・除外・出走取消、当日馬体重(増減)、馬場の急変発表 (= ①直前情報、最優先)",
        "  2. 前走の不利メモ・厩舎の勝負気配・パドック/返し馬気配・展開の言語予想 (= ②軟情報)",
        "  3. 直近5走の着順詳細・距離/コース適性 (公開済で市場に入りやすいが波形確認に使う)",
        "  4. 騎手の当該コース成績 / 乗り替わりの強化・弱体",
        "**検索すべきでない**: 既に上表にある数値 (オッズ/人気/適性)、競馬の基本ルール、"
        "1か月以上前の汎用情報、市場の鏡にしかならない「単に人気だから強い」という類の確認。",
        f"**検索予算**: このレースは {len(horses)} 頭立て。**全馬を 1 頭あたり約 2 クエリ "
        f"(合計 ~{len(horses) * 2} クエリ) まで** Brave/Tavily で補強してよい。各馬について最低 "
        "1 回は ①直前情報 (取消/馬体重) または ②軟情報 (近走の不利/勝負気配) を確認し、人気・評価が"
        "割れる馬は 2 回まで深掘りする。各クエリ前に「何が決まるか」を 1 行説明。",
        "**クエリ例 (①②を狙う)**: \"<馬名>\" 取消 OR 除外 OR 馬体重 / \"<場名>\" <YYYYMMDD> 馬場 含水率 / "
        "\"<馬名>\" 前走 不利 OR 出遅れ OR 詰まる / \"<馬名>\" 厩舎 OR 調教 OR 仕上がり / "
        "\"<開催名>\" {race_no}R 展開 OR ペース / \"<騎手名>\" <場名> 成績".replace("{race_no}", str(r.race_number)),
        "",
        "## 指数の付け方 (重要)",
        "- **0-100 の相対評価**。最も強い馬を高く (目安 ~100)、勝ち目の薄い馬を低く。市場とは "
        "独立に、各馬の力 (適性・近走・騎手・展開) を相対的に数値化する (市場暗黙率に揃えない)。",
        "- **上げる材料は ①直前情報 / ②軟情報を最重視**: 当日馬体重が良化・前走に明確な不利"
        "(本来もっと走れた)・厩舎の勝負気配・展開が脚質に有利。次いで距離/コース/馬場適性◎・直近好走。",
        "- **下げる材料**: 馬体重大幅増減 (±10kg超)・パドック/気配難・前走が展開に恵まれた幻の好走・"
        "距離/コース/馬場の不適性・近走凡走・乗替りマイナス・展開不利。人気でも弱いと判断すれば低くしてよい。",
        "- **取消/除外/重度の体調不安**が確認できた馬は指数 0 にする (絡む目をモデルが落とせるよう)。",
        "- ①②の確たる根拠が無い馬は適性総合・オッズの常識的水準に留め、過度に動かさない (楽観バイアス警戒)。",
        "",
        "## 各馬の補強根拠件数 (support)",
        "各馬について、指数を動かす **裏付けとなった検索根拠の件数** を 0-3+ で出す。"
        "**プラス材料もマイナス材料も同じく 1 件として数える** (例: 馬体重-12kg=1 / 前走不利=1 / "
        "厩舎勝負気配=1 / 距離不適性が判明=1 / 乗替りで実績薄=1)。①直前情報・②軟情報の根拠を"
        "特に重視して数える。支持が多い馬ほどモデルはあなたの指数 (上げ・下げ問わず) を厚く採用する。"
        "材料が無ければ 0。",
        "",
        "## 各馬の直前/軟情報フラグ (alerts) — 構造化して必ず出す",
        "検索で見つけた **①直前情報・②軟情報を短い日本語ラベルの配列**として馬番ごとに出す。"
        "これは将来モデルが取消馬を確率から落とす等に使う構造化フィールド。根拠が無い馬は省略 or 空配列。",
        "**ラベル例 (簡潔に、1 ラベル = 1 事実)**: ",
        '  "取消" / "除外" / "馬体重-12kg" / "馬体重+16kg" / "前走不利" / "出遅れ" / "詰まる" / '
        '"厩舎勝負気配" / "叩き2走目" / "乗替り強化" / "乗替り" / "パドック良" / "イレ込み" / '
        '"馬場渋化" / "高速馬場" / "逃げ濃厚" / "ハナ争い" / "展開有利" / "展開不利" / "距離不安"',
        "取消/除外を入れた馬は scores も 0 にすること。",
        "",
        "## 出力",
        "全出走馬の 馬番→指数(0-100) と support を必ず網羅し、alerts は該当馬のみ、"
        "最後に以下の JSON を ```json ... ``` で出力:",
        "```json",
        '{"scores": {"7": 82, "2": 64, "11": 40, "3": 0},'
        ' "support": {"7": 3, "2": 1, "11": 0, "3": 1},'
        ' "alerts": {"7": ["前走不利", "厩舎勝負気配"], "2": ["馬体重-10kg"], "3": ["取消"]},'
        ' "notes": {"7": "補強3件: 前走直線で詰まる不利/厩舎遠征の勝負気配/距離適性◎"},'
        ' "summary": "考察の総評 (直前/軟情報で市場とどこがズレるか)", "confidence": "high|mid|low"}',
        "```",
    ]
    return "\n".join(lines)


def score_horses_stream(
    rd: RaceData,
    *,
    model: str = "opus",
    timeout: int = 300,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
) -> Iterator[tuple[str, Any]]:
    """各馬の強さ指数を web 検索付きで出させる stream-json。select_bundle_stream と
    同じ spawn/event 形式。出力は parse_horse_scores で {scores,...} に正規化する。
    """
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return
    horses = [h for h in rd.race.horses if not h.absent]
    if not horses:
        yield ("result", "")
        return
    prompt = build_horse_score_prompt(
        rd, aptitudes=aptitudes, market_signals=market_signals,
        horse_best_times=horse_best_times,
    )
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        # effort=max: claude CLI の推論深度を最大化 (`claude --help` で実在確認:
        # --effort <low|medium|high|xhigh|max>)。-p (print) モードでも有効。
        "--effort", "max",
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
        text=True, bufsize=1, env=_claude_env(),
    )
    assert proc.stdout is not None
    timer, timed_out = _start_kill_timer(proc, timeout)
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
            if etype == "assistant":
                for block in ev.get("message", {}).get("content", []) or []:
                    if block.get("type") == "tool_use":
                        yield ("tool_use", {"name": block.get("name", ""), "input": block.get("input", {})})
                    elif block.get("type") == "text" and block.get("text"):
                        yield ("text", block["text"])
            elif etype == "result":
                yield ("result", ev.get("result", "") or "")
            elif etype == "error":
                yield ("error", ev.get("message", "unknown error"))
    except Exception as e:  # noqa: BLE001
        yield ("error", f"stream parse error: {e}")
    finally:
        for ev in _finalize_claude_proc(proc, timer, timed_out):
            yield ev


def _normalize_alerts(raw_alerts: Any) -> dict[int, list[str]]:
    """alerts (各馬の直前/軟情報フラグ配列) を {int: [str,...]} に正規化。

    LLM は {"3": ["取消", "馬体重-12kg"]} 形式で出す想定。値が単一文字列でもリスト化、
    空文字/None は除外、空配列の馬は dict から落とす。壊れた/無い入力は {} (空でも壊れない)。
    """
    out: dict[int, list[str]] = {}
    if not isinstance(raw_alerts, dict):
        return out
    for k, v in raw_alerts.items():
        try:
            num = int(k)
        except (ValueError, TypeError):
            continue
        if isinstance(v, str):
            items = [v]
        elif isinstance(v, (list, tuple)):
            items = list(v)
        else:
            continue
        labels = [str(x).strip() for x in items if x is not None and str(x).strip()]
        if labels:
            out[num] = labels
    return out


def parse_horse_scores(text: str) -> dict:
    """score 出力の JSON を正規化して
    {"scores":{int:float}, "support":{int:int}, "scale":str, "alerts":{int:[str]},
     "notes":{}, "summary":str, "confidence":str} で返す。壊れた/空出力は scores 空で返す
    (raise しない)。

    正規形は `scores` (各馬の 0-100 強さ指数、市場独立の相対評価) + `support` (補強根拠件数)
    + `alerts` (各馬の直前/軟情報フラグ配列、無ければ空)。scale="strength" を付け、ev 側は
    softmax(指数/T_LLM) の温度パスで確率化する。後方互換で `win_prob` (推定勝率 %) も拾い、
    その場合 scale="prob" (温度なし直接使用)。
    """
    empty = {"scores": {}, "support": {}, "scale": "strength", "alerts": {},
             "notes": {}, "summary": "", "confidence": ""}
    raw = parse_evidence(text)
    if not isinstance(raw, dict):
        return dict(empty)
    src = raw.get("scores")
    scale = "strength"
    if not isinstance(src, dict) or not src:
        wp = raw.get("win_prob")
        if isinstance(wp, dict) and wp:
            src, scale = wp, "prob"   # 後方互換 (勝率 % 形式)
        else:
            src = {}   # データ無し: 空のまま (空 scores は ev で no-op)
    scores: dict[int, float] = {}
    for k, v in src.items():
        try:
            scores[int(k)] = float(v)
        except (ValueError, TypeError):
            continue
    support: dict[int, int] = {}
    for k, v in (raw.get("support") or {}).items():
        try:
            support[int(k)] = max(0, int(float(v)))
        except (ValueError, TypeError):
            continue
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    return {
        "scores": scores,
        "support": support,
        "scale": scale,
        "alerts": _normalize_alerts(raw.get("alerts")),
        "notes": notes,
        "summary": str(raw.get("summary", "")),
        "confidence": str(raw.get("confidence", "")),
    }


def parse_bundle_review(text: str) -> dict:
    """選定出力の JSON ブロックを {picks,cuts,notes,summary,confidence} に正規化。

    新方式は picks (買う leg id 配列)。後方互換で cuts も拾う。
    """
    raw = parse_evidence(text)  # 同じ ```json``` 抽出ロジックを流用
    if not isinstance(raw, dict):
        return {}
    picks = raw.get("picks")
    cuts = raw.get("cuts")
    return {
        "picks": [str(p) for p in picks] if isinstance(picks, list) else None,
        "cuts": [str(c) for c in cuts] if isinstance(cuts, list) else [],
        "notes": raw.get("notes") if isinstance(raw.get("notes"), dict) else {},
        "summary": str(raw.get("summary", "")),
        "confidence": str(raw.get("confidence", "")),
    }


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

    training_block = _training_data_block()

    return f"""**締切 5 分前の最終確認** ({r.venue_name} {r.schedule_index}日目 {r.race_number}R)

オッズが更新されました。初回分析からの変動を踏まえ、**最終 Plan を確定**してください。
{weather_section}{caps_block}
{training_block}
{aptitude_section}{index_section}{predictions_section}
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
        # effort=max: claude CLI の推論深度を最大化 (`claude --help` で実在確認:
        # --effort <low|medium|high|xhigh|max>)。-p (print) モードでも有効。
        "--effort", "max",
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
        env=_claude_env(),
    )
    assert proc.stdout is not None
    timer, timed_out = _start_kill_timer(proc, timeout)

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
    except Exception as e:  # noqa: BLE001 - 想定外でも error として伝える
        yield ("error", f"stream parse error: {e}")
    finally:
        for ev in _finalize_claude_proc(proc, timer, timed_out):
            yield ev


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


def _training_data_block() -> str:
    """学習データ / モデルメタの要約 + Read で参照可能なファイルパスを案内。

    LLM がモデルの強み/弱み (どの特徴量に依存しているか、validation 性能、
    Phase 18-23 の holdout finding) を把握した上で補強根拠を判断できるよう、
    要点を圧縮して prompt に埋め込み、詳細は Read で取りに行ってもらう。
    """
    meta_path = ROOT / "data" / "models" / "lgbm_metadata.json"
    dataset_path = ROOT / "data" / "datasets" / "all.parquet"

    summary_lines: list[str] = []
    meta_block = ""
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            trained_at = meta.get("trained_at", "?")
            n_train = meta.get("n_train_races", "?")
            n_valid = meta.get("n_valid_races", "?")
            best_iter = meta.get("best_iteration", "?")
            best_scores = (meta.get("best_scores") or {}).get("valid") or {}
            ndcg5 = best_scores.get("ndcg@5")
            ndcg1 = best_scores.get("ndcg@1")
            feature_cols = meta.get("feature_cols") or []
            T_meta = meta.get("softmax_temperature")
            meta_block = (
                f"- 学習日時: {trained_at}\n"
                f"- 訓練 race 数: {n_train} (時系列前半)\n"
                f"- 検証 race 数: {n_valid} (時系列後半)\n"
                f"- best iteration: {best_iter}\n"
                f"- valid ndcg@5: {ndcg5:.3f} / ndcg@1: {ndcg1:.3f}\n"
                if isinstance(ndcg5, (int, float)) and isinstance(ndcg1, (int, float))
                else f"- 学習日時: {trained_at} / 訓練 {n_train} 検証 {n_valid} race\n"
            )
            meta_block += f"- 特徴量数: {len(feature_cols)}\n"
            if T_meta is not None:
                meta_block += f"- softmax temperature (model-specific): {T_meta}\n"
            top_feats = meta.get("top_features_by_gain") or []
            if top_feats:
                top5 = top_feats[:5]
                top_names = ", ".join(f["name"] for f in top5)
                meta_block += (
                    f"- 重要特徴量 top 5 (gain 順): {top_names}\n"
                    "  (モデルはこれらを最も重視。"
                    "recent_form_score = 直近 3 走の重み付け着順スコア、"
                    "last3f_idx_recent = 直近の上がり 3F 指数、"
                    "popularity_outperformance = 人気との乖離、"
                    "shrunk_show_rate = 条件付き shrinkage 3着率、"
                    "body_weight = 馬体重)\n"
                )
        except Exception:
            meta_block = "- (metadata 読込失敗)\n"

    findings = (
        "## 学習データ / モデルの背景知識 (Phase 18-23 holdout findings, n=291+149)\n\n"
        "**確認済み +EV (sliding-window でも robust):**\n"
        "- 単勝 (1 着馬予測) を β=0.78 で blend すると市場 baseline +7-8 pt の ROI 改善。\n"
        "  W3: 95.9% (vs 88.5%)、W4: 88.3% (vs 80.4%)。\n\n"
        "**Plan-level の +EV 主張は overfit と判明 (CV + sliding-window で破綻):**\n"
        "- Plan H1/H2 β=0 (in-sample 110-132% ROI に見えた) → CV mean 64% < 控除率 77.5%\n"
        "- Plan G β=1.0 (in-sample 108% ROI) → 新規 LGBM での W4 で hit 0/149\n"
        "- Plan B (最高 P×O 上位 3 点) は 0 hits / 440 races の楽観バイアスの罠\n\n"
        "**示唆:**\n"
        "- モデルの確率は **rank に意味あり** だが **絶対値の calibration は粗い**\n"
        "- 検索の補強根拠を「ツール推定指数の絶対値」より重視すべき\n"
        "- 単勝向けの 1 着馬予測が最もシャープ。3 連単は連鎖確率で誤差が積み上がる\n"
        f"\n{meta_block if meta_block else ''}"
    )

    paths_section = (
        "\n**詳しく見たいときは Read tool で以下を参照可能:**\n"
        f"- `{meta_path.relative_to(ROOT)}` — モデルメタ (特徴量一覧、ハイパラ、best_iter、ndcg@5 等)\n"
        f"- `{dataset_path.relative_to(ROOT)}` — 学習データ全体 (parquet)。"
        "race_id 12 桁の時系列順、各馬の特徴量 / win_odds / finish_pos / target_top1 を含む\n"
        "- `data/predictions/<race_id>.json` — 過去の analyze snapshot (P×O / Plan picks / aptitude)\n"
        "- `data/results/<race_id>.json` — 過去の結果 (finish_order, payout)\n"
        "- `CLAUDE.md` — 確率モデルの保守化哲学、bet-type-specific β、Plan の実証階層\n"
        "- `data/cache/aptitudes/<race_id>.json` — 過去 race の aptitude top 6 横顔\n"
        "\n注意: parquet は pandas が無い環境では schema 確認程度。"
        "主に metadata.json と CLAUDE.md と過去 snapshot を参照することを推奨。\n"
    )

    return findings + paths_section


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

    training_block = _training_data_block()

    return f"""あなたは中央競馬 (JRA) 3 連単 EV 分析のレビュアーです。CLAUDE.md の分析フレームワークに必ず従って評価してください。
{caps_block}
{training_block}

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
