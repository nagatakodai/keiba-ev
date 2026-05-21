"""netkeiba HTML から RaceData を組み立てる。

入口:
  - `fetch_and_parse(race_url)` — URL から HTML を取得 → 出馬表 + 3 連単オッズを解析
  - `parse_shutuba(html)`       — 出馬表 HTML 単体
  - `parse_trifecta(html)`      — 3 連単オッズ HTML 単体
  - `parse_state(state)`        — 旧 WINTICKET 互換シム (`{"html": "..."}` を受ける)

netkeiba HTML の構造は変わりやすいので、parser は best-effort。
失敗した場合は warning を出して空フィールドを返す。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from .models import Horse, Race, RaceData, TrifectaOdds, Weather
from .scrape import (
    extract_race_id,
    fetch_html,
    odds_trifecta_url,
    result_url,
    shutuba_url,
)

# 場コード -> 競馬場名
VENUE_CODE = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}


def fetch_and_parse(url: str) -> RaceData:
    """shutuba / odds / racecard どの URL でも race_id を抜いて両方取得・統合する。"""
    race_id = extract_race_id(url)
    if not race_id:
        raise ValueError(f"race_id を抽出できません: {url}")
    shutuba_html = fetch_html(shutuba_url(race_id))
    odds_html = fetch_html(odds_trifecta_url(race_id))
    rd = parse_shutuba(shutuba_html, race_id=race_id)
    rd.trifecta = parse_trifecta(odds_html)
    return rd


def parse_state(state: dict[str, Any]) -> RaceData:
    """旧 WINTICKET 互換シム。`{"html": "..."}` を受け取って出馬表だけ解析。

    オッズは別途 `parse_trifecta` で取る必要がある。analyze.py が両方を呼ぶ。
    """
    html = state.get("html", "")
    if not html:
        raise ValueError("空の state を受け取りました")
    return parse_shutuba(html)


# --- 出馬表 (shutuba.html) ---

def parse_shutuba(html: str, *, race_id: str | None = None) -> RaceData:
    """出馬表 HTML から Race + Horse 一覧を組み立てる。"""
    soup = BeautifulSoup(html, "lxml")

    rid = race_id or _extract_race_id_from_dom(soup) or ""
    venue_name, schedule_index, race_number = _split_race_id(rid)

    title = (soup.select_one("title") or _empty()).get_text(strip=True)
    race_name = _text(soup, ".RaceName") or title

    # 距離 / 馬場 / 周回方向 / 天候 / 馬場状態 / 発走時刻 はサブヘッダ部分にまとめて入っている
    subhead = _text(soup, ".RaceData01") + " " + _text(soup, ".RaceData02")
    distance, surface, direction = _parse_distance_surface(subhead)
    weather_text = _parse_weather_text(subhead)
    start_at = _parse_start_at(subhead, race_id=rid)
    race_class = _parse_race_class(subhead, soup)

    horses = _parse_horse_table(soup)

    race = Race(
        cup_id=rid[:8] if rid else "",  # YYYYMMDD を cup_id 代わり (キャリブレ ID 用)
        schedule_index=schedule_index,
        race_number=race_number,
        venue_id=int(rid[8:10]) if rid and rid[8:10].isdigit() else 0,
        venue_name=venue_name,
        race_class=race_class or race_name,
        distance=distance,
        surface=surface,
        direction=direction,
        weather_text=weather_text,
        start_at=start_at,
        close_at=start_at,  # netkeiba は明示的な締切時刻なし。発走 = 締切扱い
        entries_number=len(horses),
        horses=horses,
        odds_updated_at=int(datetime.now().timestamp()),
        weather=None,
    )
    return RaceData(race=race, trifecta=[])


def _parse_horse_table(soup: BeautifulSoup) -> list[Horse]:
    """出馬表テーブルから Horse 一覧を抽出。

    netkeiba の `.Shutuba_Table` は列が固定:
      枠 / 馬番 / 印 / 馬名 / 性齢 / 斤量 / 騎手 / 厩舎 / 馬体重(増減) / オッズ / 人気
    クラス名 / 構造は時期で変わるので best-effort で複数 selector を試す。
    """
    horses: list[Horse] = []
    table = soup.select_one(".Shutuba_Table") or soup.select_one("table.RaceTable01")
    if not table:
        return horses

    rows = table.select("tr.HorseList") or table.select("tr")
    for row in rows:
        cells = row.find_all(["td"])
        if len(cells) < 8:
            continue
        bracket = _to_int(_cell_text(cells, [0]))
        number = _to_int(_cell_text(cells, [1]))
        if number == 0:
            continue

        # 馬名は <a> リンクで取れる
        name_a = row.select_one(".HorseInfo a, .Horse_Name a") or row.select_one("a[href*='/horse/']")
        name = name_a.get_text(strip=True) if name_a else _cell_text(cells, [3])

        # 性齢 / 斤量 / 騎手 / 厩舎 / 馬体重
        sex_age = _cell_text(cells, [4])
        weight_kg = _to_float(_cell_text(cells, [5]))
        jockey_a = row.select_one(".Jockey a") or row.select_one("a[href*='/jockey/']")
        jockey_name = jockey_a.get_text(strip=True) if jockey_a else _cell_text(cells, [6])
        trainer_a = row.select_one(".Trainer a") or row.select_one("a[href*='/trainer/']")
        trainer_name = trainer_a.get_text(strip=True) if trainer_a else _cell_text(cells, [7])

        # 馬体重 (例: "480(+2)" or "計不")
        body_text = _cell_text(cells, [8])
        body_weight, body_weight_diff = _parse_body_weight(body_text)

        # 単勝オッズ / 人気
        win_odds = 0.0
        # netkeiba の出馬表テーブルにはオッズ列 (Popular_Ninki / Txt_R / Popular) がある
        odds_text = _cell_text(cells, [9])
        win_odds = _to_float(odds_text)

        # 取消判定 (馬名行に「取消」クラスや背景色)
        absent = bool(row.select_one(".Cancel") or "取消" in row.get_text())

        href = name_a.get("href", "") if name_a else ""
        horse_id = ""
        m = re.search(r"/horse/(\d+)", href)
        if m:
            horse_id = m.group(1)

        jhref = jockey_a.get("href", "") if jockey_a else ""
        jid = ""
        m2 = re.search(r"/jockey/(?:result/recent/)?(\d+)", jhref)
        if m2:
            jid = m2.group(1)

        horses.append(
            Horse(
                number=number,
                name=name,
                bracket=bracket,
                sex_age=sex_age,
                weight_kg=weight_kg,
                body_weight=body_weight,
                body_weight_diff=body_weight_diff,
                jockey_name=jockey_name,
                jockey_id=jid,
                trainer_name=trainer_name,
                rating=0.0,  # netkeiba 出馬表に明示的なレートはなし (タイム指数等は別ページ)
                win_rate=0.0,
                quinella_rate=0.0,
                trio_rate=0.0,
                style="",
                win_odds=win_odds,
                absent=absent,
                horse_id=horse_id,
                interview_comment="",
            )
        )
    horses.sort(key=lambda h: h.number)
    return horses


# --- 3 連単オッズ (odds/index.html?type=b8) ---

def parse_trifecta(html: str) -> list[TrifectaOdds]:
    """3 連単オッズ HTML から TrifectaOdds 一覧を抽出。

    netkeiba の 3 連単オッズページは:
      1) 1 着馬を選択 → その馬を 1 着に固定した (b, c) のオッズ表が表示される構造
      2) JS で `<select id="list_select_horse">` を変えるごとにテーブル再描画

    Playwright で raw HTML を取ると、選択中の 1 着馬ぶんだけしか乗らない可能性が高い。
    そこで両方を試す:
      a) ページ内に `oddsData` (JS グローバル) として 1 着馬別の全オッズが埋め込まれて
         いるパターン → 全件取得
      b) DOM テーブル (tr.Odds_Td3) から見える分だけ抽出 → 1 着馬分のみ

    注意:
      netkeiba は odds ページに `<script>` で全オッズオブジェクトを埋めることが多い。
      `var Odds_Data = {...}` や `OddsData` を探して取れれば一発で全件パースできる。
    """
    out: list[TrifectaOdds] = []

    # (a) script タグから JSON-like なオッズ辞書を探す
    js_odds = _extract_odds_from_scripts(html)
    if js_odds:
        for (a, b, c), odds in js_odds.items():
            if odds <= 0:
                continue
            out.append(TrifectaOdds(key=(a, b, c), odds=odds, popularity=0))

    # (b) DOM フォールバック: tr.Odds_Td3 等のテーブルから抽出
    if not out:
        soup = BeautifulSoup(html, "lxml")
        for row in soup.select("tr.Odds_Td3, tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            key_text = _text_of(cells[0])
            odds_text = _text_of(cells[1])
            m = re.search(r"(\d+)\s*[\-→ー]\s*(\d+)\s*[\-→ー]\s*(\d+)", key_text)
            if not m:
                continue
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            odds = _to_float(odds_text)
            if odds <= 0:
                continue
            out.append(TrifectaOdds(key=(a, b, c), odds=odds, popularity=0))

    # 人気順 (popularity) を odds 昇順から付ける
    out.sort(key=lambda t: t.odds)
    for i, t in enumerate(out, 1):
        t.popularity = i

    return out


def _extract_odds_from_scripts(html: str) -> dict[tuple[int, int, int], float]:
    """script タグ内の JS オブジェクトから 3 連単オッズを抽出する。

    netkeiba がよく使うパターン:
      var Odds_Data = { "rateList": {...}, ... };
      var OddsData = {...};
      "1-2-3": "12.3"  形式の辞書
    複数パターンに対応するため、(a-b-c, value) 形式の reg-ex を直接適用する。
    """
    out: dict[tuple[int, int, int], float] = {}

    # 主要なパターン: `"1-2-3":"12.3"` (3 連単キー / 文字列値)
    for m in re.finditer(r'"(\d{1,2})-(\d{1,2})-(\d{1,2})"\s*:\s*"?([\d\.]+)"?', html):
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        odds = _to_float(m.group(4))
        if odds > 0:
            out[(a, b, c)] = odds

    return out


# --- 結果 (race/result.html) ---

def parse_result(html: str) -> dict | None:
    """結果 HTML から finish_order (上位 3 頭) と 3 連単払戻金を取り出す。

    返り値:
      { "finish_order": [a, b, c], "payout": int (100 円あたり払戻金), "source": "html" }
      or None (未確定 / パース失敗)
    """
    soup = BeautifulSoup(html, "lxml")

    # 1) 着順表 (table.ResultTableWrap / table.RaceTable01 等)
    table = soup.select_one("table.ResultTableWrap") or soup.select_one(".RaceTable01")
    if not table:
        return None
    finish_order: list[int] = []
    for row in table.select("tr.HorseList") or table.select("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        # 着順は cells[0]、馬番は cells[2] が典型
        order_text = _text_of(cells[0])
        num_text = _text_of(cells[2]) if len(cells) >= 3 else ""
        if order_text in ("1", "2", "3"):
            n = _to_int(num_text)
            if n:
                finish_order.append(n)
        if len(finish_order) >= 3:
            break

    if len(finish_order) < 3:
        return None

    # 2) 払戻金テーブルから 3 連単を探す
    payout = 0
    for row in soup.select("table.Payout_Detail_Table tr, table.PayoutTable tr"):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        head = _text_of(cells[0])
        if "三連単" in head or "3連単" in head:
            # 払戻金額 (例: "12,340円")
            for c in cells[1:]:
                m = re.search(r"([\d,]+)\s*円", _text_of(c))
                if m:
                    payout = int(m.group(1).replace(",", ""))
                    break
            break

    return {
        "finish_order": finish_order[:3],
        "payout": payout,
        "source": "netkeiba-html",
    }


# --- 開催一覧 (race_list.html?kaisai_date=...) ---

def parse_race_list(html: str, kaisai_date: str) -> list[dict]:
    """開催一覧 HTML からその日のレース (race_id, 場名, R 数, 発走時刻) 一覧を返す。"""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for a in soup.select("a[href*='shutuba.html?race_id=']"):
        href = a.get("href", "")
        m = re.search(r"race_id=(\d{12})", href)
        if not m:
            continue
        rid = m.group(1)
        venue_name, _, race_number = _split_race_id(rid)
        # 発走時刻 (e.g. "10:00") はリンク周辺のテキストから取る
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        tm = re.search(r"(\d{1,2}):(\d{2})", parent_text)
        start_at = 0
        if tm:
            now = datetime.now()
            try:
                dt = datetime(
                    int(kaisai_date[:4]), int(kaisai_date[4:6]), int(kaisai_date[6:8]),
                    int(tm.group(1)), int(tm.group(2)),
                )
                start_at = int(dt.timestamp())
            except (ValueError, OSError):
                pass
        out.append({
            "race_id": rid,
            "venue": venue_name,
            "race_no": race_number,
            "start_at": start_at,
            "url": shutuba_url(rid),
        })
    # 重複除去
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in out:
        if r["race_id"] in seen:
            continue
        seen.add(r["race_id"])
        uniq.append(r)
    return uniq


# --- 小道具 ---

def _split_race_id(rid: str) -> tuple[str, int, int]:
    """race_id (YYYYMMDDPP00RR) を 場名 / 開催日連番 / R 数 に分解。

    schedule_index は本来「開催何日目」だが、netkeiba ID には含まれない。
    暫定で日付 MMDD を整数として使う。キャリブレ join は race_id 文字列で行うので
    厳密性は不要。
    """
    if not rid or len(rid) != 12:
        return ("", 0, 0)
    code = rid[8:10]
    venue = VENUE_CODE.get(code, f"場{code}")
    schedule_index = int(rid[4:8]) if rid[4:8].isdigit() else 0  # MMDD
    race_number = int(rid[10:12]) if rid[10:12].isdigit() else 0
    return (venue, schedule_index, race_number)


def _extract_race_id_from_dom(soup: BeautifulSoup) -> str | None:
    for a in soup.select("a[href*='race_id=']"):
        m = re.search(r"race_id=(\d{12})", a.get("href", ""))
        if m:
            return m.group(1)
    canonical = soup.select_one("link[rel='canonical']")
    if canonical:
        m = re.search(r"race_id=(\d{12})", canonical.get("href", ""))
        if m:
            return m.group(1)
    return None


_DISTANCE_RE = re.compile(r"(芝|ダ|ダート|障)\s*(\d{3,5})\s*m", re.IGNORECASE)
_DIRECTION_RE = re.compile(r"(右|左|直線)")
_START_RE = re.compile(r"発走\s*[:：]?\s*(\d{1,2}):(\d{2})")
_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _parse_distance_surface(text: str) -> tuple[int, str, str]:
    m = _DISTANCE_RE.search(text)
    if not m:
        return (0, "", "")
    surface_raw = m.group(1)
    surface = {"芝": "芝", "ダ": "ダート", "ダート": "ダート", "障": "障害"}.get(surface_raw, surface_raw)
    distance = int(m.group(2))
    md = _DIRECTION_RE.search(text)
    direction = md.group(1) if md else ""
    return (distance, surface, direction)


def _parse_weather_text(text: str) -> str:
    m = re.search(r"天候\s*[:：]?\s*([^\s/]+)\s*/?\s*(?:馬場\s*[:：]?\s*([^\s]+))?", text)
    if not m:
        return ""
    weather = m.group(1) or ""
    track = m.group(2) or ""
    if weather and track:
        return f"{weather} / {track}"
    return weather or track


def _parse_start_at(text: str, *, race_id: str = "") -> int:
    sm = _START_RE.search(text)
    if not sm:
        return 0
    h, mi = int(sm.group(1)), int(sm.group(2))
    # 日付は race_id (YYYYMMDD) から取れれば最優先
    if race_id and len(race_id) >= 8:
        try:
            d = datetime(int(race_id[:4]), int(race_id[4:6]), int(race_id[6:8]), h, mi)
            return int(d.timestamp())
        except (ValueError, OSError):
            pass
    dm = _DATE_RE.search(text)
    if dm:
        try:
            d = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)), h, mi)
            return int(d.timestamp())
        except (ValueError, OSError):
            pass
    return 0


def _parse_race_class(text: str, soup: BeautifulSoup) -> str:
    # ".RaceData02" にクラス情報 (例: "サラ系3歳上 / G1 / 18頭") が入る
    for m in re.finditer(r"(G[1-3]|JpnG?[1-3]|L|OP|3勝クラス|2勝クラス|1勝クラス|新馬|未勝利|オープン)", text):
        return m.group(1)
    title = (soup.select_one(".RaceName") or _empty()).get_text(strip=True)
    return title


def _parse_body_weight(text: str) -> tuple[int, int]:
    if not text or "計不" in text:
        return (0, 0)
    m = re.match(r"\s*(\d+)\s*\(?([+\-]?\d+)?\)?", text)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)


def _empty() -> Tag:
    return BeautifulSoup("<x/>", "lxml").x  # type: ignore[return-value]


def _text(soup: BeautifulSoup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _text_of(el) -> str:
    if el is None:
        return ""
    return el.get_text(" ", strip=True)


def _cell_text(cells: list, indices: list[int]) -> str:
    for i in indices:
        if i < len(cells):
            t = _text_of(cells[i])
            if t:
                return t
    return ""


def _to_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(v).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0


def load_from_file(path: Path) -> RaceData:
    """ファイルから読み込む。`.html` なら HTML として、`.json` (state) なら state として。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".html":
        return parse_shutuba(text)
    state = json.loads(text)
    if "html" in state:
        return parse_shutuba(state["html"])
    raise ValueError(f"未対応のファイル形式: {p}")
