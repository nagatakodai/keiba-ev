"""固定クエリ Tavily プリフェッチ (ARCH-B, ユーザ指示 2026-07-05).

score 段の web リサーチを「LLM が MCP 経由で対話的に検索する」(ARCH-A) から
「**Python が固定テンプレのクエリを Tavily API に直接発行**して資料 (dossier) を作り、
LLM は読解と採点 1 回だけ」に置き換える。

根拠 (実測 85R・検索 1,286 本の tool_usage ログ分類): Claude の score 段クエリは
パドック/馬体重 43%・馬名+場+クラス 21%・馬場 9%・予想 7%・取消 7%・展開/騎手/厩舎 計13%
と **8割超がテンプレ的** で、クエリ生成に LLM の知性はほぼ使われていない。LLM の本体価値は
検索結果の読解 (同名別馬/古い記事の排除)・±調整・evidence 化にあるので、そこだけ残す。

- クエリ: レース級 3 本 (馬場/取消/予想 — レースで 1 回 = ARCH-A のシャード重複が消える) +
  各馬 2 本 (パドック系・近況) + ユニーク騎手 1 本ずつ (上限あり)
- Tavily API 直叩き (env `TAVILY_API_KEY`)。MCP/claude 非依存・輻輳しない並列度 (既定 4)。
- 結果は `data/cache/research/<race_id>.json` にキャッシュ (TTL 既定 900s — 自動再score や
  リトライの再フェッチを防ぐ)。
- 部分失敗は呑む (取れた分だけの dossier)。API キー無し/全滅は None → 呼び元 (llm.score_horses)
  が従来 (agentic) にフォールバック。

env:
  TAVILY_API_KEY               必須 (無ければ prefetch 不可)
  KEIBA_PREFETCH_RESULTS       1クエリの取得件数 (既定 4)
  KEIBA_PREFETCH_DEPTH         basic|advanced (既定 basic。advanced は 2 クレジット/クエリ)
  KEIBA_PREFETCH_CONCURRENCY   同時クエリ数 (既定 4)
  KEIBA_PREFETCH_TTL_SEC       キャッシュ TTL (既定 900)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import RaceData

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache" / "research"
_JST = ZoneInfo("Asia/Tokyo")
_API_URL = "https://api.tavily.com/search"

# スニペット/タイトルのクランプ (プロンプト肥大防止)。12頭 × 2クエリ × 4件 × ~280字 ≈ 27KB。
_CONTENT_CLAMP = 280
_TITLE_CLAMP = 90
_MAX_JOCKEY_QUERIES = 8


def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v > 0 else default
    except ValueError:
        return default


def _api_key() -> str:
    return (os.environ.get("TAVILY_API_KEY") or "").strip()


def _race_date(rd: RaceData) -> dt.date:
    """発走日 (JST)。start_at が無ければ当日 (score 段は当日レースが前提)。"""
    if rd.race.start_at:
        return dt.datetime.fromtimestamp(rd.race.start_at, _JST).date()
    return dt.datetime.now(_JST).date()


def build_queries(rd: RaceData) -> tuple[list[dict[str, str]], dict[int, list[dict[str, str]]]]:
    """固定テンプレのクエリ集合 (実測分布ベース) を組む。

    返り値 (race_level, per_horse)。各要素 {"kind": 分類, "query": クエリ文字列}。
    レース級はレースで 1 回だけ (馬場/取消/予想)。騎手はユニーク騎手のみ race_level に載せる
    (同騎手の重複検索を防ぐ)。
    """
    r = rd.race
    d = _race_date(rd)
    date_ja = f"{d.year}年{d.month}月{d.day}日"
    venue = r.venue_name or ""
    cls = (r.race_class or "").strip()
    surface = (r.surface or "").strip()

    race_level: list[dict[str, str]] = [
        {"kind": "track",
         "query": f"{venue}競馬 {date_ja} 馬場状態 含水率 {surface}".strip()},
        {"kind": "scratch",
         "query": f"{venue} {date_ja} {r.race_number}R 出走取消 OR 除外 OR 騎手変更"},
        {"kind": "preview",
         "query": f"{venue} {r.race_number}R {cls} {date_ja} 予想 OR 展開 OR 見解".strip()},
    ]

    horses = [h for h in r.horses if not h.absent]
    per_horse: dict[int, list[dict[str, str]]] = {}
    for h in horses:
        name = (h.name or "").strip()
        if not name:
            continue
        per_horse[h.number] = [
            # 実測 43%: 直前情報 (パドック/馬体重/気配) — 最重要バケット。
            {"kind": "paddock",
             "query": f'"{name}" {venue} パドック OR 馬体重 OR 気配 OR 歩様 {date_ja}'},
            # 実測 21%: 馬名+場+クラス の近況確認。
            {"kind": "recent", "query": f'"{name}" {venue} {cls}'.strip()},
        ]

    # 騎手 (実測 4%): ユニーク騎手のみ・多頭数レースでの爆発を上限で抑える。
    seen: set[str] = set()
    for h in horses:
        j = (getattr(h, "jockey_name", "") or "").strip()
        if not j or j in seen or len(seen) >= _MAX_JOCKEY_QUERIES:
            continue
        seen.add(j)
        race_level.append({"kind": "jockey", "query": f"{j} 騎手 {venue} 成績"})
    return race_level, per_horse


def _search(query: str, *, max_results: int, depth: str, timeout: int = 15) -> list[dict[str, str]]:
    """Tavily /search を 1 回叩き、[{title,url,content}] を返す (クランプ済)。失敗は例外。

    リポジトリの流儀 (scrape_keibago 等) に合わせ stdlib urllib を使う (requests 非依存)。"""
    body = json.dumps({
        "api_key": _api_key(), "query": query, "max_results": max_results,
        "search_depth": depth, "include_answer": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        _API_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = []
    for it in (data.get("results") or [])[:max_results]:
        out.append({
            "title": str(it.get("title") or "")[:_TITLE_CLAMP],
            "url": str(it.get("url") or ""),
            "content": str(it.get("content") or "")[:_CONTENT_CLAMP],
        })
    return out


def _cache_path(race_id: str) -> Path:
    return CACHE_DIR / f"{race_id}.json"


def fetch_dossier(rd: RaceData, race_id: str | None = None, *,
                  force: bool = False) -> dict[str, Any] | None:
    """固定クエリを一括検索して dossier を返す (キャッシュ TTL 内は再利用)。

    返り値: {"race_id", "fetched_at", "queries": [全クエリ文字列], "race_level": [...],
    "horses": {馬番: [...]}} — 各要素は {"kind","query","results":[{title,url,content}]}。
    **API キー無し / 全クエリ失敗は None** (呼び元が agentic にフォールバック)。部分失敗は
    取れた分だけで返す (results 空の要素は落とす)。
    """
    if not _api_key():
        return None
    rid = race_id or f"{rd.race.cup_id}-{rd.race.schedule_index}-{rd.race.race_number}"
    ttl = _env_int("KEIBA_PREFETCH_TTL_SEC", 900)
    cp = _cache_path(rid)
    if not force and cp.exists():
        try:
            cached = json.loads(cp.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("fetched_at") or 0) < ttl:
                return cached
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    race_level, per_horse = build_queries(rd)
    if not per_horse:
        return None
    n_results = _env_int("KEIBA_PREFETCH_RESULTS", 4)
    depth = (os.environ.get("KEIBA_PREFETCH_DEPTH") or "basic").strip() or "basic"
    conc = _env_int("KEIBA_PREFETCH_CONCURRENCY", 4)

    jobs: list[tuple[str, int | None, dict[str, str]]] = []   # (scope, 馬番|None, qdict)
    for q in race_level:
        jobs.append(("race", None, q))
    for num, qs in per_horse.items():
        for q in qs:
            jobs.append(("horse", num, q))

    done_race: list[dict[str, Any]] = []
    done_horse: dict[int, list[dict[str, Any]]] = {num: [] for num in per_horse}
    ok = 0
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(_search, j[2]["query"], max_results=n_results, depth=depth): j
                for j in jobs}
        for fut in as_completed(futs):
            scope, num, qd = futs[fut]
            try:
                results = fut.result()
            except Exception:  # noqa: BLE001  — 部分失敗は呑む (取れた分だけの dossier)
                continue
            if not results:
                continue
            ok += 1
            row = {"kind": qd["kind"], "query": qd["query"], "results": results}
            if scope == "race":
                done_race.append(row)
            else:
                done_horse[int(num)].append(row)

    if ok == 0:
        return None   # 全滅 (レート制限/障害) → agentic フォールバック
    dossier = {
        "race_id": rid,
        "fetched_at": time.time(),
        "queries": [j[2]["query"] for j in jobs],
        "race_level": done_race,
        "horses": {str(k): v for k, v in done_horse.items()},
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dossier, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cp)
    except OSError:
        pass   # キャッシュ書き込み失敗は無害 (次回再フェッチ)
    return dossier


def render_dossier(dossier: dict[str, Any], rd: RaceData) -> str:
    """dossier をプロンプト用テキストに描画 (生スニペット = 未読解であることを明示)。"""
    lines: list[str] = []
    kind_ja = {"track": "馬場", "scratch": "取消/除外", "preview": "予想/展開",
               "jockey": "騎手", "paddock": "パドック/馬体重", "recent": "近況"}

    def _rows(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            lines.append(f"- [{kind_ja.get(row.get('kind'), row.get('kind'))}] "
                         f"クエリ: {row.get('query', '')}")
            for res in row.get("results") or []:
                dom = (res.get("url") or "").split("/")[2] if "//" in (res.get("url") or "") else ""
                lines.append(f"    ・{res.get('title', '')} — {res.get('content', '')}"
                             f"{f' ({dom})' if dom else ''}")

    lines.append("### レース全体 (馬場・取消・展開)")
    _rows(dossier.get("race_level") or [])
    names = {h.number: h.name for h in rd.race.horses}
    for num_s, rows in sorted((dossier.get("horses") or {}).items(),
                              key=lambda kv: int(kv[0])):
        if not rows:
            continue
        num = int(num_s)
        lines.append(f"### 馬番 {num} {names.get(num, '')}")
        _rows(rows)
    return "\n".join(lines)
