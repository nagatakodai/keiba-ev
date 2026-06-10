"""claude CLI を spawn する LLM パイプライン (競馬版, 3連単的中モード特化)。

役割は2つ: ①score ステージ = 各馬の強さ指数 (0-100) を web 検索補強つきで出す
(`score_horses_stream`) ②bet ステージ = 締切直前の 3連単買い目選定 (`select_trifecta_stream`)。
旧「回収優先AI」(EV束の picks/cuts 選定 = select_bundle_stream / evaluate_stream 系) は
2026-06-06 に撤去した。stream-json でイベントを受け取り、ツール呼び出しや進捗を
リアルタイム表示できる。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from .models import RaceData

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


def _finalize_claude_proc(proc, timer, timed_out, err_file=None) -> list[tuple[str, Any]]:
    """read loop 後の後始末 (watchdog 解除 + reap)。yield すべき error 一覧を返す。

    err_file は _spawn_claude が stderr を書いている一時ファイル (無ければ旧来の
    proc.stderr PIPE を読む)。異常終了時のみ末尾 ~600 文字を読み、必ず close する。
    """
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
        err = ""
        if err_file is not None:
            try:
                err_file.seek(0, 2)
                size = err_file.tell()
                err_file.seek(max(0, size - 600))
                err = err_file.read()[:600]
            except Exception:  # noqa: BLE001
                err = ""
        elif proc.stderr:
            err = (proc.stderr.read() or "")[:600]
        if err:
            errs.append(("error", f"claude exit {proc.returncode}: {err}"))
    if err_file is not None:
        try:
            err_file.close()
        except Exception:  # noqa: BLE001
            pass
    return errs


def _spawn_claude(cmd: list[str]) -> tuple["subprocess.Popen", Any]:
    """claude subprocess を **stderr=一時ファイル** で起動する。(proc, err_file) を返す。

    stderr=PIPE だと streaming 中に誰も drain しないため、CLI/MCP のログが pipe
    buffer (64KB) を超えた時点で child=stderr write ブロック / parent=stdout read
    ブロックの deadlock になり、kill timer (score 段は 15 分) 発火まで全ブロックして
    **完成済みの score 結果ごと喪失**していた (2026-06-10 bughunt 実再現)。
    一時ファイルなら無制限に書けて deadlock しない。エラー時は
    _finalize_claude_proc(err_file=) が末尾を読み、正常時もそこで close される。
    """
    err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=err_f,
        text=True, bufsize=1, env=_claude_env(),
    )
    return proc, err_f


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
        "**並列実行 (重要・速度に直結)**: 互いに依存しない検索・WebFetch は **必ず 1 ターンで同時に "
        "(parallel に複数 tool_use を) 呼び出す**こと。馬ごとに 1 つずつ順番 (sequential) に投げると "
        "1 ラウンド 30-50s × 頭数で timeout する。例: 全馬の近走を一度にまとめて並列発行 → 結果が揃って "
        "から、人気・評価が割れる馬だけ次の 1 バッチを並列で深掘りする。**2-3 バッチで全馬を調べ切る**のが目安。",
        "**時間厳守**: これは締切直前の処理。深追いして timeout すると指数が**丸ごと失われる** "
        "(部分結果も残らない) ので、予算を超えそうなら手元の根拠で各馬の指数を確定し、**必ず最後まで "
        "JSON を出力する**こと。完璧な調査より「全馬の指数を時間内に出し切る」を優先。1 つの "
        "WebFetch/検索が重いと感じたら打ち切って次へ進む。",
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
    timeout: int = 900,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
) -> Iterator[tuple[str, Any]]:
    """各馬の強さ指数を web 検索付きで出させる stream-json (spawn/event 形式は
    select_trifecta_stream と共通)。出力は parse_horse_scores で {scores,...} に正規化する。
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
    proc, _err_f = _spawn_claude(cmd)
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
        for ev in _finalize_claude_proc(proc, timer, timed_out, err_file=_err_f):
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
    # `or {}` は falsy しか救済しない — LLM が support を list 等の非 dict で返すと
    # .items() で AttributeError になり score dispatch 全体が死ぬ (2026-06-10 bughunt)。
    # scores/notes/alerts と同じく isinstance ガードで「壊れた出力は raise しない」契約を守る。
    sup_raw = raw.get("support")
    if isinstance(sup_raw, dict):
        for k, v in sup_raw.items():
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


# 締切1分前の高速 3連単選定では web 検索もファイル読みも一切させない (純粋推論で ~10-30s)。
_TRIFECTA_SELECT_DISALLOWED = (
    DISALLOWED_TOOLS + ",Read,WebFetch,"
    "mcp__brave-search__brave_web_search,mcp__brave-search__brave_local_search,"
    "mcp__tavily__tavily_search,mcp__tavily__tavily-search,"
    "mcp__tavily__tavily_extract,mcp__tavily__tavily-extract"
)


def build_trifecta_select_prompt(
    rd: RaceData, *, llm_index: dict[int, float] | None,
    aptitudes: dict[int, Any] | None = None,
    bankroll: int = 10_000,
    max_points: int = 48,
    mode: str = "hit",
    exclude_head: int | None = None,
) -> str:
    """締切直前の 3連単買い目選定 prompt (検索なし・Claude 指数上位から自由構築)。

    mode="hit" (全力的中) / mode="recovery" (回収=穴狙い)。各馬の **Claude 指数 (score 段で
    web 研究済)** + 適性総合のみを提示し、指数上位を本命に 1着→2着→3着 のフォーメーションを
    **自由に構築**させる。出力は買い目 (ordered triple) の配列のみ。トリガミ防止・配分は後段
    (build_trifecta_from_keys) が行う。

    **市場は一切見せない**: 両モードとも「市場無視」が本質なので、単勝オッズ・人気はプロンプトに
    含めない (含めると Claude が市場に引きずられて指数選定が崩れる)。並べ替えも指数→適性→馬番で
    市場フリーにする。購入は **1レース予算 (bankroll) 内に収める**よう点数を絞らせる。

    **回収モードの唯一の例外 (exclude_head)**: 市場1番人気の馬番だけは harness 側でゲート判定
    (Claude 指数 ≤ 90 なら 1着除外) され、ここに渡される。プロンプトには「この馬を1着に置かない」
    という指示としてのみ現れる — それ以外の市場情報 (オッズ・人気順) は一切渡さない
    (ユーザ指示 2026-06-07: 市場は1番人気を1着に入れるか否かの判定のみに使う)。
    """
    r = rd.race
    idx = llm_index or {}
    apt = aptitudes or {}
    horses = [h for h in r.horses if not getattr(h, "absent", False)]
    recovery = mode == "recovery"
    mode_label = "回収 (穴狙い)" if recovery else "全力的中"

    def _apt_total(n: int) -> float:
        a = apt.get(n)
        return float(a.total) if a is not None and hasattr(a, "total") else -1.0

    # 並べ替えは指数降順 → 適性降順 → 馬番昇順 (市場を一切使わない tiebreaker)。
    ranked = sorted(horses, key=lambda h: (idx.get(h.number, -1.0),
                                           _apt_total(h.number), -h.number), reverse=True)
    venue = getattr(r, "venue_name", "") or ""
    dist = getattr(r, "distance", None)
    surf = getattr(r, "surface", "") or ""
    rno = getattr(r, "race_number", "") or ""
    lines = [
        f"# {venue} {rno}R 3連単「{mode_label}」買い目選定 ({dist}m {surf})",
        "あなたは競馬の3連単買い目を組むエキスパート。**各馬の強さ指数 (score 段で web 研究済)** と "
        "適性総合を見て、**指数上位を本命に 1着→2着→3着 のフォーメーション (買い目) を自由に構築**する。",
        "**検索はしない** (締切直前・指数は研究済)。",
        "**重要: このモードは『市場無視』。単勝オッズ・人気・市場の評価は一切与えていないし、"
        "推測もしないこと。あくまで下の Claude 指数 (と適性) だけで強さ順を判断して買い目を組む。**",
    ]
    if recovery:
        lines.append(
            "**回収モード (穴狙い)**: 的中時の払戻 (回収) を重視する。市場の人気で序列を推測せず、"
            "Claude 指数だけで強い馬を選ぶ — 指数と市場の乖離がそのまま妙味になる。"
        )
        if exclude_head is not None:
            ex_name = next((getattr(h, "name", "") for h in horses
                            if getattr(h, "number", None) == exclude_head), "")
            ex_label = f"馬 {exclude_head}" + (f" ({ex_name})" if ex_name else "")
            lines.append(
                f"**【1着除外ルール】{ex_label} は市場1番人気のため、どの買い目でも 1着に置かない** "
                f"(keys の先頭 ≠ {exclude_head})。2着・3着には置いてよい。"
                "これ以外の市場情報は一切与えていない (推測もしない)。"
            )
    lines += [
        "",
        "## 各馬 (Claude 指数降順)",
        "| 馬番 | 馬名 | 指数(0-100) | 適性総合 |",
        "|---|---|---|---|",
    ]
    for h in ranked:
        n = h.number
        a = apt.get(n)
        atot = f"{a.total:.0f}" if a is not None and hasattr(a, "total") else "-"
        lines.append(f"| {n} | {getattr(h, 'name', '')} | "
                     f"{idx.get(n, 0):.0f} | {atot} |")
    head_rule = (
        f"- **1着** は指数最上位を 1〜2 頭に**絞る** (指数が拮抗するなら2頭)。"
        + (f"1着除外ルールの馬 ({exclude_head}) は1着候補から外す。"
           if recovery and exclude_head is not None else "")
    )
    lines += [
        "",
        f"## 組み方 ({mode_label}モード)",
        head_rule,
        "- **2着** は中くらい (指数上位 3〜5 頭程度)。**3着** は広めに取る (上位 5〜8 頭程度・"
        "3着づけは妙味も拾う)。head ⊆ mid ⊆ tail に拘らず、指数で妥当な馬を各列に置いてよい。",
        f"- **このレースの購入予算は ¥{bankroll:,}**。買い目の合計購入額が**この予算内に収まる**よう"
        f"点数を絞ること (1点あたり最低 ¥100・100円単位、トリガミ防止後に予算内)。予算 ÷ 100 が"
        f"買える点数の概算上限 (≈{max(1, bankroll // 100)} 点) だが、薄い目を無理に足さず"
        + ("「的中したときの回収の大きさ」と当たりやすさのバランスを取る点数に抑える。"
           if recovery else "「当たりやすさ」を最大化する点数に抑える。"),
        f"- **総点数の上限は {max_points} 点**。予算と上限の小さい方を超えない。"
        "薄すぎる(指数下位どうしの)目は入れない。",
        "- 取消・極端に指数の低い馬は外す。指数 0 の馬は買い目に入れない。",
        "",
        "## 出力 (買い目のみ・JSON)",
        "考察は短く。最後に必ず以下を ```json ... ``` で出力する。keys は [1着,2着,3着] の配列:",
        "```json",
        '{"keys": [[7,2,11],[7,11,2],[2,7,11]],'
        ' "formation": "1×4×6", "summary": "指数1位7番を1着固定、…",'
        ' "confidence": "high|mid|low"}',
        "```",
    ]
    return "\n".join(lines)


def select_trifecta_stream(
    rd: RaceData, *,
    llm_index: dict[int, float] | None,
    aptitudes: dict[int, Any] | None = None,
    bankroll: int = 10_000,
    max_points: int = 48,
    model: str = "opus",
    timeout: int = 75,
    mode: str = "hit",
    exclude_head: int | None = None,
) -> Iterator[tuple[str, Any]]:
    """締切直前の高速 3連単選定 stream-json。**web 検索なし** (純粋推論で ~10-30s)。

    市場 (単勝オッズ・人気) はプロンプトに含めず Claude 指数のみで選定させる (両モード=市場無視)。
    mode="recovery" (回収=穴狙い) では exclude_head (市場1番人気, 指数ゲート通過済) を
    1着に置かない指示が加わる — 市場情報はこの除外指示のみ。
    bankroll は 1レース購入予算で、合計購入額をこの予算内に収めるよう点数を絞らせる。
    出力は parse_trifecta_selection で {keys, formation, summary, confidence} に正規化する。
    """
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return
    horses = [h for h in rd.race.horses if not getattr(h, "absent", False)]
    if not horses:
        yield ("result", "")
        return
    prompt = build_trifecta_select_prompt(
        rd, llm_index=llm_index, aptitudes=aptitudes,
        bankroll=bankroll, max_points=max_points,
        mode=mode, exclude_head=exclude_head,
    )
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--effort", "high",        # 検索なしの純粋推論なので max でなく high で十分高速
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "",                       # ツール一切なし = 検索しない・高速
        "--disallowedTools", _TRIFECTA_SELECT_DISALLOWED,
    ]
    proc, _err_f = _spawn_claude(cmd)
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
                    if block.get("type") == "text" and block.get("text"):
                        yield ("text", block["text"])
            elif etype == "result":
                yield ("result", ev.get("result", "") or "")
            elif etype == "error":
                yield ("error", ev.get("message", "unknown error"))
    except Exception as e:  # noqa: BLE001
        yield ("error", f"stream parse error: {e}")
    finally:
        for ev in _finalize_claude_proc(proc, timer, timed_out, err_file=_err_f):
            yield ev


def parse_trifecta_selection(text: str) -> dict:
    """3連単選定出力の JSON を {keys:[[a,b,c]...], formation, summary, confidence} に正規化。

    壊れた/空出力は keys 空で返す (raise しない)。keys は相異3整数の triple のみ採用。
    """
    out = {"keys": [], "formation": "", "summary": "", "confidence": ""}
    raw = parse_evidence(text)   # ```json``` 抽出を流用
    if not isinstance(raw, dict):
        return out
    keys: list[list[int]] = []
    for k in (raw.get("keys") or []):
        if not isinstance(k, (list, tuple)) or len(k) != 3:
            continue
        try:
            a, b, c = int(k[0]), int(k[1]), int(k[2])
        except (TypeError, ValueError):
            continue
        if len({a, b, c}) != 3:
            continue
        keys.append([a, b, c])
    out["keys"] = keys
    out["formation"] = str(raw.get("formation", ""))
    out["summary"] = str(raw.get("summary", ""))
    out["confidence"] = str(raw.get("confidence", ""))
    return out
