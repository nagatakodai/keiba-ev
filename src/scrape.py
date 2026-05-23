"""netkeiba.com (中央 / 地方) から HTML を取得する。

netkeiba は WINTICKET と違い __PRELOADED_STATE__ を埋め込んでいない (server-side HTML)。
そのため Playwright で render 後の HTML を取り、BeautifulSoup でパースする方針。

主要 URL:
  - 出馬表    https://race.netkeiba.com/race/shutuba.html?race_id=...     (JRA)
              https://nar.netkeiba.com/race/shutuba.html?race_id=...      (NAR)
  - オッズ    .../odds/index.html?type=b8&race_id=...                     (画面)
              .../odds/odds_get_form.html?type=b8&race_id=...&jiku=N      (AJAX 実体)
  - 結果      .../race/result.html?race_id=...
  - 開催一覧  .../top/race_list.html?kaisai_date=YYYYMMDD

race_id 形式 (12 桁、JRA と NAR で意味が違う):
  JRA: YYYY(4) + 場(2) + 開催回(2) + 開催日(2) + R(2)
       例 202605020711 = 2026 東京(05) 第2回 7日目 11R
  NAR: YYYY(4) + 場(2) + MM(2) + DD(2) + R(2)
       例 202644052111 = 2026 大井(44) 05月21日 11R
  どちらも 4-5 文字目が場コード。01-10 が JRA、30+ が NAR。
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

JRA_HOST = "race.netkeiba.com"
NAR_HOST = "nar.netkeiba.com"

# JRA 場コード: 01-10 (札幌〜小倉)
JRA_VENUE_CODES = {f"{i:02d}" for i in range(1, 11)}


def is_nar_race_id(race_id: str) -> bool:
    """race_id の場コード (4-5 文字目) から NAR/JRA を判定。

    JRA = 01-10、それ以外 (30+ など) は NAR 扱い。
    """
    if not race_id or len(race_id) < 6:
        return False
    return race_id[4:6] not in JRA_VENUE_CODES


def _host_for(race_id: str | None, *, nar: bool | None = None) -> str:
    if nar is True:
        return NAR_HOST
    if nar is False:
        return JRA_HOST
    if race_id and is_nar_race_id(race_id):
        return NAR_HOST
    return JRA_HOST


class NetkeibaBlocked(RuntimeError):
    """netkeiba (CloudFront) から空 HTML / 400 が返ってきた場合に投げる。

    多数の連続 request 後に IP / UA がレート制限される現象が知られている。
    `fetch_html` が body の無い HTML (≤60 字、`<body></body>` 形式) を検出
    したら本例外を投げて呼び出し側で明示的に扱う。
    """


def fetch_html(url: str, *, timeout_ms: int = 60_000, settle_ms: int = 4_000) -> str:
    """URL を開いて render 後の HTML を返す。

    netkeiba は CloudFront 経由で HTTP 400 を返すことがあり、その場合
    Playwright は ~40 字の空 HTML を返す。これを silently 通すと「レースが
    見つからない」と誤認するので `NetkeibaBlocked` を投げる。
    """
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
    # 空 body 検出: netkeiba の CloudFront 400 は <body></body> 形式 (~40 字)
    stripped = html.strip()
    if len(stripped) < 80 and "<body></body>" in stripped.replace(" ", ""):
        raise NetkeibaBlocked(
            f"netkeiba returned empty body for {url} "
            f"(likely CloudFront 400; possibly IP rate-limited after recent heavy scraping)"
        )
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
    return f"https://{_host_for(race_id)}/race/shutuba.html?race_id={race_id}"


def shutuba_past_url(race_id: str) -> str:
    """馬柱 (各馬の直近 5 走) ページ URL。"""
    return f"https://{_host_for(race_id)}/race/shutuba_past.html?race_id={race_id}"


def odds_index_url(race_id: str, type_: str = "b8") -> str:
    """オッズ画面 URL。type は b1=単複 / b3=馬連 / b4=ワイド / b5=馬単 / b6=3連複 / b8=3連単 / b9=枠連。"""
    return f"https://{_host_for(race_id)}/odds/index.html?type={type_}&race_id={race_id}"


def odds_trifecta_url(race_id: str) -> str:
    """3 連単 (type=b8) の表示画面 URL。互換のため残置。"""
    return odds_index_url(race_id, "b8")


def odds_get_form_url(race_id: str, type_: str, *, jiku: int | None = None, housiki: str | None = None) -> str:
    """AJAX で各オッズ種の生 HTML を返す内部エンドポイント。

    type:
      b1 単複 / b3 馬連 / b4 ワイド / b5 馬単 / b6 3 連複 / b8 3 連単 / b9 枠連
    jiku:
      b8 (3 連単) で軸馬番を指定。指定がないと 1 着馬 1 の view のみ。
      b3/b4/b5/b6 でも一部のレースで axis 指定が効く (効かない場合は無視される)。
    """
    url = f"https://{_host_for(race_id)}/odds/odds_get_form.html?type={type_}&race_id={race_id}"
    if jiku is not None:
        url += f"&jiku={jiku}"
    if housiki:
        url += f"&housiki={housiki}"
    return url


def result_url(race_id: str) -> str:
    return f"https://{_host_for(race_id)}/race/result.html?race_id={race_id}"


def race_list_url(date_yyyymmdd: str, *, nar: bool = False) -> str:
    """開催一覧 URL。NAR/JRA はホストが違うので nar=True で切替。"""
    host = NAR_HOST if nar else JRA_HOST
    return f"https://{host}/top/race_list.html?kaisai_date={date_yyyymmdd}"


def fetch_trifecta_full(
    race_id: str,
    *,
    n_horses: int,
    settle_ms: int = 1500,
    timeout_ms: int = 60_000,
) -> list[str]:
    """3 連単オッズを全 1 着馬ぶん AJAX 取得して HTML 配列で返す。

    呼び出し側で `parse_trifecta_html_list` に渡して TrifectaOdds に変換する。
    n_horses は出走頭数 (取消含まず)。1..n_horses 全てに対して fetch する。
    """
    htmls: list[str] = []
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
        for jiku in range(1, n_horses + 1):
            url = odds_get_form_url(race_id, "b8", jiku=jiku)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            htmls.append(page.content())
        browser.close()
    return htmls


def fetch_odds_simple(
    race_id: str,
    type_: str,
    *,
    settle_ms: int = 1500,
    timeout_ms: int = 60_000,
) -> str:
    """1 fetch で全買い目が取れる odds page (b1 単複 / b3 馬連 / b4 ワイド) を取得。

    馬連・ワイドは netkeiba のデフォルト view で全 (i,j) ペアが表示される前提。
    馬単 (b5) / 3 連複 (b6) / 3 連単 (b8) は jiku 軸でビューが変わるため `fetch_odds_per_jiku` を使う。
    """
    if type_ not in ("b1", "b3", "b4"):
        raise ValueError(
            f"fetch_odds_simple は b1/b3/b4 のみ対応。{type_} は fetch_odds_per_jiku を使う。"
        )
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
        url = odds_get_form_url(race_id, type_)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(settle_ms)
        html = page.content()
        browser.close()
    return html


def fetch_odds_per_jiku(
    race_id: str,
    type_: str,
    *,
    n_horses: int,
    settle_ms: int = 1500,
    timeout_ms: int = 60_000,
) -> list[str]:
    """jiku iteration が必要な odds page (b5 馬単 / b6 3 連複 / b8 3 連単) を全軸馬ぶん取得。

    馬単は jiku=1 着馬、3 連複は jiku=軸馬 1 頭、3 連単は jiku=1 着馬。
    """
    if type_ not in ("b5", "b6", "b8"):
        raise ValueError(
            f"fetch_odds_per_jiku は b5/b6/b8 のみ対応。{type_} は fetch_odds_simple を使う。"
        )
    htmls: list[str] = []
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
        for jiku in range(1, n_horses + 1):
            url = odds_get_form_url(race_id, type_, jiku=jiku)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            htmls.append(page.content())
        browser.close()
    return htmls


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
