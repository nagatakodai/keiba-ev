"""代替データソースからの race list / 出馬表 取得。

netkeiba の race.* / nar.* / db.* サブドメインが CloudFront に block された時、
本モジュール経由で他サイトから race info を取得する。

## サイト別 accessibility (本日 2026-05-23 調査時点)

| サイト                 | 状態           | 利用可能 data                 |
|------------------------|---------------|-------------------------------|
| www.netkeiba.com       | ✅ 200 OK     | ニュース・記事中心             |
| race.netkeiba.com      | ❌ 400 (block) | (block 解除後 fresh odds)     |
| nar.netkeiba.com       | ❌ 400 (block) | (block 解除後 NAR odds)        |
| db.netkeiba.com        | ❌ 400 (block) | (block 解除後 過去 race)       |
| **keibalab.jp**        | ✅ 200 OK     | race list, shutuba, 結果      |
| www.jra.go.jp          | ✅ 200 OK     | JRA 公式 (Shift_JIS, 解析難)  |
| sports.yahoo.co.jp     | ✅ 200 OK     | JS render 必須                 |

実装方針:
- **keibalab.jp が最も実用的** (race_id 形式 = netkeiba と同じ 12 桁、JS 不要、
  発走時刻 / 開催場 / 出走馬リンクが静的 HTML に含まれる)
- live odds の代替は無い (keibalab の odds page は JS render が必要、しかも
  3 連単の per-jiku 単位は 提供無し)。**block 中は live betting 不能** が原則。
- 本モジュールは "race list 取得" と "shutuba 構造" の代替のみ提供する。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.request import Request, urlopen

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


@dataclass
class AltRace:
    """代替ソース由来の race info (auto_watch / 列挙 用)。"""
    race_id: str         # 12 桁 (netkeiba と同形式)
    venue: str           # 開催場名
    race_no: int         # R 数
    start_at: int        # 発走 unix
    url: str             # netkeiba style URL (block 解除後の再取得用)
    source: str          # "keibalab"


def _http_get(url: str, *, timeout: int = 15) -> str:
    """Playwright 不要の軽量 GET (keibalab は静的 HTML なので urllib で十分)。"""
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_race_list_keibalab(yyyymmdd: str | None = None) -> list[AltRace]:
    """keibalab.jp 当日 race list を取得。

    URL: https://keibalab.jp/db/race/
    HTML 内に `/db/race/<rid>/` 形式の link が並び、発走時刻 + 場名が
    table 構造で含まれる。
    """
    html = _http_get("https://keibalab.jp/db/race/")
    # 当日 = `/db/race/` の default 表示が today's list
    # race_id は <a href="/db/race/202605230401/"> 形式
    rids = re.findall(r'href="/db/race/(\d{12})/"', html)
    rids = sorted(set(rids))  # uniq + sort

    # 発走時刻と場名は table row 単位で並んでいる。同じ順序で抽出する。
    # 完璧なパーサは難しいので、まず race_id だけ返して、必要なら呼び出し側で
    # /db/race/<rid>/ を個別 fetch して詳細を取る方針。
    today = yyyymmdd or datetime.now().strftime("%Y%m%d")
    out: list[AltRace] = []
    for rid in rids:
        if not rid.startswith(today[:8]):
            continue
        # netkeiba style URL を構築 (block 解除後の再取得用)
        url = f"https://race.netkeiba.com/race/shutuba.html?race_id={rid}"
        out.append(AltRace(
            race_id=rid,
            venue="",       # 後段の per-race fetch で埋める
            race_no=int(rid[-2:]),
            start_at=0,     # 同上
            url=url,
            source="keibalab",
        ))
    return out


def is_alt_available() -> bool:
    """keibalab が応答するかを quick check する (auto_watch の fallback 判定用)。"""
    try:
        _http_get("https://keibalab.jp/", timeout=5)
        return True
    except Exception:
        return False
