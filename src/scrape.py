"""netkeiba.com から HTML を取得する。

netkeiba は WINTICKET と違い __PRELOADED_STATE__ を埋め込んでいない (sevser-side HTML)。
そのため Playwright で render 後の HTML を取り、BeautifulSoup でパースする方針。

主要 URL:
  - 出馬表    https://race.netkeiba.com/race/shutuba.html?race_id=YYYYMMDDPP00RR
  - オッズ    https://race.netkeiba.com/odds/index.html?type=b8&race_id=...   (3 連単)
  - 結果      https://race.netkeiba.com/race/result.html?race_id=...
  - 開催一覧  https://race.netkeiba.com/top/race_list.html?kaisai_date=YYYYMMDD

race_id 形式:
  YYYY (年) + MMDD (開催日) + PP (場コード 2桁 01-10) + 00 + RR (R 数 2桁)
  例: 202605210601 = 2026/05/21 阪神 (06) 1R
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


def fetch_html(url: str, *, timeout_ms: int = 60_000, settle_ms: int = 4_000) -> str:
    """URL を開いて render 後の HTML を返す。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="ja-JP",
            viewport={"width": 1280, "height": 1800},
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(settle_ms)
        html = page.content()
        browser.close()
    return html


def cache_html(html: str, race_id: str, root: Path, suffix: str = "") -> Path:
    """HTML を data/raw/<race_id><suffix>.html にキャッシュ。"""
    out = root / "data/raw" / f"{race_id}{suffix}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def cache_state(state: dict[str, Any], race_id: str, root: Path) -> Path:
    """旧 WINTICKET 互換のため state JSON もキャッシュできるよう残置。"""
    out = root / "data/raw" / f"{race_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --- URL ヘルパ ---

RACE_ID_RE = re.compile(r"race_id=(\d{12})")


def extract_race_id(url: str) -> str | None:
    m = RACE_ID_RE.search(url)
    return m.group(1) if m else None


def shutuba_url(race_id: str) -> str:
    return f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"


def odds_trifecta_url(race_id: str) -> str:
    """3 連単 (type=b8)。"""
    return f"https://race.netkeiba.com/odds/index.html?type=b8&race_id={race_id}"


def result_url(race_id: str) -> str:
    return f"https://race.netkeiba.com/race/result.html?race_id={race_id}"


def race_list_url(date_yyyymmdd: str) -> str:
    return f"https://race.netkeiba.com/top/race_list.html?kaisai_date={date_yyyymmdd}"


# --- 旧 WINTICKET 互換シム (テストや analyze CLI の --state からの読み込みを壊さないため) ---

def fetch_state(url: str, *, timeout_ms: int = 60_000, settle_ms: int = 4_000) -> dict[str, Any]:
    """netkeiba 版では `fetch_state` は使わない。互換のため空 dict を返す。

    新しいエントリポイントは `fetch_html` + `parse.parse_race_data`。
    """
    raise NotImplementedError(
        "netkeiba 版では fetch_state は廃止。"
        "代わりに src.parse.fetch_and_parse(url) を使ってください。"
    )


def parse_state_from_html(html: str) -> dict[str, Any]:
    """旧 WINTICKET 互換シム。netkeiba HTML ではそのまま raw HTML を含む dict を返す。

    parse.parse_state が `html` キーを見て HTML パーサに切り替える。
    """
    return {"html": html}
