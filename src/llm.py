"""claude CLI を spawn する LLM パイプライン (競馬版, 3連単的中モード特化)。

役割は2つ: ①score ステージ = 各馬の強さ指数 (0-100) を web 検索補強つきで出す
(`score_horses_stream`) ②bet ステージ = 締切直前の 3連単買い目選定 (`select_trifecta_stream`)。
旧「回収優先AI」(EV束の picks/cuts 選定 = select_bundle_stream / evaluate_stream 系) は
2026-06-06 に撤去した。stream-json でイベントを受け取り、ツール呼び出しや進捗を
リアルタイム表示できる。
"""
from __future__ import annotations

import json
import os
import queue as _queue
import shutil
import subprocess
import tempfile
import threading
import time
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


def _fmt_past_run(p: Any) -> str:
    """1 走分の PastRun を 1 行のコンパクト表記に。空フィールドは省く。

    例: "06/15 盛岡11R B1 ダ1400良 12頭 4人→3着 上り37.2 通過5-5"。
    着順は 1/2/3 のみ確定 (馬柱は他を明示しない) → それ以外は "着外" (= 4着以下)。
    """
    raw = (getattr(p, "date", "") or "").strip()
    parts = raw.replace("-", ".").replace("/", ".").split(".")
    md = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else raw
    venue = getattr(p, "venue", "") or ""
    rno = f"{p.race_no}R" if getattr(p, "race_no", 0) else ""
    klass = (getattr(p, "race_class", "") or "").strip()
    surf = getattr(p, "surface", "") or ""
    dist = str(p.distance) if getattr(p, "distance", 0) else ""
    going = getattr(p, "going", "") or ""
    fs = f"{p.field_size}頭" if getattr(p, "field_size", 0) else ""
    pop = f"{p.popularity}人" if getattr(p, "popularity", 0) else ""
    fp = getattr(p, "finish_pos", None)
    fin = f"{fp}着" if fp in (1, 2, 3) else "着外"
    last3f = f"上り{p.last_3f_sec:.1f}" if getattr(p, "last_3f_sec", 0) else ""
    passing = f"通過{p.passing}" if (getattr(p, "passing", "") or "").strip() else ""
    head = " ".join(x for x in (md, f"{venue}{rno}", klass,
                                f"{surf}{dist}{going}", fs) if x)
    res = f"{pop}→{fin}" if pop else fin
    tail = " ".join(x for x in (res, last3f, passing) if x)
    return (head + " " + tail).strip()


def _render_past_runs_lines(horses: list, past_source: str = "") -> list[str]:
    """各馬の **前走〜近走 (公式の確定成績)** を prompt セクションとして描画。

    h.past_runs は keibago (HorseMarkInfo=地方競馬公式) / JRA (accessU=JRA公式) / netkeiba 馬柱
    から既に取得済 & leakage 防止 (対象日以降除外・直近5走) 済み。ここで提示することで Claude が
    着順・距離・馬場・頭数・人気・上り・通過順を **web 検索せず** に読めるようにし、検索予算を
    「それ以外」(直前情報・軟情報・騎手成績) に集中させる (ユーザ指示 2026-06-29)。

    過去走が 1 頭も無ければ空リスト (= セクションを出さない → 従来どおり Claude が近走を検索)。
    section header は partition マーカー ('## 検索 MCP の運用ルール' / '## 指数の付け方') と
    衝突しない文字列にする (string-surgery 保護)。
    """
    if not any(getattr(h, "past_runs", None) for h in horses):
        return []
    src = f" ({past_source} 取得)" if past_source else " (公式データ)"
    out = [
        "",
        "## 前走戦績 (公式データ・検索不要)",
        f"下記は各馬の **前走〜近走の確定成績**{src}。**着順・距離・馬場・頭数・人気・上り・"
        "通過順はここを正とし、これらは web 検索しない** (検索予算は下記「それ以外」に回す)。"
        "\"着外\" は 4 着以下 (馬柱に着順非掲載)。",
    ]
    for h in horses:
        runs = getattr(h, "past_runs", None) or []
        if runs:
            body = " / ".join(_fmt_past_run(p) for p in runs[:5])
        else:
            body = "(前走データなし → この馬のみ近走を検索で補ってよい)"
        out.append(f"- {h.number} {h.name or '?'}: {body}")
    return out


def build_horse_score_prompt(
    rd: RaceData,
    *,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    queries_per_horse: int = 2,
    past_source: str = "",
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
        "あなたは競馬の考察家です。下の出走馬について **web 検索 (Tavily)** で "
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
        "適性総合の常識水準に留め、無理に動かさない (= 楽観バイアスを避ける)。",
        "**※ 単勝オッズ・人気は意図的に与えていません** — 市場と独立した検索ドリブンの指数にするため。"
        "市場 (オッズ) は後段で別途ブレンドされるので、あなたが市場を再現する必要はありません "
        "(適性総合は過去走由来でオッズ非依存なので参考に残しています)。",
        "",
        "## 出走馬 (馬番 / 馬名 / 性齢 / 騎手 / 馬体重(増減) / 適性総合)",
        "| 馬番 | 馬名 | 性齢 | 騎手 | 馬体重 | 適性 |",
        "|---|---|---|---|---|---|",
    ]
    for h in horses:
        a = apt.get(h.number)
        atot = f"{getattr(a, 'total', 0):.0f}" if a is not None else "-"
        sa = getattr(h, "sex_age", "") or "-"
        jk = getattr(h, "jockey_name", "") or "-"
        bw = getattr(h, "body_weight", 0)
        bwd = getattr(h, "body_weight_diff", 0)
        bws = f"{bw}({bwd:+d})" if bw else "-"
        lines.append(f"| {h.number} | {h.name or '?'} | {sa} | {jk} | {bws} | {atot} |")
    # 前走戦績 (公式の確定成績) を提示 — Claude が近走を検索せず読めるようにし、検索は「それ以外」へ。
    past_lines = _render_past_runs_lines(horses, past_source)
    lines += past_lines
    has_past = bool(past_lines)
    # has_past のとき 前走着順は上の公式データを正とし検索しない (= 検索予算を直前/軟情報に集中)。
    bullet3 = (
        "  3. **前走戦績は上の「前走戦績」セクション (公式データ) を参照 — 着順/距離/馬場/頭数/"
        "人気/上り/通過は検索しない**。前走の \"質\" (不利・展開・made the running 等の言葉) のみ②で補う。"
        if has_past else
        "  3. 直近5走の着順詳細・距離/コース適性 (公開済で市場に入りやすいが波形確認に使う)"
    )
    no_search = (
        "**検索すべきでない**: 既に上表にある数値 (適性)、**上の前走戦績にある確定成績 "
        "(着順/距離/馬場/頭数/人気/上り/通過) の再取得**、市場の人気/オッズ (= 市場の鏡で edge に"
        "ならない)、競馬の基本ルール、1か月以上前の汎用情報、「単に人気だから強い」という類の確認。"
        if has_past else
        "**検索すべきでない**: 既に上表にある数値 (適性)、市場の人気/オッズ (= 市場の鏡で edge にならない)、"
        "競馬の基本ルール、1か月以上前の汎用情報、「単に人気だから強い」という類の確認。"
    )
    lines += [
        "",
        "## 検索 MCP の運用ルール (CLAUDE.md 準拠)",
        ("**前走戦績は上の公式データで判明済み**。検索は **それ以外** (①直前情報・②軟情報・騎手成績) "
         "に全予算を充てる。" if has_past else ""),
        "**検索すべき情報 (優先度順 — 上の ①直前情報 / ②軟情報 を最優先)**: ",
        "  1. 取消・除外・出走取消、当日馬体重(増減)、馬場の急変発表 (= ①直前情報、最優先)",
        "  2. 前走の不利メモ・厩舎の勝負気配・パドック/返し馬気配・展開の言語予想 (= ②軟情報)",
        bullet3,
        "  4. 騎手の当該コース成績 / 乗り替わりの強化・弱体",
        no_search,
        f"**検索予算**: このレースは {len(horses)} 頭立て。**全馬を 1 頭あたり約 "
        f"{max(1, queries_per_horse)} クエリ (合計 ~{len(horses) * max(1, queries_per_horse)} "
        "クエリ) まで** Tavily で補強してよい。各馬について最低 "
        "1 回は ①直前情報 (取消/馬体重) または ②軟情報 (近走の不利/勝負気配) を確認し、評価が"
        "割れる馬は深掘りする。各クエリ前に「何が決まるか」を 1 行説明。",
        "**並列実行 (重要・速度に直結)**: 互いに依存しない検索・WebFetch は **必ず 1 ターンで同時に "
        "(parallel に複数 tool_use を) 呼び出す**こと。馬ごとに 1 つずつ順番 (sequential) に投げると "
        "1 ラウンド 30-50s × 頭数で timeout する。例: 全馬の近走を一度にまとめて並列発行 → 結果が揃って "
        "から、評価が割れる馬だけ次の 1 バッチを並列で深掘りする。**2-3 バッチで全馬を調べ切る**のが目安。",
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
        "距離/コース/馬場の不適性・近走凡走・乗替りマイナス・展開不利。強そうに見えても根拠が弱いと判断すれば低くしてよい。",
        "- **取消/除外/重度の体調不安**が確認できた馬は指数 0 にする (絡む目をモデルが落とせるよう)。",
        "- ①②の確たる根拠が無い馬は適性総合の常識的水準に留め、過度に動かさない (楽観バイアス警戒)。",
        "",
        "## 各馬の補強根拠 (evidence) と件数 (support) — 詳しく・全部出す",
        "各馬について、指数を上げ下げした **裏付け根拠を 1 件ずつ具体的に** `evidence` 配列へ書く。"
        "**件数の上限は無い — 3 件で打ち切らず、見つけた根拠はあるだけ全部 (10 件以上でも構わない、"
        "多いほどよい) を列挙する。** 1 要素 = 1 事実を、馬名・数値・出典の手がかりを添えて具体的に "
        "(例: \"前走(東京1400)は直線で前が詰まり追えず着順以上に強い内容\" / \"今走は叩き2走目で上昇度大\" / "
        "\"鞍上◯◯は当該コース複勝率45%\" / \"当日馬体重-12kgで余裕残しなし\" / \"距離短縮が末脚を活かす形\")。"
        "**プラス材料もマイナス材料も同じく 1 件**。①直前情報・②軟情報の根拠を特に重視して挙げる。"
        "`support` は **その evidence の件数** (= len(evidence)、上限なし)。根拠が多い馬ほどモデルは"
        "あなたの指数 (上げ・下げ問わず) を厚く採用する。材料が無ければ evidence は空配列・support 0。",
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
        "全出走馬の 馬番→指数(0-100)・support・evidence を必ず網羅し (evidence は省略せず全件)、"
        "alerts は該当馬のみ、最後に以下の JSON を ```json ... ``` で出力:",
        "```json",
        '{"scores": {"7": 82, "2": 64, "11": 40, "3": 0},'
        ' "support": {"7": 5, "2": 1, "11": 0, "3": 1},'
        ' "evidence": {"7": ["前走(東京1400)は直線で前が詰まり追えず着順以上に強い内容",'
        ' "今走は叩き2走目で上昇度大", "距離短縮が末脚を活かす形に合致",'
        ' "鞍上が当該コース複勝率45%と相性良", "厩舎が遠征に勝負気配のコメント"],'
        ' "2": ["当日馬体重-10kgで仕上がりに不安"], "3": ["出走取消を確認"]},'
        ' "alerts": {"7": ["前走不利", "厩舎勝負気配"], "2": ["馬体重-10kg"], "3": ["取消"]},'
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
    past_source: str = "",
) -> Iterator[tuple[str, Any]]:
    """各馬の強さ指数を web 検索付きで出させる stream-json (spawn/event 形式は
    select_trifecta_stream と共通)。出力は parse_horse_scores で {scores,...} に正規化する。

    検索予算は env `KEIBA_SCORE_QUERIES_PER_HORSE` (既定 2) を尊重する。並列 score (ARCH-A) が
    効かない少頭数 (< KEIBA_SCORE_MIN_HORSES_FOR_PARALLEL) でも、shobu 等が同 env を立てていれば
    単一セッションでも「頭数 × N クエリ」が流れる (env 未設定なら従来どおり 2/頭 で挙動不変)。
    """
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return
    horses = [h for h in rd.race.horses if not h.absent]
    if not horses:
        yield ("result", "")
        return
    qph = _env_int("KEIBA_SCORE_QUERIES_PER_HORSE", 2)
    prompt = build_horse_score_prompt(
        rd, aptitudes=aptitudes, market_signals=market_signals,
        horse_best_times=horse_best_times, queries_per_horse=qph,
        past_source=past_source,
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


def _normalize_evidence(raw: Any, *, max_items: int = 40, max_len: int = 300) -> dict[int, list[str]]:
    """evidence (各馬の補強根拠の詳細配列) を {int: [str,...]} に正規化。

    alerts と同じ {"3": ["…", "…"]} 形式だが、こちらは **件数上限を緩く** (max_items, 既定 40)
    取り、各要素を max_len で軽くクランプするだけ (ユーザ指示「あればあるだけ・3件で打ち切らない」)。
    単一文字列もリスト化、空文字/None は除外、空配列の馬は落とす。壊れた/無い入力は {}。
    """
    out: dict[int, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
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
        cleaned: list[str] = []
        for x in items:
            if x is None:
                continue
            s = str(x).strip()
            if s:
                cleaned.append(s[:max_len])
        if cleaned:
            out[num] = cleaned[:max_items]
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
             "evidence": {}, "notes": {}, "summary": "", "confidence": ""}
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
    evidence = _normalize_evidence(raw.get("evidence"))
    # support が無い/過小でも evidence があれば件数で補完 (UI の「根」= evidence 件数に揃える)。
    for num, items in evidence.items():
        if items and support.get(num, 0) < len(items):
            support[num] = len(items)
    return {
        "scores": scores,
        "support": support,
        "scale": scale,
        "alerts": _normalize_alerts(raw.get("alerts")),
        "evidence": evidence,
        "notes": notes,
        "summary": str(raw.get("summary", "")),
        "confidence": str(raw.get("confidence", "")),
    }


# ───────────────────────── 並列 score (Claude 指数) ─────────────────────────
# KEIBA_SCORE_PARALLEL=1 で有効化する **プロセス並列** score パイプライン (既定 OFF)。
# ARCH-A: 検索の重い部分を K 個の `claude -p` RESEARCH 子プロセスに分割 (各シャードは担当馬を
# 高検索予算で調べ「事実」だけ返す = 0-100 は付けない) → 1 個の `claude -p` SCORING 段が
# 全馬 + 収集事実を見て **レース内相対 0-100** を一括採点。相対性は採点が単一段に閉じることで
# 構造的に保たれる (単一セッション版と同義)。どこかで失敗したら単一セッション score_horses_stream
# にフォールバック (= 既定挙動)。検索バジェットは env で大幅増 (既定 6 クエリ/頭, 旧 2)。
# env knobs: KEIBA_SCORE_PARALLEL(master,既定OFF) / KEIBA_SCORE_QUERIES_PER_HORSE(6) /
#   KEIBA_SCORE_HORSES_PER_SHARD(4) / KEIBA_SCORE_MAX_SHARDS(4) /
#   KEIBA_SCORE_MIN_HORSES_FOR_PARALLEL(8) / KEIBA_LLM_MAX_CONCURRENT(5)。

def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return v if v > 0 else default


def _score_cmd(prompt: str, model: str) -> list[str]:
    """score 系 claude -p の共通コマンド (score_horses_stream と同一フラグ)。"""
    return [
        "claude", "-p", prompt,
        "--model", model,
        "--effort", "max",
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", DISALLOWED_TOOLS,
    ]


def _shard_numbers(numbers: list[int], per_shard: int, max_shards: int) -> list[list[int]]:
    """馬番リストを最大 max_shards 個・1 シャード ~per_shard 頭で均等分割 (全馬を必ず被覆)。"""
    import math
    n = len(numbers)
    if n == 0:
        return []
    k = max(1, min(max_shards, math.ceil(n / max(1, per_shard))))
    base, rem = divmod(n, k)
    shards: list[list[int]] = []
    i = 0
    for s in range(k):
        size = base + (1 if s < rem else 0)
        if size:
            shards.append(numbers[i:i + size])
        i += size
    return [s for s in shards if s]


# 並列 claude -p の同時実行をプロセス横断で best-effort に制限する file-slot semaphore。
# 複数 watch-auto ループ (Web UI の JRA/NAR 同時稼働等) が同時に score を走らせると共有
# TAVILY_API_KEY が 429 になり得る。block せず「取れた permit 数まで shard を減らす」方向で
# degrade し、締切を守る。fail-open (lock dir が使えなければ throttle 無し)。
_CLAUDE_SLOT_DIR = ROOT / "data" / "cache" / "llm_claude_slots"
_CLAUDE_SLOT_STALE_SEC = 1200


class _ClaudeGate:
    """research 子プロセス用 best-effort 並列ゲート。acquire() は取れた permit 数 (0..want) を返す。"""

    def __init__(self, want: int):
        self.want = max(1, want)
        self.held: list[Path] = []

    def acquire(self) -> int:
        cap = _env_int("KEIBA_LLM_MAX_CONCURRENT", 5)
        try:
            _CLAUDE_SLOT_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            return self.want  # fail-open: throttle 無し
        self._reclaim_stale()
        for _ in range(self.want):
            slot = self._claim_one(cap)
            if slot is None:
                break
            self.held.append(slot)
        return len(self.held)

    def _claim_one(self, cap: int) -> Path | None:
        for i in range(cap):
            p = _CLAUDE_SLOT_DIR / f"slot_{i}.lock"
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return p
            except FileExistsError:
                continue
            except Exception:  # noqa: BLE001
                return None
        return None

    def _reclaim_stale(self) -> None:
        now = time.time()
        try:
            for f in _CLAUDE_SLOT_DIR.glob("slot_*.lock"):
                try:
                    if now - f.stat().st_mtime > _CLAUDE_SLOT_STALE_SEC:
                        f.unlink()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def release(self) -> None:
        for p in self.held:
            try:
                p.unlink()
            except Exception:  # noqa: BLE001
                pass
        self.held = []


def build_horse_research_prompt(
    rd: RaceData,
    shard: list[int],
    *,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    queries_per_horse: int = 6,
    past_source: str = "",
) -> str:
    """RESEARCH 専用プロンプト (担当 shard の馬だけ高予算で調べ、事実のみ返す・採点しない)。

    既存 build_horse_score_prompt の **ヘッダ + edge 説明 + 出走馬表 + 前走戦績** を流用 (文字列手術)
    し、検索ルール以降を「担当馬のみ・大幅増の検索予算・facts JSON (0-100 なし)」に差し替える。
    前走戦績セクションは `pre` (検索 MCP マーカーより前) に含まれるので RESEARCH 子にも伝わる。
    """
    base = build_horse_score_prompt(
        rd, aptitudes=aptitudes, market_signals=market_signals,
        horse_best_times=horse_best_times, past_source=past_source,
    )
    pre = base.partition("## 検索 MCP の運用ルール")[0]  # ヘッダ + edge + 出走馬表 + 前走戦績
    has_past = "## 前走戦績" in pre
    shard_set = sorted({int(x) for x in shard})
    nq = len(shard_set) * max(1, queries_per_horse)
    example_no = shard_set[0] if shard_set else 3
    rules = [
        "## ⚠ これはリサーチ専用タスク (点数・順位は付けない)",
        f"あなたの担当馬は **馬番 {shard_set}** のみ。担当馬だけを web 検索 (Tavily) で深く調べ、"
        "**0-100 の強さ指数や順位は一切付けない** (全馬まとめての採点は次段が行う)。担当馬について "
        "上記 ①直前情報・②軟情報 を中心に事実を集め、構造化して出すのが仕事。担当外の馬は調べない。",
        "",
        "## 検索 MCP の運用ルール",
        ("**前走戦績は上の「前走戦績」セクション (公式データ) で判明済み — 着順/距離/馬場/頭数/"
         "人気/上り/通過は検索しない**。検索は担当馬の ①直前情報・②軟情報・騎手成績 (= それ以外) "
         "に全予算を充てる。" if has_past else ""),
        f"**検索予算 (大幅増)**: 担当 {len(shard_set)} 頭 × 約 {queries_per_horse} クエリ = "
        f"**合計 ~{nq} クエリまで** Tavily で深掘りしてよい (従来の単一セッション 2/頭 より大幅増)。"
        "各馬最低 1 回は ①直前情報 (取消/馬体重) または ②軟情報 (近走の不利/勝負気配) を確認する。",
        "**並列実行 (重要・速度に直結)**: 互いに依存しない検索・WebFetch は **必ず 1 ターンで同時に "
        "複数 tool_use** で呼ぶ。担当馬の ①直前/②軟情報を一度に並列発行 → 評価が割れる馬だけ次バッチで深掘り。",
        "**時間厳守**: 締切直前処理。timeout すると担当分が失われるので、予算を超えそうなら手元の "
        "根拠で確定し **必ず最後まで JSON を出力する**。",
        "**検索すべきでない**: 上表の数値 (適性)、**上の前走戦績にある確定成績 (着順/距離/馬場/頭数/"
        "人気/上り/通過) の再取得**、市場の人気/オッズ (= 市場の鏡)、競馬の基本ルール、1 か月以上前の汎用情報。",
        "",
        "## 出力 (担当馬のみ・facts)",
        "担当馬それぞれについて、調べた ①直前/②軟情報 を構造化して以下の JSON を ```json ... ``` で "
        "出力 (担当外の馬・0-100 指数は含めない):",
        "  - alerts: 短い日本語ラベル配列 (\"取消\"/\"馬体重-12kg\"/\"前走不利\"/\"厩舎勝負気配\"/"
        "\"乗替り\"/\"馬場渋化\"/\"逃げ濃厚\"/\"展開不利\" 等。根拠が無ければ空配列)",
        "  - evidence: 指数を動かす裏付け根拠を **1 件ずつ具体的に** 書いた配列。"
        "**上限なし — あるだけ全部 (10 件以上でも可・多いほどよい)**。各要素は数値・出典の手がかりを"
        "添えて具体的に (1 要素 = 1 事実、プラス材料もマイナス材料も 1 件)。根拠が無ければ空配列。",
        "  - support: evidence の件数 (= len(evidence)、上限なし)",
        "  - digest: その馬の調査要約 (240 字以内・市場とズレる点を中心に)",
        "```json",
        '{"facts": {"' + str(example_no) + '": {"alerts": ["前走不利", "厩舎勝負気配"], '
        '"evidence": ["前走(東京1400)は直線で前が詰まり追えず着順以上に強い", "今走は叩き2走目で上昇度大", '
        '"距離短縮が末脚を活かす形に合致", "鞍上が当該コース複勝率45%と相性良"], '
        '"support": 4, "digest": "前走は直線で詰まる不利、本来は掲示板級。今走は叩き2走目で上昇、距離も合う。"}}}',
        "```",
    ]
    return pre + "\n".join(rules)


def build_horse_score_from_research_prompt(
    rd: RaceData,
    research: dict[int, dict],
    *,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    past_source: str = "",
) -> str:
    """収集済みリサーチを使った SCORING プロンプト (全馬を一括で相対 0-100 採点・新規検索ほぼ無し)。

    既存 build_horse_score_prompt の **ヘッダ + edge + 出走馬表** と **指数の付け方以降 (採点ルール
    + JSON schema)** をそのまま流用 (文字列手術) し、検索ルールを「収集済みリサーチ + 採点指示」に
    差し替える。0-100 の相対採点を **単一段で全馬まとめて** 行うので相対性が保たれる。
    """
    base = build_horse_score_prompt(
        rd, aptitudes=aptitudes, market_signals=market_signals,
        horse_best_times=horse_best_times, past_source=past_source,
    )
    head, sep, tail = base.partition("## 指数の付け方")     # tail = 採点ルール + JSON schema
    pre = head.partition("## 検索 MCP の運用ルール")[0]      # ヘッダ + edge + 出走馬表
    rlines = ["## 収集済みリサーチ (各馬・並列検索で取得済)"]
    if research:
        for num in sorted(research):
            rec = research[num] or {}
            al = " / ".join(rec.get("alerts") or []) or "—"
            dg = (str(rec.get("digest") or "")).strip() or "—"
            rlines.append(f"- 馬番 {num}: [{al}] {dg}")
            for e in (rec.get("evidence") or []):
                rlines.append(f"    ・{e}")        # 補強根拠を 1 件ずつ (採点段が全件 evidence へ書き出す)
    else:
        rlines.append("- (リサーチ無し → 出走馬表の適性と近走から採点)")
    rlines += [
        "",
        "## 採点時の検索ルール (収集済みの根拠を使用)",
        "上の **収集済みリサーチ** + 出走馬表 (適性) を使って **全馬を相対採点** する。"
        "新規検索は原則不要 (どうしても矛盾を確認したい時のみ最大 1 クエリ)。リサーチで取消/除外が"
        "確認された馬は指数 0。**収集済みリサーチの根拠 (・ 行) は省略せず `evidence` 配列に全件"
        "書き出し** (各馬 10 件以上でも構わない・あるだけ全部)、support はその件数にする。",
        "",
    ]
    return pre + "\n".join(rlines) + sep + tail


def _merge_research(shard_texts: list[str]) -> dict[int, dict]:
    """各 RESEARCH シャードの出力 (facts JSON 文字列) を馬番キーの evidence dict に union。

    数値の再正規化は一切しない (0-100 採点は次段が単一で行う)。壊れた/空のシャードは無視。
    """
    research: dict[int, dict] = {}
    for text in shard_texts:
        obj = parse_evidence(text or "")
        facts = obj.get("facts") if isinstance(obj, dict) else None
        if not isinstance(facts, dict):
            continue
        for k, v in facts.items():
            try:
                num = int(k)
            except (ValueError, TypeError):
                continue
            if not isinstance(v, dict):
                continue
            alerts = _normalize_alerts({num: v.get("alerts")}).get(num, [])
            evidence = _normalize_evidence({num: v.get("evidence")}).get(num, [])
            try:
                support = max(0, int(float(v.get("support", 0))))
            except (ValueError, TypeError):
                support = 0
            support = max(support, len(evidence))   # evidence があれば件数で補完
            digest = str(v.get("digest", "") or "")[:240]
            research[num] = {"alerts": alerts, "evidence": evidence,
                             "support": support, "digest": digest}
    return research


def _run_research_child(
    *, rd: RaceData, shard: list[int], shard_id: int, model: str, timeout: int,
    queries_per_horse: int, aptitudes, market_signals, horse_best_times,
    out_queue: "_queue.Queue", past_source: str = "",
) -> None:
    """1 シャードの RESEARCH を `claude -p` で実行。tool_use は live 転送、本文は最後に集約して
    out_queue へ。例外/timeout でも必ず ('shard_done', id) を出して呼び側を進める。"""
    text_parts: list[str] = []
    try:
        prompt = build_horse_research_prompt(
            rd, shard, aptitudes=aptitudes, market_signals=market_signals,
            horse_best_times=horse_best_times, queries_per_horse=queries_per_horse,
            past_source=past_source,
        )
        proc, err_f = _spawn_claude(_score_cmd(prompt, model))
        timer, timed_out = _start_kill_timer(proc, timeout)
        try:
            assert proc.stdout is not None
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
                            out_queue.put(("tool_use", {"name": block.get("name", ""),
                                                        "input": block.get("input", {})}))
                        elif block.get("type") == "text" and block.get("text"):
                            text_parts.append(block["text"])  # 本文は join せず集約 (採点段の JSON を汚さない)
                elif etype == "result":
                    text_parts.append(ev.get("result", "") or "")
        finally:
            for _etype, msg in _finalize_claude_proc(proc, timer, timed_out, err_file=err_f):
                out_queue.put(("shard_error", shard_id, msg))
    except Exception as e:  # noqa: BLE001
        out_queue.put(("shard_error", shard_id, f"shard {shard_id}: {e}"))
    finally:
        out_queue.put(("shard_text", shard_id, "\n".join(text_parts)))
        out_queue.put(("shard_done", shard_id))


def score_horses_parallel(
    rd: RaceData,
    *,
    model: str = "opus",
    timeout: int = 900,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    past_source: str = "",
) -> Iterator[tuple[str, Any]]:
    """ARCH-A 並列 score。score_horses_stream と同じ (event_type, payload) を yield する。

    Phase A (並列 research, 高検索予算) → facts を merge → Phase B (単一 relative scoring)。
    Phase B の ('result', json) のみが採点 JSON。研究子の本文は yield しない (採点 JSON を汚さない)。
    どこかで result を出せなければ ('error',...) で終わる (呼び側 score_horses がフォールバック)。
    """
    if not is_available():
        yield ("error", "claude CLI が見つかりません")
        return
    horses = [h for h in rd.race.horses if not h.absent]
    if not horses:
        yield ("result", "")
        return
    numbers = [h.number for h in horses]
    per_shard = _env_int("KEIBA_SCORE_HORSES_PER_SHARD", 4)
    max_shards = _env_int("KEIBA_SCORE_MAX_SHARDS", 4)
    qph = _env_int("KEIBA_SCORE_QUERIES_PER_HORSE", 6)
    shards = _shard_numbers(numbers, per_shard, max_shards)
    research_timeout = max(60, int(timeout * 0.6))
    scoring_timeout = max(60, timeout - research_timeout)

    gate = _ClaudeGate(len(shards))
    got = gate.acquire()
    if got < 1:
        gate.release()
        yield ("error", "claude gate: permit が取れず単一セッションへ")
        return
    if got < len(shards):
        shards = _shard_numbers(numbers, per_shard, got)  # 取れた permit 数まで shard を縮約 (全馬被覆維持)

    out_q: "_queue.Queue" = _queue.Queue()
    threads: list[threading.Thread] = []
    try:
        for sid, shard in enumerate(shards):
            t = threading.Thread(
                target=_run_research_child,
                kwargs=dict(
                    rd=rd, shard=shard, shard_id=sid, model=model, timeout=research_timeout,
                    queries_per_horse=qph, aptitudes=aptitudes, market_signals=market_signals,
                    horse_best_times=horse_best_times, out_queue=out_q, past_source=past_source,
                ),
                daemon=True,
            )
            t.start()
            threads.append(t)
        done = 0
        shard_texts: dict[int, str] = {}
        while done < len(shards):
            item = out_q.get()
            kind = item[0]
            if kind == "tool_use":
                yield ("tool_use", item[1])
            elif kind == "shard_text":
                shard_texts[item[1]] = item[2]
            elif kind == "shard_done":
                done += 1
            # shard_error は握りつぶす (該当シャードは空 evidence として merge される)
    finally:
        gate.release()

    research = _merge_research([shard_texts.get(i, "") for i in sorted(shard_texts)])
    if not research:
        yield ("error", "全 research シャードが facts を返さず → フォールバック")
        return

    # Phase B: 単一 claude で全馬の相対 0-100 を一括採点 (検索ほぼ無し)。
    prompt = build_horse_score_from_research_prompt(
        rd, research, aptitudes=aptitudes, market_signals=market_signals,
        horse_best_times=horse_best_times, past_source=past_source,
    )
    proc, err_f = _spawn_claude(_score_cmd(prompt, model))
    timer, timed_out = _start_kill_timer(proc, scoring_timeout)
    try:
        assert proc.stdout is not None
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
        for ev in _finalize_claude_proc(proc, timer, timed_out, err_file=err_f):
            yield ev


def score_horses(
    rd: RaceData,
    *,
    model: str = "opus",
    timeout: int = 900,
    aptitudes: dict[int, Any] | None = None,
    market_signals: dict[int, Any] | None = None,
    horse_best_times: list[dict] | None = None,
    past_source: str = "",
) -> Iterator[tuple[str, Any]]:
    """score ステージの dispatcher。KEIBA_SCORE_PARALLEL かつ十分な頭数なら並列 score を試し、
    'result' を出せなければ単一セッション score_horses_stream にフォールバック。既定 (env 未設定)
    は従来どおり score_horses_stream をそのまま流す (挙動変化なし)。"""
    horses = [h for h in rd.race.horses if not h.absent]
    min_h = _env_int("KEIBA_SCORE_MIN_HORSES_FOR_PARALLEL", 8)
    if not (_env_truthy("KEIBA_SCORE_PARALLEL") and len(horses) >= min_h):
        yield from score_horses_stream(
            rd, model=model, timeout=timeout, aptitudes=aptitudes,
            market_signals=market_signals, horse_best_times=horse_best_times,
            past_source=past_source,
        )
        return
    # 並列パスを buffer して result の有無で fallback 判定 (二重 result を出さない)。
    buffered: list[tuple[str, Any]] = []
    saw_result = False
    try:
        for ev in score_horses_parallel(
            rd, model=model, timeout=timeout, aptitudes=aptitudes,
            market_signals=market_signals, horse_best_times=horse_best_times,
            past_source=past_source,
        ):
            buffered.append(ev)
            if ev[0] == "result" and ev[1]:
                saw_result = True
    except Exception as e:  # noqa: BLE001
        saw_result = False
        buffered.append(("error", f"parallel score 例外: {e}"))
    if saw_result:
        yield from buffered
        return
    # フォールバック: 並列で result が出なかった → 単一セッションで再実行 (今日までの実績パス)。
    yield ("text", "[並列 score が result 無し → 単一セッション score_horses_stream にフォールバック]")
    yield from score_horses_stream(
        rd, model=model, timeout=timeout, aptitudes=aptitudes,
        market_signals=market_signals, horse_best_times=horse_best_times,
        past_source=past_source,
    )


# 締切1分前の高速 3連単選定では web 検索もファイル読みも一切させない (純粋推論で ~10-30s)。
_TRIFECTA_SELECT_DISALLOWED = (
    DISALLOWED_TOOLS + ",Read,WebFetch,"
    "mcp__tavily__tavily_search,mcp__tavily__tavily-search,"
    "mcp__tavily__tavily_extract,mcp__tavily__tavily-extract"
)


def build_trifecta_select_prompt(
    rd: RaceData, *, llm_index: dict[int, float] | None,
    aptitudes: dict[int, Any] | None = None,
    bankroll: int = 10_000,
    max_points: int = 48,
) -> str:
    """締切直前の 3連単買い目選定 prompt (検索なし・Claude 指数上位から自由構築)。

    各馬の **Claude 指数 (score 段で web 研究済)** + 適性総合のみを提示し、指数上位を本命に
    1着→2着→3着 のフォーメーションを **自由に構築**させる (全力的中)。出力は買い目
    (ordered triple) の配列のみ。トリガミ防止・配分は後段 (build_trifecta_from_keys) が行う。

    **市場は一切見せない**: 「市場無視」が本質なので、単勝オッズ・人気はプロンプトに
    含めない (含めると Claude が市場に引きずられて指数選定が崩れる)。並べ替えも指数→適性→馬番で
    市場フリーにする。購入は **1レース予算 (bankroll) 内に収める**よう点数を絞らせる。
    """
    r = rd.race
    idx = llm_index or {}
    apt = aptitudes or {}
    horses = [h for h in r.horses if not getattr(h, "absent", False)]

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
        f"# {venue} {rno}R 3連単「全力的中」買い目選定 ({dist}m {surf})",
        "あなたは競馬の3連単買い目を組むエキスパート。**各馬の強さ指数 (score 段で web 研究済)** と "
        "適性総合を見て、**指数上位を本命に 1着→2着→3着 のフォーメーション (買い目) を自由に構築**する。",
        "**検索はしない** (締切直前・指数は研究済)。",
        "**重要: このモードは『市場無視』。単勝オッズ・人気・市場の評価は一切与えていないし、"
        "推測もしないこと。あくまで下の Claude 指数 (と適性) だけで強さ順を判断して買い目を組む。**",
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
    head_rule = "- **1着** は指数最上位を 1〜2 頭に**絞る** (指数が拮抗するなら2頭)。"
    lines += [
        "",
        "## 組み方 (全力的中モード)",
        head_rule,
        "- **2着** は中くらい (指数上位 3〜5 頭程度)。**3着** は広めに取る (上位 5〜8 頭程度・"
        "3着づけは妙味も拾う)。head ⊆ mid ⊆ tail に拘らず、指数で妥当な馬を各列に置いてよい。",
        f"- **このレースの購入予算は ¥{bankroll:,}**。買い目の合計購入額が**この予算内に収まる**よう"
        f"点数を絞ること (1点あたり最低 ¥100・100円単位、トリガミ防止後に予算内)。予算 ÷ 100 が"
        f"買える点数の概算上限 (≈{max(1, bankroll // 100)} 点) だが、薄い目を無理に足さず"
        "「当たりやすさ」を最大化する点数に抑える。",
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
) -> Iterator[tuple[str, Any]]:
    """締切直前の高速 3連単選定 stream-json。**web 検索なし** (純粋推論で ~10-30s)。

    市場 (単勝オッズ・人気) はプロンプトに含めず Claude 指数のみで選定させる (市場無視)。
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
