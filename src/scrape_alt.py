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


def fetch_race_list_keibabook(yyyymmdd: str | None = None) -> list[dict]:
    """競馬ブック中央 TOP から指定日 (既定: 当日) の JRA race 発走時刻を取得。

    URL: https://p.keibabook.co.jp/cyuou/top
    返り値: [{"venue": "東京", "race_no": 1, "start_at": <unix>, "date": "YYYYMMDD"}, ...]

    keibabook の race_id 形式 (YYYY<kk><場code><日目><R>) は netkeiba と別 namespace
    なので **URL からの race_id 直接変換はしない**。場名と R を抽出して呼出側で
    `scrape_jra.discover_jra_races()` の (venue_code → venue_name) と join する
    (auto_watch._list_due_races の JRA 部分)。

    静的 HTML 構造 (実機確認 2026-05-29):
      <li class="active" style="<yyyymmdd>">  ← 当日 tab
      <div class="active" style="<yyyymmdd>">  ← 当日 content
        <th colspan="3" class="midasi green">N回<場名>M日目</th>  ← 場 section header
        <tr>...<p class="raceno">XR</p>...<td>...HH:MM</td></tr>  ← 各レース
    """
    target_date = yyyymmdd or datetime.now().strftime("%Y%m%d")
    html = _http_get("https://p.keibabook.co.jp/cyuou/top")
    # 当日 div は `style="<yyyymmdd>"` を持つ (id="tab_menu-box" 内、最初のみ active)
    # 当日が掲載されていない (前日 etc) 場合は空 (フォールバック先で判定)
    # 日付 div は `<div class="active|" style="<yyyymmdd>">` の形 (active は当日のみ、
    # 他の日 (週末両日 等) は class="" だが style に日付が入る)。当日の div を style 一致
    # で探し、次の日付 div か tab_menu-box 末尾 まで切り出す。
    target_div = re.search(
        rf'<div\s+class="[^"]*"\s+style="{target_date}">(.*?)(?=<div\s+class="[^"]*"\s+style="\d{{8}}">|<div\s+class="midasi_sub">)',
        html, re.DOTALL,
    )
    if not target_div:
        return []
    body = target_div.group(1)
    # 場 section (th class="midasi green") を順に walk
    sec_re = re.compile(
        r'<th\s+colspan="3"\s+class="midasi green">(\d+)回([^\d<]+?)\s*(\d+)日目</th>(.*?)'
        r'(?=<th\s+colspan="3"\s+class="midasi green">|</tbody>\s*</table>)',
        re.DOTALL,
    )
    race_re = re.compile(
        r'<p\s+class="raceno">(\d+)R</p>.*?<td>(.*?)</td>',
        re.DOTALL,
    )
    time_re = re.compile(r'(\d{1,2}):(\d{2})')

    out: list[dict] = []
    for sec in sec_re.finditer(body):
        venue = sec.group(2).strip()
        sec_body = sec.group(4)
        for rm in race_re.finditer(sec_body):
            race_no = int(rm.group(1))
            cell = rm.group(2)
            tm = time_re.search(cell)
            if not tm:
                continue
            hh, mm = int(tm.group(1)), int(tm.group(2))
            try:
                dt = datetime.strptime(target_date, "%Y%m%d").replace(hour=hh, minute=mm)
                start_at = int(dt.timestamp())
            except ValueError:
                continue
            out.append({
                "venue": venue,
                "race_no": race_no,
                "start_at": start_at,
                "date": target_date,
            })
    return out
