"""オッズパーク (oddspark.com) から NAR (地方競馬) のオッズを取得するフォールバック。

netkeiba が IP 規制 (CloudFront 400) を食らっている間、NAR レースの **単勝/複勝**
オッズを oddspark から取得して EV 解析を続行するための経路。

なぜ単複だけか:
  - oddspark は単複のみ単純な list テーブル。馬連/ワイド/馬単/3連複/3連単 は
    すべて 2D グリッド表記でパースが別物 (将来対応; TODO)。
  - 本リポジトリで唯一 robust に +EV 確認できているのは **単勝 β=0.78** (CLAUDE.md)。
    単複が取れれば、規制中でも最重要の戦略は回せる。

URL 体系 (netkeiba の 12 桁 race_id とは別):
  KaisaiRaceList.do?raceDy=YYYYMMDD                  → その日の開催 (場) 一覧
  OneDayRaceList.do?raceDy=..&opTrackCd=..&sponsorCd=.. → 1 場 1 日のレース一覧
  Odds.do?raceDy=..&opTrackCd=..&sponsorCd=..&raceNb=N&betType=1&viewType=0 → 単複

oddspark の場コード (opTrackCd) は netkeiba の場コードと別 namespace なので、
**場名でマッチング**する (netkeiba race_id → 場名 → oddspark opTrackCd)。
"""
from __future__ import annotations

import html as _html
import re
import ssl
from dataclasses import dataclass
from datetime import datetime
from urllib.request import Request, urlopen

from .models import BetOdds, Horse, PastRun, Race, RaceData, TrifectaOdds
from .parse import VENUE_CODE, _split_race_id, is_nar_race_id

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
_BASE = "https://www.oddspark.com/keiba"
_SSL = ssl.create_default_context()

# oddspark opTrackCd → 場名 (実測 2026-05)。未知コードは OneDayRaceList の title で動的解決。
OP_TRACK_TO_VENUE = {
    "03": "帯広", "11": "盛岡", "32": "船橋", "34": "川崎", "41": "金沢",
    "42": "笠松", "43": "名古屋", "51": "園田", "55": "高知", "61": "佐賀",
}
_VENUE_RE = re.compile(
    r"門別|盛岡|水沢|浦和|船橋|大井|川崎|金沢|笠松|名古屋|園田|姫路|高知|佐賀|帯広"
)


# netkeiba 場名 → netkeiba 場コード (race_id 構築用、VENUE_CODE の逆)
_NETKEIBA_VENUE_TO_CODE = {v: k for k, v in VENUE_CODE.items()}


class OddsparkError(RuntimeError):
    pass


def _get(url: str, *, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read().decode("utf-8", errors="replace")


@dataclass
class OddsparkLoc:
    race_dy: str       # YYYYMMDD
    op_track_cd: str
    sponsor_cd: str
    race_nb: int
    venue: str


def _nar_date(rid: str) -> str | None:
    """NAR 12 桁 race_id から YYYYMMDD (oddspark raceDy 用) を復元。"""
    if len(rid) != 12 or not is_nar_race_id(rid):
        return None
    return f"{rid[:4]}{rid[6:8]}{rid[8:10]}"


def fetch_race_list_oddspark(yyyymmdd: str | None = None) -> list[dict]:
    """oddspark から当日の NAR レース一覧を取得 (netkeiba block 中の race discovery 用)。

    返り値: [{race_id (内部正規化), netkeiba_race_id (12桁), venue, race_no, start_at,
             url (netkeiba shutuba), source="oddspark"}].
    KaisaiRaceList → 各場の OneDayRaceList から発走時刻 (race 順) を取り、netkeiba 場
    コードへ逆引きして 12 桁 race_id を構築する。場名が netkeiba VENUE_CODE に無い
    (ばんえい等で表記差) 場は skip。
    """
    date = yyyymmdd or datetime.now().strftime("%Y%m%d")
    yyyy, mm, dd = int(date[:4]), int(date[4:6]), int(date[6:8])
    try:
        kl = _get(f"{_BASE}/KaisaiRaceList.do?raceDy={date}")
    except Exception as ex:  # noqa: BLE001
        raise OddsparkError(f"KaisaiRaceList 取得失敗: {ex}") from ex
    pairs: list[tuple[str, str]] = []
    seen = set()
    for tc, sc in re.findall(
        r"OneDayRaceList\.do\?raceDy=\d+&amp;opTrackCd=(\d+)&amp;sponsorCd=(\d+)", kl
    ):
        if (tc, sc) not in seen:
            seen.add((tc, sc))
            pairs.append((tc, sc))

    out: list[dict] = []
    for tc, sc in pairs:
        try:
            page = _get(f"{_BASE}/OneDayRaceList.do?raceDy={date}&opTrackCd={tc}&sponsorCd={sc}")
        except Exception:
            continue
        vm = _VENUE_RE.search(page)
        venue = vm.group(0) if vm else None
        code = _NETKEIBA_VENUE_TO_CODE.get(venue or "")
        if not code:
            continue  # netkeiba コード不明 (= race_id 構築不可)
        # 発走時刻を race 順 (R1, R2, ...) に抽出
        times = re.findall(r"発走時間.{0,120}?(\d{1,2}):(\d{2})", page, re.DOTALL)
        for i, (hh, mn) in enumerate(times, start=1):
            try:
                start_at = int(datetime(yyyy, mm, dd, int(hh), int(mn)).timestamp())
            except (ValueError, OSError):
                continue
            rid = f"{date[:4]}{code}{date[4:8]}{i:02d}"  # YYYY+場+MMDD+RR
            out.append({
                "race_id": _norm_race_id(rid),
                "netkeiba_race_id": rid,
                "venue": venue,
                "race_no": i,
                "start_at": start_at,
                "url": f"https://nar.netkeiba.com/race/shutuba.html?race_id={rid}",
                "source": "oddspark",
            })
    return out


def _norm_race_id(netkeiba_rid: str) -> str:
    """netkeiba race_id → 内部 'cup_id-schedule-raceno' (auto_watch._normalize_race_id 互換)。"""
    venue, schedule_index, race_number, cup_id = _split_race_id(netkeiba_rid)
    return f"{cup_id}-{schedule_index}-{race_number}"


def find_oddspark_race(netkeiba_rid: str) -> OddsparkLoc | None:
    """netkeiba の NAR race_id → oddspark の (raceDy, opTrackCd, sponsorCd, raceNb)。

    場名でマッチング。その日の KaisaiRaceList から opTrackCd/sponsorCd を引き、
    OP_TRACK_TO_VENUE に無いコードは OneDayRaceList の title で場名を確認する。
    """
    if not is_nar_race_id(netkeiba_rid):
        return None
    venue = VENUE_CODE.get(netkeiba_rid[4:6], "")
    race_dy = _nar_date(netkeiba_rid)
    if not venue or not race_dy:
        return None
    race_nb = int(netkeiba_rid[10:12]) if netkeiba_rid[10:12].isdigit() else 0
    try:
        kl = _get(f"{_BASE}/KaisaiRaceList.do?raceDy={race_dy}")
    except Exception as ex:  # noqa: BLE001
        raise OddsparkError(f"KaisaiRaceList 取得失敗: {ex}") from ex
    pairs = []
    seen = set()
    for tc, sc in re.findall(
        r"OneDayRaceList\.do\?raceDy=\d+&amp;opTrackCd=(\d+)&amp;sponsorCd=(\d+)", kl
    ):
        if (tc, sc) not in seen:
            seen.add((tc, sc))
            pairs.append((tc, sc))
    for tc, sc in pairs:
        known = OP_TRACK_TO_VENUE.get(tc)
        if known is None:
            try:
                page = _get(f"{_BASE}/OneDayRaceList.do?raceDy={race_dy}&opTrackCd={tc}&sponsorCd={sc}")
            except Exception:
                continue
            m = _VENUE_RE.search(page)
            known = m.group(0) if m else None
        if known == venue:
            return OddsparkLoc(race_dy=race_dy, op_track_cd=tc, sponsor_cd=sc,
                               race_nb=race_nb, venue=venue)
    return None


def _clean(cell: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()


@dataclass
class OddsparkHorse:
    number: int
    name: str
    win_odds: float
    place_min: float
    place_max: float
    lineage_nb: str = ""   # oddspark 競走馬 ID (HorseDetail 馬柱取得用)


_HD_SURFACE = {"芝": "芝", "ダ": "ダート", "障": "障害"}


def _hd_dist_surface(s: str) -> tuple[str, int]:
    m = re.match(r"\s*([芝ダ障])\s*(\d{3,4})", s)
    return (_HD_SURFACE.get(m.group(1), m.group(1)), int(m.group(2))) if m else ("", 0)


def _hd_time_sec(s: str) -> float:
    """'1:41.2' / '41.9' → 秒。"""
    m = re.match(r"(?:(\d+):)?(\d+(?:\.\d+)?)\s*$", s.strip())
    if not m:
        return 0.0
    return (int(m.group(1)) if m.group(1) else 0) * 60 + float(m.group(2))


def parse_horse_detail(html: str, *, limit: int = 12) -> list[PastRun]:
    """oddspark HorseDetail の成績表 → [PastRun] (新しい順、最大 limit 件)。

    列順 (実測): 年月日/競馬場/レース名/距離/馬場(天候)/頭数/枠番/馬番/人気/着順/
    騎手/負担重量/馬体重/タイム/着差/上3F/通過順位/1着馬。タイムは馬の自走時計なので
    own_time_sec として直接採用 (winner_time_sec=own, time_diff_sec=0)。着順は netkeiba
    馬柱と同じ慣習で 1/2/3 のみ int・他は None。馬体重前走比は表に無いので 0。
    """
    # 成績表 = レース日付 (YYYY/MM/DD) が最も多く並ぶ <table> (ヘッダは tag/zero-width
    # が混ざり文字列一致が効かないので、日付行数で選ぶ)。
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    _date_re = re.compile(r"\d{4}[./]\d{1,2}[./]\d{1,2}")
    target = max(tables, key=lambda t: len(_date_re.findall(t)), default=None)
    if not target or len(_date_re.findall(target)) < 1:
        return []
    out: list[PastRun] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", target, re.DOTALL)[1:]:
        c = [_html.unescape(re.sub(r"<[^>]+>", " ", x)).strip()
             for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.DOTALL)]
        if len(c) < 14 or not re.match(r"\d{4}[./]\d", c[0]):
            continue  # ヘッダ/取消行などはスキップ (タイム列まで無い)
        surface, distance = _hd_dist_surface(c[3])
        fin = int(c[9]) if c[9] in ("1", "2", "3") else None
        out.append(PastRun(
            date=c[0].replace("/", "."),
            venue=c[1],
            surface=surface,
            distance=distance,
            going=re.sub(r"\(.*", "", c[4]).strip(),
            field_size=int(re.sub(r"\D", "", c[5]) or 0),
            horse_number=int(re.sub(r"\D", "", c[7]) or 0),
            popularity=int(c[8]) if c[8].isdigit() else 0,
            finish_pos=fin,
            jockey=c[10],
            weight_kg=float(c[11]) if re.match(r"\d+\.?\d*$", c[11]) else 0.0,
            body_weight=int(c[12]) if c[12].isdigit() else 0,
            body_weight_diff=0,
            winner_time_sec=_hd_time_sec(c[13]),   # own time を own_time_sec に
            time_diff_sec=0.0,
            last_3f_sec=float(c[15]) if len(c) > 15 and re.match(r"\d+\.?\d*$", c[15]) else 0.0,
            passing=c[16] if len(c) > 16 else "",
        ))
        if len(out) >= limit:
            break
    return out


def fetch_horse_past_runs(lineage_nb: str) -> list[PastRun]:
    if not lineage_nb:
        return []
    try:
        return parse_horse_detail(_get(f"{_BASE}/HorseDetail.do?lineageNb={lineage_nb}"))
    except Exception:  # noqa: BLE001
        return []


def _date_key(s: str) -> tuple[int, int, int]:
    """'YYYY.M.D' / 'YYYY.MM.DD' / 'YYYY/M/D' → (y, m, d) タプル。

    leakage 除外の日付比較は **文字列比較だと非ゼロ詰め日付で破綻**する
    ('2026.5.2' < '2026.05.26' が False になり正当な過去走を誤除外)。
    数値タプルで比較するため正規化する。パース不能は (0,0,0)。
    """
    parts = re.split(r"[./]", s.strip())
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (0, 0, 0)


def parse_tanfuku(html: str) -> list[OddsparkHorse]:
    """単複 (betType=1) ページの table → [OddsparkHorse]。

    行構造: [枠番, 馬番, 馬名, 単勝, 複勝 "min - max"]
    """
    out: list[OddsparkHorse] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        lineage_m = re.search(r"HorseDetail\.do\?lineageNb=(\d+)", tr)  # 馬柱取得用 ID
        cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
        cells = [c for c in cells if c]
        if len(cells) < 4 or not re.match(r"^\d{1,2}$", cells[1] if len(cells) > 1 else ""):
            continue
        # 馬番 = cells[1], 馬名 = cells[2], 単勝 = 最初の x.y, 複勝 = "min - max"
        try:
            number = int(cells[1])
        except ValueError:
            continue
        name = cells[2]
        # 馬名が空 or 数字のみ (= 別テーブル/枠サマリの混入行) は除外。
        # 実在の馬名は必ずカナ等の非数字を含む。
        if not name or re.fullmatch(r"[\d.\s\-－]+", name):
            continue
        win_m = re.search(r"^\d{1,4}\.\d$", cells[3]) if len(cells) > 3 else None
        win_odds = float(cells[3]) if win_m else 0.0
        place_min = place_max = 0.0
        for c in cells[3:]:
            pm = re.search(r"(\d{1,4}\.\d)\s*[-－]\s*(\d{1,4}\.\d)", c)
            if pm:
                place_min, place_max = float(pm.group(1)), float(pm.group(2))
                break
        if number > 0 and (win_odds > 0 or place_min > 0):
            out.append(OddsparkHorse(number, name, win_odds, place_min, place_max,
                                     lineage_nb=lineage_m.group(1) if lineage_m else ""))
    return out


def _odds_table(html: str) -> str:
    """ページ内で小数オッズが最も多い <table> を返す (オッズ本体表)。"""
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    if not tables:
        return ""
    return max(tables, key=lambda t: len(re.findall(r"\d+\.\d", t)))


def _to_odds(text: str, *, mode: str = "single") -> float:
    """セル文字列 → オッズ float。mode='min' は範囲 'a - b' の下限を採る (複勝/ワイド)。"""
    text = _html.unescape(re.sub(r"<[^>]+>", "", text))
    nums = re.findall(r"\d{1,5}\.\d", text)
    if not nums:
        return 0.0
    if mode == "min":
        return min(float(x) for x in nums)
    return float(nums[0])


def _grid_header_firsts(table: str) -> list[int]:
    """グリッド表の先頭行 (th のみ) の馬番リスト = 列見出し。"""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL)
    if not rows:
        return []
    return [int(x) for x in re.findall(r"<th[^>]*>\s*(\d+)\s*</th>", rows[0])]


def parse_pair_grid(html: str, *, value_mode: str = "single") -> list[tuple[int, int, float]]:
    """馬連/ワイド の三角グリッド → [(a, b, odds)] (a<b)。

    各データ行 (1-indexed r) のセルは `<th>`=大きい方の馬番、`<td>`=オッズ。
    対角構造で **小さい方 = th − r** (r 行目は「2着差 = r」の対角)。列見出しは
    8 列で頭打ちになる (大頭数だと小馬番側≥9 のペアは grid から脱落する) ため、
    列位置ではなく対角規則で 1 頭目を復元する (列ベースだと誤マッピングで dup/誤オッズ)。
    ワイドは td が範囲 'min - max' なので value_mode='min'。

    注意: oddspark の grid は 1 頭目を 1..8 までしか表示しないため、>9 頭立てでは
    小馬番側が 9 以上のペア (= 最も人気薄の組) が欠落する。誤オッズは出さない。
    """
    table = _odds_table(html)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL)
    if len(rows) < 2:
        return []

    def row_cells(row: str) -> list[tuple[str, str]]:
        return re.findall(r"<th[^>]*>\s*(\d+)\s*</th>\s*<td[^>]*>(.*?)</td>", row, re.DOTALL)

    data_rows = rows[1:]
    # grid 幅 (= 1頭目の対角列数の上限、通常 8)。先頭データ行のセル数で決まる。
    cap = len(row_cells(data_rows[0])) if data_rows else 0
    out: dict[tuple[int, int], float] = {}
    for r, row in enumerate(data_rows, start=1):
        cells = row_cells(row)
        extra_idx = 0
        in_diag = True
        for i, (th_s, val) in enumerate(cells):
            b = int(th_s)                       # th = 大きい方の馬番 (2着相当)
            if in_diag and b == (i + 1) + r:
                a = i + 1                        # 対角帯: 小さい方 = 列位置+1
            else:
                # 折り返しセル: 小さい方 = cap+1, cap+2, ... (大頭数で >8 の組)
                in_diag = False
                a = cap + 1 + extra_idx
                extra_idx += 1
            odds = _to_odds(val, mode=value_mode)
            if odds > 0 and 1 <= a != b:
                lo, hi = (a, b) if a < b else (b, a)
                out.setdefault((lo, hi), odds)   # keep-first (dedup 安全)
    return [(a, b, o) for (a, b), o in out.items()]


def parse_horse_options(html: str) -> list[tuple[int, str]]:
    """`selectHorseNb` の option から出走馬 [(馬番, 馬名)] を得る (権威ソース)。

    単複テーブルより堅牢 (取消含む全出走馬が確実に載る)。
    """
    body = None
    for sm in re.finditer(r"<select([^>]*)>(.*?)</select>", html, re.DOTALL):
        if "selectHorseNb" in sm.group(1):
            body = sm.group(2)
            break
    if body is None:
        return []
    out: list[tuple[int, str]] = []
    for v, t in re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)</option>', body):
        name = re.sub(r"^\d+\s*", "", _html.unescape(t)).strip().replace("　", "")
        if name:  # 馬名付き option のみ (空 value 等を除外)
            out.append((int(v), name))
    return out


def parse_exacta_grid(html: str) -> list[tuple[int, int, float]]:
    """馬単 の正方グリッド → [(1着, 2着, odds)]。

    3連複と同様 **複数テーブルに分割**される (table3=2着1-8、table4=2着9 の列)。
    各テーブル: 先頭行 th=2着の列見出し、データ行 `<th>`=1着(行内一定)、`<td>`=オッズ。
    列見出し[col] がそのまま 2着 (自分の列は空セル=スキップ)。全テーブルを舐めて
    N(N-1) を完全列挙する。
    """
    out: dict[tuple[int, int], float] = {}
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL)
        if not rows:
            continue
        seconds = [int(x) for x in re.findall(r"<th[^>]*>\s*(\d+)\s*</th>", rows[0])]
        if not seconds:
            continue  # 2着見出しの無いテーブル (単複サイドバー等) は skip
        for row in rows[1:]:
            pairs = re.findall(r"<th[^>]*>\s*(\d+)\s*</th>\s*<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if not pairs:
                continue
            first = int(pairs[0][0])           # 行内一定 = 1着
            for col, (_th, val) in enumerate(pairs):
                if col >= len(seconds):
                    break
                second = seconds[col]
                odds = _to_odds(val)
                if odds > 0 and first != second:
                    out[(first, second)] = odds
    return [(a, b, o) for (a, b), o in out.items()]


def parse_trio_grid(html: str) -> list[tuple[tuple[int, int, int], float]]:
    """3連複の全グリッド (1 ページ複数テーブル) → [(key昇順, odds)]。

    1着軸ごとに別 <table> (table3=軸1 のペア '1-x'、table4=軸2 の '2-x' …) に分かれ、
    全テーブルを舐めると C(n,3) を完全列挙できる (axis 切替の GET/JS 不要)。
    各テーブル: 先頭行 th='a-b' (ペア=上位2頭)、データ行 th=3頭目、td=オッズ。
    """
    out: dict[tuple[int, int, int], float] = {}
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL)
        if not rows:
            continue
        pairs = [(int(a), int(b)) for a, b in
                 re.findall(r"<th[^>]*>\s*(\d+)\s*-\s*(\d+)\s*</th>", rows[0])]
        if not pairs:
            continue  # 3連複グリッドでないテーブル
        for row in rows[1:]:
            cells = re.findall(r"<th[^>]*>\s*(\d+)\s*</th>\s*<td[^>]*>(.*?)</td>", row, re.DOTALL)
            for col, (third, val) in enumerate(cells):
                if col >= len(pairs):
                    break
                a, b = pairs[col]
                key = tuple(sorted({a, b, int(third)}))
                odds = _to_odds(val)
                if odds > 0 and len(key) == 3:
                    out[key] = odds  # type: ignore[index]
    return sorted(out.items(), key=lambda kv: kv[1])


def parse_triple_list(html: str, *, ordered: bool) -> list[tuple[tuple[int, ...], float]]:
    """3連複(a-b-c)/3連単(a → b → c) のリスト表 → [(key, odds)]。

    ordered=True は 3連単 (順あり)、False は 3連複 (順不同, key は昇順)。
    """
    sep = "→" if ordered else "[-－]"
    pat = re.compile(
        r'<th[^>]*>\s*(\d+)\s*' + sep + r'\s*(\d+)\s*' + sep + r'\s*(\d+)\s*</th>'
        r'\s*<td[^>]*>.*?(\d{1,5}\.\d).*?</td>',
        re.DOTALL,
    )
    out: list[tuple[tuple[int, ...], float]] = []
    for a, b, c, od in pat.findall(html):
        key = (int(a), int(b), int(c))
        if not ordered:
            key = tuple(sorted(key))
        out.append((key, float(od)))
    return out


def _odds_url(loc: OddsparkLoc, bet_type: int) -> str:
    return (f"{_BASE}/Odds.do?raceDy={loc.race_dy}&opTrackCd={loc.op_track_cd}"
            f"&sponsorCd={loc.sponsor_cd}&raceNb={loc.race_nb}&betType={bet_type}&viewType=0")


def fetch_oddspark_tanfuku(loc: OddsparkLoc) -> list[OddsparkHorse]:
    return parse_tanfuku(_get(_odds_url(loc, 1)))


@dataclass
class OddsparkBets:
    """oddspark から取得した 1 レース分のオッズ (単複/馬連/ワイド/3連単)。"""
    horses: list[tuple[int, str]]                 # (馬番, 馬名) 全出走馬 (selectHorseNb 由来)
    tanfuku: list[OddsparkHorse]                  # 単複
    quinella: list[tuple[int, int, float]]        # 馬連 (a<b, odds)
    wide: list[tuple[int, int, float]]            # ワイド (a<b, min odds)
    exacta: list[tuple[int, int, float]]          # 馬単 (1着,2着 順あり, full)
    trio: list[tuple[tuple[int, int, int], float]]      # 3連複 (順不同, full)
    trifecta: list[tuple[tuple[int, int, int], float]]  # 3連単 (順あり, full)


def fetch_oddspark_trifecta(
    loc: OddsparkLoc, valid: set[int]
) -> list[tuple[tuple[int, int, int], float]]:
    """3連単を全 1着軸 (horseNb=1..N) で列挙 → 全組合せ。

    oddspark の 3連単は 1着軸を `&horseNb=N` で切替可能 (各軸 = (N-1)(N-2) 組)。
    """
    combos: dict[tuple[int, int, int], float] = {}
    for a in sorted(valid):
        # 1 軸の fetch/parse 失敗 (transient 5xx / 取消馬の空ページ等) で全軸を捨てない。
        # find_oddspark_race / fetch_race_list_oddspark と同じく per-iteration で握る。
        try:
            html = _get(_odds_url(loc, 8) + f"&horseNb={a}")
        except Exception:  # noqa: BLE001
            continue
        for key, od in parse_triple_list(html, ordered=True):
            if od > 0 and len(set(key)) == 3 and all(x in valid for x in key):
                combos[key] = od  # type: ignore[assignment]
    return sorted(combos.items(), key=lambda kv: kv[1])


def fetch_oddspark_bets(loc: OddsparkLoc, *, with_trifecta: bool = True) -> OddsparkBets:
    """**実オッズと cross-validation 済の bet type のみ**を取得: 単勝/複勝 + 3連単。

    重要 (2026-05 の調査結果): 馬連/ワイド/馬単/3連複 の oddspark グリッドは 1 セルに
    馬番が 1 つしか出ず、もう片方は **列位置から推定**するしかない。この位置推定は
    >9 頭立てでセルの折り返しが起きると **誤った組にオッズを割り当てる** (netkeiba 実
    オッズとの照合で 1番人気 2.4 倍 → 514 倍 のような取り違えを確認)。誤オッズは賭け金
    を動かす最悪のバグなので、これらグリッド型は **production では無効化**する。
    一方 単複 (list: 馬番+馬名+オッズ) と 3連単 (th に "a → b → c" が明示) は組合せが
    HTML に明示され、netkeiba 実オッズと一致 (単勝 6/8・3連単 ~85% 一致、残差は時間
    ドリフト) するため採用する。`parse_pair_grid`/`parse_exacta_grid`/`parse_trio_grid`
    は将来の信頼できる解法用に残置 (現状未使用)。
    """
    tanfuku = parse_tanfuku(_get(_odds_url(loc, 1)))
    grid8 = _get(_odds_url(loc, 8))               # selectHorseNb (全馬リスト) を含む
    horses = parse_horse_options(grid8) or [(h.number, h.name) for h in tanfuku]
    valid = {n for n, _ in horses}
    trifecta = fetch_oddspark_trifecta(loc, valid) if with_trifecta else []
    # グリッド型 (馬連/ワイド/馬単/3連複) は誤オッズ割当の危険があり無効化 (上記 docstring)。
    return OddsparkBets(horses=horses, tanfuku=tanfuku, quinella=[],
                        wide=[], exacta=[], trio=[], trifecta=trifecta)


def _pair_bets(combos: list[tuple[int, int, float]], bet_type: str) -> list[BetOdds]:
    bets = [BetOdds(bet_type=bet_type, key=(a, b), odds=o) for a, b, o in combos if o > 0]
    bets.sort(key=lambda b: b.odds)
    for i, b in enumerate(bets, 1):
        b.popularity = i
    return bets


def market_win_probs_from_tanfuku(horses: list[OddsparkHorse]) -> dict[int, float]:
    """単勝オッズ → 市場暗黙 1 着率 (1/odds 正規化)。estimate_probs(market_win_override=) 用。"""
    raw = {h.number: 1.0 / h.win_odds for h in horses if h.win_odds > 0}
    s = sum(raw.values())
    return {k: v / s for k, v in raw.items()} if s > 0 else {}


def overlay_oddspark_odds(rd: RaceData, horses: list[OddsparkHorse]) -> RaceData:
    """既存 RaceData (cached netkeiba 出馬表由来) に oddspark の単複オッズを上書きする。

    Horse.win_odds を更新し、other_bets["win"]/["place"] を構築。features/past_runs
    は元の rd を温存 (= netkeiba cache の馬柱で確率モデルが効く)。
    """
    by_num = {h.number: h for h in rd.race.horses}
    win_bets: list[BetOdds] = []
    place_bets: list[BetOdds] = []
    for oh in horses:
        h = by_num.get(oh.number)
        if h is not None and oh.win_odds > 0:
            h.win_odds = oh.win_odds
        if oh.win_odds > 0:
            win_bets.append(BetOdds(bet_type="win", key=(oh.number,), odds=oh.win_odds))
        if oh.place_min > 0:
            # 複勝は下限採用 (CLAUDE.md: 実払戻が下限以上で確定する保守値)
            place_bets.append(BetOdds(bet_type="place", key=(oh.number,), odds=oh.place_min))
    rd.other_bets = dict(rd.other_bets or {})
    if win_bets:
        rd.other_bets["win"] = sorted(win_bets, key=lambda b: b.odds)
        for i, b in enumerate(rd.other_bets["win"], 1):
            b.popularity = i
    if place_bets:
        rd.other_bets["place"] = place_bets
    return rd


def build_oddspark_racedata(
    tanfuku: list[OddsparkHorse], netkeiba_rid: str,
    *, horse_options: list[tuple[int, str]] | None = None,
    fetch_past: bool = True,
) -> RaceData:
    """cached 出馬表が無い場合の RaceData (oddspark 由来)。

    fetch_past=True で各馬の HorseDetail から **馬柱 (past_runs) を取得**し Horse に付与
    → build_features が効き、estimate_probs が市場主導でなくモデルの edge を出せる。
    horse_options (selectHorseNb 由来) を渡せば全出走馬を確実に載せる。
    """
    venue, schedule_index, race_number, cup_id = _split_race_id(netkeiba_rid)
    win_by_num = {h.number: h.win_odds for h in tanfuku}
    lineage_by_num = {h.number: h.lineage_nb for h in tanfuku}
    if horse_options:
        horse_rows = [Horse(number=n, name=nm, win_odds=win_by_num.get(n, 0.0))
                      for n, nm in horse_options]
    else:
        horse_rows = [Horse(number=h.number, name=h.name, win_odds=h.win_odds) for h in tanfuku]
    if fetch_past:
        # leakage 防止: HorseDetail は (過去 race を解析する場合) 対象 race 自身の結果も
        # 含むので、**対象 race の日付以降を除外**する (live では対象 race は未走で no-op)。
        # 窓は netkeiba 馬柱 (直近5走) に揃えてモデルの学習分布と一致させる。
        race_date = (f"{netkeiba_rid[:4]}.{netkeiba_rid[6:8]}.{netkeiba_rid[8:10]}"
                     if is_nar_race_id(netkeiba_rid) else "")
        for h in horse_rows:
            ln = lineage_by_num.get(h.number, "")
            if ln:
                runs = fetch_horse_past_runs(ln)
                if race_date:
                    rk = _date_key(race_date)
                    runs = [r for r in runs if _date_key(r.date) < rk]
                h.past_runs = runs[:5]
    race = Race(
        cup_id=cup_id, schedule_index=schedule_index, race_number=race_number,
        venue_id=int(netkeiba_rid[4:6]) if netkeiba_rid[4:6].isdigit() else 0,
        venue_name=venue, race_class="", distance=0, horses=horse_rows,
    )
    rd = RaceData(race=race, trifecta=[], other_bets={})
    return overlay_oddspark_odds(rd, tanfuku)


# ---------- 解析パイプライン (NAR フォールバック) ----------


def analyze_oddspark(netkeiba_rid: str, *, save_snapshot: bool = False, start_at: int = 0,
                     with_llm: bool = True) -> dict:
    """NAR race を oddspark の単複/3連単オッズで解析する (netkeiba block 中のフォールバック)。

    出馬表 (馬柱/特徴量) は data/raw の netkeiba cache があれば使い (確率モデルが効く)、
    無ければ oddspark の馬リストだけで市場ブレンド主導の確率を出す。
    save_snapshot=True で data/predictions/<race_id>.json を保存 (dashboard / watch-auto 用)。
    返り値: {rd, probs, tables, bundle, ...}。
    """
    import gzip
    from pathlib import Path

    from . import ev as ev_mod
    from . import portfolio as pf
    from .parse import parse_past_runs, parse_shutuba

    loc = find_oddspark_race(netkeiba_rid)
    if loc is None:
        raise OddsparkError(f"oddspark で {netkeiba_rid} の開催が見つからない (NAR/日付/場名)")
    bets = fetch_oddspark_bets(loc)
    if not bets.tanfuku and not bets.horses:
        raise OddsparkError("oddspark オッズが空")

    root = Path(__file__).resolve().parents[1]
    sh = root / "data" / "raw" / f"{netkeiba_rid}-shutuba.html.gz"
    used_cache = False
    if sh.exists():
        rd = parse_shutuba(gzip.open(sh, "rt", encoding="utf-8").read(), race_id=netkeiba_rid)
        past = root / "data" / "raw" / f"{netkeiba_rid}-past.html.gz"
        if past.exists():
            runs = parse_past_runs(gzip.open(past, "rt", encoding="utf-8").read())
            for h in rd.race.horses:
                h.past_runs = runs.get(h.number, [])
        overlay_oddspark_odds(rd, bets.tanfuku)
        used_cache = True
    else:
        rd = build_oddspark_racedata(bets.tanfuku, netkeiba_rid, horse_options=bets.horses)

    # 発走時刻 (discovery 由来) を補完: cache に無い/0 のときのみ上書き
    if start_at and not rd.race.start_at:
        rd.race.start_at = start_at
        rd.race.close_at = start_at

    # 馬連 / ワイド を other_bets に、3連単を rd.trifecta に追加
    rd.other_bets = dict(rd.other_bets or {})
    if bets.quinella:
        rd.other_bets["quinella"] = _pair_bets(bets.quinella, "quinella")
    if bets.wide:
        rd.other_bets["wide"] = _pair_bets(bets.wide, "wide")
    if bets.exacta:
        rd.other_bets["exacta"] = _pair_bets(bets.exacta, "exacta")
    if bets.trio:
        trio_bets = [BetOdds(bet_type="trio", key=k, odds=o) for k, o in bets.trio if o > 0]
        trio_bets.sort(key=lambda b: b.odds)
        for i, b in enumerate(trio_bets, 1):
            b.popularity = i
        rd.other_bets["trio"] = trio_bets
    if bets.trifecta:
        rd.trifecta = [
            TrifectaOdds(key=k, odds=o, popularity=i)
            for i, (k, o) in enumerate(bets.trifecta, 1)
        ]

    mwp = market_win_probs_from_tanfuku(bets.tanfuku)
    probs = ev_mod.estimate_probs(rd, market_blend=0.78, market_win_override=mwp)
    tables = {bt: ev_mod.build_bet_table(rd.other_bets.get(bt, []), probs, bet_type=bt)
              for bt in ("win", "place", "quinella", "wide", "exacta", "trio")}
    # 3連単 EV table (rd.trifecta 由来)
    tri_table = ev_mod.build_table(rd, probs) if rd.trifecta else []
    cands = [
        {"bet_type": r.bet_type, "key": list(r.key), "odds": r.odds,
         "prob": r.prob, "px_o": r.px_o, "tier": r.tier}
        for tbl in tables.values() for r in tbl
    ]
    cands += [
        {"bet_type": "trifecta", "key": list(r.key), "odds": r.odds,
         "prob": r.prob, "px_o": r.px_o, "tier": r.tier}
        for r in tri_table
    ]
    bundle = pf.build_bundle(cands, probs)
    bundle["source"] = "oddspark"
    tables["trifecta"] = tri_table

    if save_snapshot:
        from . import analyze as az_mod
        from .aptitude import compute_aptitudes
        from .features import build_features
        race_id = f"{rd.race.cup_id}-{rd.race.schedule_index}-{rd.race.race_number}"
        # cached でも oddspark HorseDetail 由来でも past_runs があれば特徴量が効く
        has_past = any(h.past_runs for h in rd.race.horses)
        feats = build_features(rd) if (used_cache or has_past) else None
        aptitudes = compute_aptitudes(rd, feats=feats) if feats else None
        apt_top = az_mod._aptitude_top_horses(aptitudes, n=6) if aptitudes else None
        plan_rows = ev_mod.apply_caps(tri_table)
        snap_bet_tables = {k: v for k, v in tables.items()
                           if k in ("win", "place") and v}
        market_signals = None
        try:
            from .market_signal import compute_market_signals
            market_signals = compute_market_signals(rd)
        except Exception:  # noqa: BLE001
            market_signals = None
        best_times = az_mod._serialize_best_times(rd, feats) if feats else []
        try:
            az_mod._save_prediction_snapshot(
                race_id, rd, tri_table, plan_rows, aptitudes, snap_bet_tables, apt_top,
                market_signals, feats=feats, lgbm_info=ev_mod.lgbm_status(),
                hit_points=3, probs=probs,
            )
            _tag_snapshot_source(race_id, "oddspark")
        except Exception as ex:  # noqa: BLE001
            print(f"[analyze_oddspark] snapshot 保存失敗: {ex}")

        # netkeiba と同じ claude 調査: ① 3連単 plan の検索補強 (evidence) ②総合オススメ
        # 束の web 検証。どちらも claude -p。netkeiba 経路 (analyze.py) と同じ関数を流用。
        if with_llm:
            from . import llm as llm_mod
            try:
                initial = az_mod._print_llm_evaluation(
                    rd, plan_rows, model="opus", probs=probs, aptitudes=aptitudes,
                    aptitude_top_horses=apt_top, market_signals=market_signals,
                    horse_best_times=best_times,
                )
                evidence = llm_mod.parse_evidence(initial)
                if evidence:
                    az_mod._save_evidence_to_snapshot(race_id, plan_rows, evidence, apt_top, hit_points=3)
            except Exception as ex:  # noqa: BLE001
                print(f"[analyze_oddspark] LLM evidence 失敗: {ex}")
            try:
                az_mod._validate_and_update_bundle(
                    race_id, rd, probs, tri_table, snap_bet_tables,
                    aptitudes=aptitudes, market_signals=market_signals,
                    horse_best_times=best_times, model="opus",
                )
            except Exception as ex:  # noqa: BLE001
                print(f"[analyze_oddspark] bundle 検証失敗: {ex}")

    return {
        "rd": rd, "probs": probs, "loc": loc, "used_cache": used_cache,
        "tables": tables, "bundle": bundle,
    }


def _tag_snapshot_source(race_id: str, source: str) -> None:
    """保存済 snapshot に odds_source を追記 (どこ由来のオッズか分かるように)。"""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "predictions" / f"{race_id}.json"
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d["odds_source"] = source
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def _cli() -> None:
    import sys

    from rich.console import Console
    from rich.table import Table

    console = Console()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    save = "--snapshot" in sys.argv
    start_at = 0
    for a in sys.argv:
        if a.startswith("--start-at="):
            start_at = int(a.split("=", 1)[1] or 0)
    if not args:
        console.print("usage: python -m src.scrape_oddspark <netkeiba_nar_race_id> [--snapshot] [--start-at=UNIX]")
        raise SystemExit(2)
    rid = args[0]
    try:
        res = analyze_oddspark(rid, save_snapshot=save, start_at=start_at,
                               with_llm="--no-llm" not in sys.argv)
    except OddsparkError as ex:
        console.print(f"[yellow]oddspark 解析不能 ({rid}): {ex}[/yellow]")
        raise SystemExit(1)
    loc = res["loc"]
    console.rule(f"[bold]oddspark {loc.venue} {loc.race_nb}R[/bold] "
                 f"(raceDy={loc.race_dy} opTrackCd={loc.op_track_cd}) "
                 f"{'[cache 出馬表使用]' if res['used_cache'] else '[oddspark 馬リストのみ]'}")
    labels = {"win": "単勝", "place": "複勝", "quinella": "馬連", "wide": "ワイド",
              "exacta": "馬単", "trio": "3連複", "trifecta": "3連単"}
    for bt, tbl in res["tables"].items():
        if not tbl:
            continue
        t = Table(title=f"{labels.get(bt, bt)} EV (P×O 降順)")
        for c in ("買い目", "オッズ", "推定P", "P×O", "帯"):
            t.add_column(c)
        for r in tbl[:6]:
            t.add_row("-".join(map(str, r.key)), f"{r.odds:.1f}", f"{r.prob*100:.1f}%",
                      f"{r.px_o:.2f}", r.tier)
        console.print(t)
    b = res["bundle"]
    console.print(f"\n[bold]総合オススメ (単複, トリガミ防止済):[/bold] "
                  f"{len(b['legs'])}点 ¥{b['total_stake']:,} "
                  f"束的中率 {b['bundle_hit_prob']*100:.1f}%")
    for l in b["legs"]:
        console.print(f"  {l['bet_type']} #{'-'.join(map(str,l['key']))} "
                      f"O={l['odds']} ¥{l['stake']:,} 払{l['payout_if_hit']:,}")


if __name__ == "__main__":
    _cli()
