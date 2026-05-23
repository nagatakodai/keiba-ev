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
    """keibalab.jp で指定日の race list (race_id + 発走時刻) を取得。

    URL: https://keibalab.jp/db/race/<YYYYMMDD>/
    各 race row の <td> に race_id link と HH:MM 発走時刻が含まれる。
    """
    from datetime import time as _time
    target_date = yyyymmdd or datetime.now().strftime("%Y%m%d")
    html = _http_get(f"https://keibalab.jp/db/race/{target_date}/")

    # 各 <tr> ブロックを舐めて、race_id と HH:MM を 1 ペアずつ取り出す
    out: list[AltRace] = []
    tr_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    rid_pattern = re.compile(r'/db/race/(\d{12})/')
    time_pattern = re.compile(r'>(\d{1,2}):(\d{2})<')

    for tr_html in tr_pattern.findall(html):
        rid_match = rid_pattern.search(tr_html)
        if not rid_match:
            continue
        rid = rid_match.group(1)
        time_match = time_pattern.search(tr_html)
        if not time_match:
            continue
        hh = int(time_match.group(1))
        mm = int(time_match.group(2))
        # target_date + HH:MM → JST unix timestamp
        dt = datetime.strptime(target_date, "%Y%m%d").replace(hour=hh, minute=mm)
        # naive JST as local time
        start_at = int(dt.timestamp())
        url = f"https://race.netkeiba.com/race/shutuba.html?race_id={rid}"
        out.append(AltRace(
            race_id=rid,
            venue="",  # 場名は per-race fetch で得る (現状未取得)
            race_no=int(rid[-2:]),
            start_at=start_at,
            url=url,
            source="keibalab",
        ))
    # uniq by race_id (table 重複がある場合)
    seen = set()
    uniq: list[AltRace] = []
    for r in out:
        if r.race_id in seen:
            continue
        seen.add(r.race_id)
        uniq.append(r)
    return uniq


def is_alt_available() -> bool:
    """keibalab が応答するかを quick check する (auto_watch の fallback 判定用)。"""
    try:
        _http_get("https://keibalab.jp/", timeout=5)
        return True
    except Exception:
        return False
