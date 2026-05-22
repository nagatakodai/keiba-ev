"""netkeiba HTML から RaceData を組み立てる。

入口:
  - `fetch_and_parse(race_url)` — URL から HTML を取得 → 出馬表 + 3 連単オッズを解析
  - `parse_shutuba(html)`       — 出馬表 HTML 単体
  - `parse_trifecta(html)`      — 3 連単オッズ HTML 単体 (1 jiku 分)
  - `parse_trifecta_multi(htmls)` — 複数 jiku の HTML をまとめてマージ
  - `parse_tanfuku(html)`       — 単勝・複勝
  - `parse_pair_odds(html, t)`  — 馬連 (b3) / ワイド (b4) / 馬単 (b5)
  - `parse_trio(html)`          — 3 連複 (b6)
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

from .models import BetOdds, Horse, PastRun, Race, RaceData, TrifectaOdds, Weather
from .scrape import (
    extract_race_id,
    fetch_html,
    fetch_odds_per_jiku,
    fetch_odds_simple,
    fetch_trifecta_full,
    is_nar_race_id,
    odds_index_url,
    odds_trifecta_url,
    result_url,
    shutuba_past_url,
    shutuba_url,
)

# 場コード -> 競馬場名 (JRA + NAR)
VENUE_CODE = {
    # JRA 中央
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
    # NAR 地方
    "30": "門別", "35": "盛岡", "36": "水沢",
    "42": "浦和", "43": "船橋", "44": "大井", "45": "川崎",
    "46": "金沢", "47": "笠松", "48": "名古屋",
    "50": "園田", "51": "姫路",
    "54": "高知", "55": "佐賀",
    "65": "帯広",  # ばんえい
}


def fetch_and_parse(
    url: str,
    *,
    with_past_runs: bool = True,
    with_other_bets: bool = True,
    with_exacta: bool = False,
    with_trio: bool = False,
) -> RaceData:
    """shutuba / odds / racecard どの URL でも race_id を抜いて両方取得・統合する。

    NAR/JRA は race_id の場コードから自動判定。3 連単オッズは AJAX エンドポイントから
    1 着馬ごとに取得してマージする (NAR/JRA 共通)。

    with_past_runs=True で馬柱 (shutuba_past.html) も取得して各 Horse に注入。
    Layer 1 特徴量を使う場合に必須。失敗時は warning のみで past_runs=[] のまま続行。

    with_other_bets=True で 単複 (b1) / 馬連 (b3) / ワイド (b4) を追加 fetch (各 1 page = 計 3 page)。
    with_exacta=True で 馬単 (b5) を jiku iteration で全取得 (= n_horses page 増、重い)。
    with_trio=True で 3 連複 (b6) を jiku iteration で全取得 (= n_horses page 増、重い)。
    取得失敗時は warning のみで rd.other_bets[type] は空のまま続行。
    """
    race_id = extract_race_id(url)
    if not race_id:
        raise ValueError(f"race_id を抽出できません: {url}")
    shutuba_html = fetch_html(shutuba_url(race_id))
    rd = parse_shutuba(shutuba_html, race_id=race_id)

    if with_past_runs:
        try:
            past_html = fetch_html(shutuba_past_url(race_id))
            runs_by_horse = parse_past_runs(past_html)
            for h in rd.race.horses:
                h.past_runs = runs_by_horse.get(h.number, [])
        except Exception as ex:  # noqa: BLE001
            import sys
            print(f"[fetch_and_parse] past runs fetch/parse failed: {ex}", file=sys.stderr)

    n_horses = len([h for h in rd.race.horses if not h.absent])
    if n_horses >= 3:
        htmls = fetch_trifecta_full(race_id, n_horses=n_horses)
        rd.trifecta = parse_trifecta_multi(htmls)
    else:
        rd.trifecta = []

    if with_other_bets and n_horses >= 1:
        # b1 (単勝・複勝) は 1 fetch で両方取れる
        try:
            html = fetch_odds_simple(race_id, "b1")
            tanfuku = parse_tanfuku(html)
            wins, places = _tanfuku_to_bets(tanfuku)
            if wins:
                rd.other_bets["win"] = wins
            if places:
                rd.other_bets["place"] = places
        except Exception as ex:  # noqa: BLE001
            import sys
            print(f"[fetch_and_parse] win/place fetch/parse failed: {ex}", file=sys.stderr)
    if with_other_bets and n_horses >= 2:
        for type_, name in (("b3", "quinella"), ("b4", "wide")):
            try:
                html = fetch_odds_simple(race_id, type_)
                pair = parse_pair_odds(html, type_)
                rd.other_bets[name] = _pair_dict_to_bets(pair, name)
            except Exception as ex:  # noqa: BLE001
                import sys
                print(f"[fetch_and_parse] {name} fetch/parse failed: {ex}", file=sys.stderr)
    if with_exacta and n_horses >= 2:
        # 馬単 (b5) は jiku iteration が必要 (3 連単と同形) で重い
        try:
            htmls = fetch_odds_per_jiku(race_id, "b5", n_horses=n_horses)
            merged_ex: dict[tuple[int, int], float] = {}
            for h in htmls:
                merged_ex.update(parse_pair_odds(h, "b5"))
            if merged_ex:
                rd.other_bets["exacta"] = _exacta_dict_to_bets(merged_ex)
        except Exception as ex:  # noqa: BLE001
            import sys
            print(f"[fetch_and_parse] exacta fetch/parse failed: {ex}", file=sys.stderr)
    if with_trio and n_horses >= 3:
        try:
            htmls = fetch_odds_per_jiku(race_id, "b6", n_horses=n_horses)
            merged: dict[tuple[int, int, int], float] = {}
            for h in htmls:
                merged.update(parse_trio(h))
            rd.other_bets["trio"] = _trio_dict_to_bets(merged)
        except Exception as ex:  # noqa: BLE001
            import sys
            print(f"[fetch_and_parse] trio fetch/parse failed: {ex}", file=sys.stderr)
    return rd


def _pair_dict_to_bets(d: dict[tuple[int, int], float], bet_type: str) -> list[BetOdds]:
    """{(a,b): odds} → list[BetOdds]。odds 昇順で popularity を 1..N に振る。"""
    items = sorted(d.items(), key=lambda kv: kv[1])
    out = []
    for i, ((a, b), odds) in enumerate(items, 1):
        out.append(BetOdds(bet_type=bet_type, key=(a, b), odds=odds, popularity=i))
    return out


def _exacta_dict_to_bets(d: dict[tuple[int, int], float]) -> list[BetOdds]:
    """馬単 (順序あり) の dict → list[BetOdds]。"""
    items = sorted(d.items(), key=lambda kv: kv[1])
    out = []
    for i, ((a, b), odds) in enumerate(items, 1):
        out.append(BetOdds(bet_type="exacta", key=(a, b), odds=odds, popularity=i))
    return out


def _trio_dict_to_bets(d: dict[tuple[int, int, int], float]) -> list[BetOdds]:
    items = sorted(d.items(), key=lambda kv: kv[1])
    out = []
    for i, ((a, b, c), odds) in enumerate(items, 1):
        out.append(BetOdds(bet_type="trio", key=(a, b, c), odds=odds, popularity=i))
    return out


def _tanfuku_to_bets(
    d: dict[int, dict[str, float]],
) -> tuple[list[BetOdds], list[BetOdds]]:
    """単複 HTML パース結果 → (win [単勝] list, place [複勝] list)。

    複勝オッズは {fuku_min, fuku_max} の範囲で来る。EV 計算では保守的に下限
    (fuku_min) を採用 (実際の払戻が下限以上で確定するため最悪ケース)。
    odds 昇順で popularity を振る。
    """
    win_items: list[tuple[int, float]] = []
    place_items: list[tuple[int, float]] = []
    for num, info in d.items():
        tan = info.get("tan")
        if tan and tan > 0:
            win_items.append((num, tan))
        fuku_min = info.get("fuku_min")
        if fuku_min and fuku_min > 0:
            place_items.append((num, fuku_min))
    win_items.sort(key=lambda kv: kv[1])
    place_items.sort(key=lambda kv: kv[1])
    wins = [
        BetOdds(bet_type="win", key=(num,), odds=odds, popularity=i)
        for i, (num, odds) in enumerate(win_items, 1)
    ]
    places = [
        BetOdds(bet_type="place", key=(num,), odds=odds, popularity=i)
        for i, (num, odds) in enumerate(place_items, 1)
    ]
    return wins, places


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
    venue_name, schedule_index, race_number, cup_id = _split_race_id(rid)

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
        cup_id=cup_id,
        schedule_index=schedule_index,
        race_number=race_number,
        venue_id=int(rid[4:6]) if rid and rid[4:6].isdigit() else 0,
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


def _select_shutuba_table(soup: BeautifulSoup) -> Tag | None:
    """出馬表テーブルを拾う。

    netkeiba は `Shutuba_Table` というクラスが「実テーブル」と「予想ラップ表
    (PredictRap_Table)」両方に付くため、PredictRap を除外する必要がある。
    JRA/NAR 共通で `ShutubaTable` (アンダーバー無し) が実テーブルに付くので優先。
    """
    # 1) ShutubaTable (no underscore) AND not PredictRap
    for t in soup.select("table.ShutubaTable"):
        cls = t.get("class", []) or []
        if "PredictRap_Table" in cls:
            continue
        return t
    # 2) Shutuba_Table (with underscore) AND not PredictRap (legacy JRA fallback)
    for t in soup.select("table.Shutuba_Table"):
        cls = t.get("class", []) or []
        if "PredictRap_Table" in cls:
            continue
        return t
    # 3) RaceTable01 最終フォールバック
    return soup.select_one("table.RaceTable01")


def _parse_horse_table(soup: BeautifulSoup) -> list[Horse]:
    """出馬表テーブルから Horse 一覧を抽出。

    netkeiba の `.ShutubaTable` は列が固定:
      枠 / 馬番 / 印 / 馬名 / 性齢 / 斤量 / 騎手 / 厩舎 / 馬体重(増減) / オッズ / 人気
    クラス名 / 構造は時期で変わるので best-effort で複数 selector を試す。
    """
    horses: list[Horse] = []
    table = _select_shutuba_table(soup)
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
        win_odds = _to_float(_cell_text(cells, [9]))

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


# --- 3 連単オッズ (odds/odds_get_form.html?type=b8) ---

# 現代の netkeiba (NAR) は cart-item 属性に odds 値を直接持つ:
#   <td class="Odds" cart-item="a5-44-11_b8_c0_1_2_3">8,761.7</td>
_CART_ITEM_B8_NAR_RE = re.compile(
    r'cart-item="a\d+-\d+-\d+_b8_c\d+_(\d+)_(\d+)_(\d+)"[^>]*>\s*([\d,\.]+)'
)
# JRA は cart-item の中に <span id="odds-8-XXXXXX"> で odds が入る:
#   <td class="Odds" cart-item="a7-5-11_b8_c0_1_2_3"><span id="odds-8-010203">12.3</span></td>
# XXXXXX は (1着, 2着, 3着) を 2 桁ずつ ZeroPad した文字列。終了レースは ---.- が入る。
_CART_ITEM_B8_JRA_RE = re.compile(
    r'cart-item="a\d+-\d+-\d+_b8_c\d+_(\d+)_(\d+)_(\d+)"[^>]*>'
    r'\s*(?:<input[^>]*>\s*)?<span[^>]*id="odds-8-\d{6}"[^>]*>([\d,\.]+)'
)
# 旧形式 (script タグ内の embedded JSON) もフォールバックで残す。
_EMBED_JSON_B8_RE = re.compile(r'"(\d{1,2})-(\d{1,2})-(\d{1,2})"\s*:\s*"?([\d\.]+)"?')
# 馬連・ワイド・馬単共通形式: `..._<b3|b4|b5>_c\d+_<a>_<b>`
# (馬連・ワイド は順不同、馬単は順序あり)


def parse_trifecta(html: str) -> list[TrifectaOdds]:
    """3 連単オッズ HTML から TrifectaOdds 一覧を抽出 (1 jiku 分)。"""
    return parse_trifecta_multi([html])


def parse_trifecta_multi(htmls: list[str]) -> list[TrifectaOdds]:
    """複数の HTML (各 jiku の AJAX 応答) をマージして TrifectaOdds 全件を返す。"""
    combined: dict[tuple[int, int, int], float] = {}

    for html in htmls:
        # (a) NAR 形式: cart-item の td 内に odds 値が直接 (例: ">8,761.7")
        for m in _CART_ITEM_B8_NAR_RE.finditer(html):
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            odds = _to_float(m.group(4))
            if odds > 0 and a != b and a != c and b != c:
                combined.setdefault((a, b, c), odds)

        # (a') JRA 形式: cart-item の td 内に <span id="odds-8-XXXXXX"> で odds 値
        for m in _CART_ITEM_B8_JRA_RE.finditer(html):
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            odds = _to_float(m.group(4))
            if odds > 0 and a != b and a != c and b != c:
                combined.setdefault((a, b, c), odds)

        # (b) 旧形式: script タグ内の embedded JSON ("a-b-c": "odds")
        if not combined:
            for m in _EMBED_JSON_B8_RE.finditer(html):
                a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
                odds = _to_float(m.group(4))
                if odds > 0 and a != b and a != c and b != c:
                    combined.setdefault((a, b, c), odds)

        # (c) DOM 最終フォールバック: tr.Odds_Td3 等のテーブル
        if not combined:
            soup = BeautifulSoup(html, "lxml")
            for row in soup.select("tr.Odds_Td3, tbody tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                key_text = _text_of(cells[0])
                odds_text = _text_of(cells[1])
                mk = re.search(r"(\d+)\s*[\-→ー]\s*(\d+)\s*[\-→ー]\s*(\d+)", key_text)
                if not mk:
                    continue
                a, b, c = int(mk.group(1)), int(mk.group(2)), int(mk.group(3))
                odds = _to_float(odds_text)
                if odds > 0:
                    combined.setdefault((a, b, c), odds)

    out = [TrifectaOdds(key=k, odds=v, popularity=0) for k, v in combined.items()]
    out.sort(key=lambda t: t.odds)
    for i, t in enumerate(out, 1):
        t.popularity = i
    return out


# --- 単勝・複勝 (b1) ---

def parse_tanfuku(html: str) -> dict[int, dict[str, float]]:
    """単勝・複勝オッズ HTML から { 馬番: {tan, fuku_min, fuku_max} } を抽出。

    netkeiba の b1 HTML 構造:
      - `id="odds_tan_block"` (単勝ブロック): 最終 td 内に単一の odds (例: "19.4"
        or `<span class="Odds">19.4</span>`)
      - `id="odds_fuku_block"` (複勝ブロック): 最終 td 内に "3.9 - 6.6" のような
        範囲テキスト (min - max) または `span.Odds.Min` / `span.Odds.Max`
    """
    soup = BeautifulSoup(html, "lxml")
    out: dict[int, dict[str, float]] = {}

    tan_block = soup.select_one("#odds_tan_block")
    if tan_block:
        for row in tan_block.select("tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            num = _to_int(_text_of(cells[1]))
            if num == 0:
                continue
            tan = _extract_first_number(_text_of(cells[-1]))
            if tan > 0:
                out.setdefault(num, {})["tan"] = tan

    fuku_block = soup.select_one("#odds_fuku_block")
    if fuku_block:
        for row in fuku_block.select("tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            num = _to_int(_text_of(cells[1]))
            if num == 0:
                continue
            txt = _text_of(cells[-1])
            vals = [_to_float(x) for x in re.findall(r"[\d,]+\.?\d*", txt)]
            vals = [v for v in vals if v > 0]
            if vals:
                out.setdefault(num, {})["fuku_min"] = min(vals)
                out.setdefault(num, {})["fuku_max"] = max(vals)
    return out


def _extract_first_number(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"[\d,]+\.?\d*", text)
    return _to_float(m.group(0)) if m else 0.0


# --- 馬連 (b3) / ワイド (b4) / 馬単 (b5) ---

def parse_pair_odds(html: str, type_: str) -> dict[tuple[int, int], float]:
    """馬連 (b3) / ワイド (b4) / 馬単 (b5) の HTML を { (a,b): odds } で返す。

    馬連・ワイドは無順 (a < b)、馬単は順序あり。
    """
    if type_ not in ("b3", "b4", "b5"):
        raise ValueError(f"unsupported pair odds type: {type_}")
    pat = re.compile(
        rf'cart-item="a\d+-\d+-\d+_{type_}_c\d+_(\d+)_(\d+)"[^>]*>\s*([\d,\.]+)'
    )
    out: dict[tuple[int, int], float] = {}
    for m in pat.finditer(html):
        a, b = int(m.group(1)), int(m.group(2))
        if a == b:
            continue
        odds = _to_float(m.group(3))
        if odds <= 0:
            continue
        key = (a, b) if type_ == "b5" else (min(a, b), max(a, b))
        out.setdefault(key, odds)
    return out


# --- 3 連複 (b6) ---

def parse_trio(html: str) -> dict[tuple[int, int, int], float]:
    """3 連複 (b6) の HTML を { (a,b,c) 昇順タプル: odds } で返す。

    注: netkeiba の b6 デフォルト view は (axis, partner) の 2 次元行列を出すだけで
    3 連複の全件 (14C3 = 364 etc.) は返らない。フォーメーション view では cart-item
    が 3 桁形式 `_b6_c\\d+_a_b_c` を吐く想定なのでそのパターンを試し、なければ
    空 dict を返す (デフォルト view では本関数で全件取得できない)。
    """
    pat3 = re.compile(
        r'cart-item="a\d+-\d+-\d+_b6_c\d+_(\d+)_(\d+)_(\d+)"[^>]*>\s*([\d,\.]+)'
    )
    out: dict[tuple[int, int, int], float] = {}
    for m in pat3.finditer(html):
        nums = sorted({int(m.group(1)), int(m.group(2)), int(m.group(3))})
        if len(nums) != 3:
            continue
        odds = _to_float(m.group(4))
        if odds <= 0:
            continue
        out.setdefault(tuple(nums), odds)
    return out


# --- 馬柱 (shutuba_past.html) ---


_TIME_HMS_RE = re.compile(r"(\d+):(\d{2})(?:\.(\d))?")
_PAST_DIST_RE = re.compile(r"(芝|ダ|障)(\d{3,5})")
_PAST_DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
_PAST_FIELDSIZE_RE = re.compile(r"(\d+)頭")
_PAST_UMABAN_RE = re.compile(r"(\d+)番")
_PAST_NINKI_RE = re.compile(r"(\d+)人")
_PAST_LAST3F_RE = re.compile(r"\(([\d\.]+)\)")
_PAST_BODY_RE = re.compile(r"(\d{3})\s*\(([+\-]?\d+)\)")
_PAST_TIMEDIFF_RE = re.compile(r"\(([+\-]?[\d\.]+)\)")


def _parse_time_to_sec(text: str) -> float:
    """`1:12.0` (M:SS.D) → 72.0 秒。"""
    m = _TIME_HMS_RE.search(text)
    if not m:
        return 0.0
    mins = int(m.group(1))
    secs = int(m.group(2))
    dec = int(m.group(3)) if m.group(3) else 0
    return mins * 60 + secs + dec / 10.0


def parse_past_runs(html: str) -> dict[int, list[PastRun]]:
    """馬柱 HTML から `{ 馬番: [PastRun, ...] }` を返す。

    各 `tr.HorseList` に `td.Past` が最大 5 個ぶら下がっており、それぞれ 1 走分。
    `Ranking_1/2/3` クラスで 1-3 着判定。4 着以下は finish_pos=None で返す。
    Data05 のタイムは「勝ち馬タイム」、Data07 の括弧内は「当該馬との時間差」(+=遅れ)。
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.Shutuba_Past5_Table") or soup.select_one("table.Shutuba_Past_Table")
    if not table:
        return {}

    out: dict[int, list[PastRun]] = {}
    for row in table.select("tr.HorseList"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        # 馬番は cells[0] or [1] (Waku の隣)
        umaban = _to_int(_cell_text(cells, [1, 0]))
        if umaban == 0:
            continue
        runs: list[PastRun] = []
        for c in cells:
            cls = c.get("class") or []
            if "Past" not in cls:
                continue
            d_item = c.select_one(".Data_Item")
            if not d_item:
                continue
            run = _parse_one_past_run(d_item, ranking_cls=cls)
            if run is None:
                continue
            runs.append(run)
        if runs:
            out[umaban] = runs
    return out


def _parse_one_past_run(d_item: Tag, *, ranking_cls: list[str]) -> PastRun | None:
    data01 = _text(d_item, ".Data01")
    if not data01:
        return None

    # 日付 + 場 + R 数
    date = ""
    venue = ""
    race_no = 0
    md = _PAST_DATE_RE.search(data01)
    if md:
        date = md.group(0)
    # date 以降が場名と R 数。`<span>2026.04.11 中山</span><span class="Num">14</span>`
    venue_span = d_item.select_one(".Data01 span:not(.Num)")
    if venue_span:
        vt = venue_span.get_text(" ", strip=True)
        # vt の末尾が場名
        parts = vt.split()
        if len(parts) >= 2:
            venue = parts[-1]
    num_span = d_item.select_one(".Data01 .Num")
    if num_span:
        race_no = _to_int(_text_of(num_span))

    # クラス + 過去 race_id
    a_class = d_item.select_one(".Data02 a")
    race_class = ""
    past_rid = ""
    if a_class:
        race_class = a_class.get_text(strip=True)
        href = a_class.get("href", "") or ""
        m = re.search(r"/race/(\d{12})", href)
        if m:
            past_rid = m.group(1)

    # 距離・サーフェス・タイム・馬場
    data05 = _text(d_item, ".Data05")
    surface = ""
    distance = 0
    md2 = _PAST_DIST_RE.search(data05)
    if md2:
        surface_raw = md2.group(1)
        surface = {"芝": "芝", "ダ": "ダート", "障": "障害"}.get(surface_raw, surface_raw)
        distance = int(md2.group(2))
    winner_time_sec = _parse_time_to_sec(data05)
    going = ""
    strong = d_item.select_one(".Data05 strong")
    if strong:
        going = strong.get_text(strip=True)

    # 頭数 / 馬番 / 人気 / 騎手 / 斤量
    data03 = _text(d_item, ".Data03")
    field_size = 0
    horse_number = 0
    popularity = 0
    jockey = ""
    weight_kg = 0.0
    if data03:
        mf = _PAST_FIELDSIZE_RE.search(data03)
        if mf:
            field_size = int(mf.group(1))
        mu = _PAST_UMABAN_RE.search(data03)
        if mu:
            horse_number = int(mu.group(1))
        mn = _PAST_NINKI_RE.search(data03)
        if mn:
            popularity = int(mn.group(1))
        # 騎手 + 斤量 は末尾、人気の後ろ
        # 例 '16頭 10番 15人 柴田大知 56.0'
        tail = re.split(r"\d+人", data03, maxsplit=1)
        if len(tail) == 2:
            t = tail[1].strip()
            # 末尾の数字が斤量
            mw = re.search(r"([\d\.]+)\s*$", t)
            if mw:
                weight_kg = _to_float(mw.group(1))
                jockey = t[: mw.start()].strip()
            else:
                jockey = t

    # 通過順 / 上がり3F / 馬体重(増減)
    data06 = _text(d_item, ".Data06")
    passing = ""
    last_3f = 0.0
    body_weight = 0
    body_weight_diff = 0
    if data06:
        # 通過順 (空白までの先頭部分): "12-14" or "3-3-1-1"
        mp = re.match(r"\s*([\d\-]+)", data06)
        if mp:
            passing = mp.group(1)
        ml = _PAST_LAST3F_RE.search(data06)
        if ml:
            last_3f = _to_float(ml.group(1))
        mb = _PAST_BODY_RE.search(data06)
        if mb:
            body_weight = int(mb.group(1))
            body_weight_diff = int(mb.group(2))

    # 勝ち馬との時間差 (Data07 の括弧内)
    data07 = _text(d_item, ".Data07")
    time_diff_sec = 0.0
    if data07:
        mt = _PAST_TIMEDIFF_RE.search(data07)
        if mt:
            time_diff_sec = _to_float(mt.group(1))

    # 着順 (Ranking_X クラスから)
    finish_pos: int | None = None
    for c in ranking_cls:
        if c.startswith("Ranking_"):
            try:
                finish_pos = int(c.split("_", 1)[1])
            except (ValueError, IndexError):
                pass
            break

    return PastRun(
        date=date,
        venue=venue,
        race_no=race_no,
        race_class=race_class,
        race_id=past_rid,
        surface=surface,
        distance=distance,
        going=going,
        winner_time_sec=winner_time_sec,
        time_diff_sec=time_diff_sec,
        field_size=field_size,
        horse_number=horse_number,
        popularity=popularity,
        jockey=jockey,
        weight_kg=weight_kg,
        passing=passing,
        last_3f_sec=last_3f,
        body_weight=body_weight,
        body_weight_diff=body_weight_diff,
        finish_pos=finish_pos,
    )


def fetch_and_parse_past_runs(race_id: str) -> dict[int, list[PastRun]]:
    """馬柱を fetch してパース。各馬番 → 直近 5 走 dict。"""
    html = fetch_html(shutuba_past_url(race_id))
    return parse_past_runs(html)


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
    """開催一覧 HTML からその日のレース (race_id, 場名, R 数, 発走時刻) 一覧を返す。

    NAR/JRA は race_id の場コードから自動判定。URL も自動で nar.netkeiba.com /
    race.netkeiba.com を出し分ける。
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []

    # NAR/JRA 共通: .RaceList_DataItem が各レースの 1 ブロック (発走前 / 結果 のどちらでも入る)
    items = soup.select(".RaceList_DataItem")
    if items:
        for item in items:
            # この block 内のどれかの link から race_id を引く (shutuba 優先、なければ result)
            href = ""
            for a in item.find_all("a"):
                h = a.get("href", "") or ""
                if "race_id=" in h:
                    href = h
                    break
            m = re.search(r"race_id=(\d{12})", href)
            if not m:
                continue
            rid = m.group(1)
            venue_name, _, race_number, _ = _split_race_id(rid)
            parent_text = item.get_text(" ", strip=True)
            tm = re.search(r"(\d{1,2}):(\d{2})", parent_text)
            start_at = _kaisai_unix(kaisai_date, tm)
            out.append({
                "race_id": rid,
                "venue": venue_name,
                "race_no": race_number,
                "start_at": start_at,
                "url": shutuba_url(rid),
                "is_nar": is_nar_race_id(rid),
            })
    else:
        # フォールバック: 旧 selector
        for a in soup.select("a[href*='shutuba.html?race_id=']"):
            href = a.get("href", "")
            m = re.search(r"race_id=(\d{12})", href)
            if not m:
                continue
            rid = m.group(1)
            venue_name, _, race_number, _ = _split_race_id(rid)
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
            tm = re.search(r"(\d{1,2}):(\d{2})", parent_text)
            start_at = _kaisai_unix(kaisai_date, tm)
            out.append({
                "race_id": rid,
                "venue": venue_name,
                "race_no": race_number,
                "start_at": start_at,
                "url": shutuba_url(rid),
                "is_nar": is_nar_race_id(rid),
            })

    # 重複除去 (race_id 単位)
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in out:
        if r["race_id"] in seen:
            continue
        seen.add(r["race_id"])
        uniq.append(r)
    return uniq


def _kaisai_unix(kaisai_date: str, tm: re.Match | None) -> int:
    if not tm:
        return 0
    try:
        dt = datetime(
            int(kaisai_date[:4]), int(kaisai_date[4:6]), int(kaisai_date[6:8]),
            int(tm.group(1)), int(tm.group(2)),
        )
        return int(dt.timestamp())
    except (ValueError, OSError):
        return 0


# --- 小道具 ---

def _split_race_id(rid: str) -> tuple[str, int, int, str]:
    """race_id を 場名 / schedule_index / race_no / cup_id に分解。

    JRA 形式: YYYY(4) + 場(2) + 開催回(2) + 開催日(2) + R(2)
      → schedule_index = 開催日 (rid[8:10])
      → cup_id = rid[:8] (年+場+回)
    NAR 形式: YYYY(4) + 場(2) + MM(2) + DD(2) + R(2)
      → schedule_index = MMDD (rid[6:10])
      → cup_id = YYYYPP + MMDD (年+場+月日)
    """
    if not rid or len(rid) != 12:
        return ("", 0, 0, "")
    code = rid[4:6]
    venue = VENUE_CODE.get(code, f"場{code}")
    race_number = int(rid[10:12]) if rid[10:12].isdigit() else 0
    if is_nar_race_id(rid):
        # NAR: 中央 4 桁が MMDD
        schedule_index = int(rid[6:10]) if rid[6:10].isdigit() else 0
        cup_id = rid[:6] + rid[6:10]  # YYYY+PP+MMDD = rid[:10]
        return (venue, schedule_index, race_number, cup_id)
    # JRA: 中央 4 桁が 開催回(2) + 開催日(2)
    schedule_index = int(rid[8:10]) if rid[8:10].isdigit() else 0
    cup_id = rid[:8]  # YYYY+PP+回
    return (venue, schedule_index, race_number, cup_id)


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
    # 日付は race_id から取れれば最優先 (JRA は ID に日付がないので DATE_RE フォールバック)
    if race_id and is_nar_race_id(race_id):
        # NAR: YYYY + PP + MMDD + RR → 日付は rid[:4] (年) + rid[6:8] (月) + rid[8:10] (日)
        try:
            d = datetime(int(race_id[:4]), int(race_id[6:8]), int(race_id[8:10]), h, mi)
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
