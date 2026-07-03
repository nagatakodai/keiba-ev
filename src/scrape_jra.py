"""JRA 公式 (www.jra.go.jp/JRADB) オッズ/結果 scraper。

netkeiba の IP block と独立した **JRA の公式オッズ源**。全7券種 (単勝/複勝/馬連/
ワイド/馬単/3連複/3連単) + 着順 + 払戻が **組合せ明示の静的 HTML** で取れる
(oddspark のグリッド位置推定問題は JRA には無い)。3連単は 1 POST で全 ordered triple。

仕組み (`accessO.html`/`accessS.html` への form POST チェーン, Shift_JIS):
  段0 オッズ入口   cname=pw15oli00/6D                 → 開催選択 (段1 トークン群)
  段1 開催→レース   cname=pw15orl1<vv><yyyy><kk><dd><yyyymmdd>/CK → レース選択 (段2)
  段2 レース×券種   cname=pw15<bt>ou1<vv><yyyy><kk><dd><RR><yyyymmdd>Z/CK → オッズ
末尾 /CK は checksum で必須かつ推測不可。**HTML の doAction(...,'token') を抽出して
walk** する (netkeiba/oddspark/keiba.go.jp と同じ流儀)。cookie/CSRF 不要、GET は 301。

netkeiba JRA race_id (YYYY VV KK DD RR) ↔ JRA token (vv=00V/year/kai/day/RR/date) は
venue+kai+day+RR で対応づく。**当日/直近開催のみ** (確定オッズは非開催日でも直近分が残る)。
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .ev import LLM_BLEND_DEFAULT, MARKET_BLEND_LIVE
from .models import BetOdds, Horse, PastRun, Race, RaceData, TrifectaOdds
from .parse import _parse_time_to_sec, _split_race_id

_BASE = "https://www.jra.go.jp/JRADB"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_ODDS_ENTRY = "pw15oli00/6D"      # 段0 オッズ入口
_RESULT_ENTRY = "pw01sli00/AF"    # 段0 結果入口

# 券種 → 段2 トークンの btype ダイジット (pw15<bt>ou...)。枠連(3)は EV に使わない。
_BT = {"tanfuku": "1", "quinella": "4", "wide": "5",
       "exacta": "6", "trio": "7", "trifecta": "8"}


class JraError(RuntimeError):
    pass


@dataclass
class JraLoc:
    netkeiba_rid: str        # 12桁 netkeiba JRA race_id
    venue: str               # 2桁 (01-10)
    kai: str
    day: str
    race_no: int
    date: str                # YYYYMMDD
    racelist_token: str      # 段1 (開催) token
    odds_tokens: dict        # {券種: 段2 token}


def _post(html_access: str, cname: str, *, timeout: float = 25.0) -> str:
    """accessO/accessS.html に cname を POST して Shift_JIS デコード済 HTML を返す。"""
    data = urllib.parse.urlencode({"cname": cname}).encode("ascii")
    req = urllib.request.Request(f"{_BASE}/{html_access}", data=data,
                                 headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("shift_jis", "replace")


def _tokens(html: str, access: str = "accessO") -> list[str]:
    """doAction('/JRADB/<access>.html', 'TOKEN') の TOKEN を出現順に抽出 (カンマ後空白許容)。"""
    return re.findall(
        rf"doAction\('/JRADB/{access}\.html',\s*'([^']+)'\)", html)


def _f(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group(0)) if m else None


def _min_odds(s: str) -> float | None:
    """セル内の全数値の最小。ワイド (min/max span) は **常に下限を採用**。"""
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s.replace(",", ""))]
    return min(nums) if nums else None


# ----------------------------------------------------------------- parsers --
# 馬連/ワイド/馬単 共通: <caption>軸</caption> ... <th scope="row">相手</th><td>odds</td>
_CAP_BLOCK_RE = re.compile(r"<caption>(.*?)</caption>(.*?)(?=<caption>|</table>|$)", re.DOTALL)
_TH_TD_RE = re.compile(r'<th scope="row">\s*(\d+)\s*</th>\s*<td[^>]*>(.*?)</td>', re.DOTALL)


def parse_tanfuku(html: str) -> tuple[list[BetOdds], list[BetOdds]]:
    """単複ページ → (win, place)。th.num + td.odds_tan + td.odds_fuku(min採用)。"""
    wins: list[tuple[int, float]] = []
    places: list[tuple[int, float]] = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        nm = re.search(r'<t[hd] class="num"[^>]*>\s*(\d+)\s*</t[hd]>', row)
        tm = re.search(r'<td class="odds_tan"[^>]*>(.*?)</td>', row, re.DOTALL)
        if not nm or not tm:
            continue
        num = int(nm.group(1))
        tan = _f(re.sub(r"<[^>]+>", " ", tm.group(1)))
        if tan is None or tan <= 0:
            continue
        wins.append((num, tan))
        fm = re.search(r'<td class="odds_fuku"[^>]*>(.*?)</td>', row, re.DOTALL)
        if fm:
            fuku = _f(re.sub(r"<[^>]+>", " ", fm.group(1)))  # 先頭=min
            if fuku and fuku > 0:
                places.append((num, fuku))
    return _to_bets(wins, "win"), _to_bets(places, "place")


def _parse_pair(html: str, bet_type: str, *, ordered: bool) -> list[BetOdds]:
    """馬連/ワイド/馬単。caption=軸馬, th=相手馬 (明示), td=odds。ワイドは min 採用。"""
    out: dict[tuple[int, ...], float] = {}
    for cap, body in _CAP_BLOCK_RE.findall(html):
        cm = re.search(r"\d+", cap)
        if not cm:
            continue
        axis = int(cm.group(0))
        for partner, odds_cell in _TH_TD_RE.findall(body):
            p = int(partner)
            if p == axis:
                continue
            cell = re.sub(r"<[^>]+>", " ", odds_cell)
            od = _min_odds(cell) if bet_type == "wide" else _f(cell)  # ワイドは常に下限
            if od is None or od < 1.0:
                continue
            key = (axis, p) if ordered else tuple(sorted((axis, p)))
            out.setdefault(key, od)
    return _rank(out, bet_type)


def parse_quinella(html: str) -> list[BetOdds]:
    return _parse_pair(html, "quinella", ordered=False)


def parse_wide(html: str) -> list[BetOdds]:
    return _parse_pair(html, "wide", ordered=False)


def parse_exacta(html: str) -> list[BetOdds]:
    return _parse_pair(html, "exacta", ordered=True)


def parse_trio(html: str) -> list[BetOdds]:
    """3連複。caption="a-b" (2頭明示), th=3頭目, td=odds → {a,b,c} 昇順。"""
    out: dict[tuple[int, ...], float] = {}
    for cap, body in _CAP_BLOCK_RE.findall(html):
        pair = re.findall(r"\d+", cap)
        if len(pair) != 2:
            continue
        a, b = int(pair[0]), int(pair[1])
        for third, odds_cell in _TH_TD_RE.findall(body):
            c = int(third)
            key = tuple(sorted((a, b, c)))
            if len(set(key)) != 3:
                continue
            od = _f(re.sub(r"<[^>]+>", " ", odds_cell))
            if od is None or od < 1.0:
                continue
            out.setdefault(key, od)
    return _rank(out, "trio")


def parse_trifecta(html: str) -> list[TrifectaOdds]:
    """3連単。sub_header=1着、ブロック内で 2着 区切り → th=3着, td=odds。全 ordered triple。"""
    out: dict[tuple[int, int, int], float] = {}
    blocks = re.split(r'class="[^"]*sub_header', html)[1:]   # 各 = 1着ブロック
    for b in blocks:
        m1 = re.search(r'<span class="num">\s*(\d+)\s*</span>', b)
        if not m1:
            continue
        first = int(m1.group(1))
        # "2着</span> ... <div class="num">N</div>" ごとに区切る (各 = (1着,2着) グループ)
        for seg in re.split(r"2着</span>", b)[1:]:
            mn = re.search(r'<div class="num">\s*(\d+)\s*</div>', seg)
            if not mn:
                continue
            second = int(mn.group(1))
            for third, odds_cell in _TH_TD_RE.findall(seg):
                c = int(third)
                key = (first, second, c)
                if len({first, second, c}) != 3:
                    continue
                od = _f(re.sub(r"<[^>]+>", " ", odds_cell))
                if od is None or od < 1.0:
                    continue
                out.setdefault(key, od)
    bets = sorted(out.items(), key=lambda kv: kv[1])
    return [TrifectaOdds(key=k, odds=v, popularity=i) for i, (k, v) in enumerate(bets, 1)]


def _to_bets(items: list[tuple[int, float]], bet_type: str) -> list[BetOdds]:
    items = sorted(items, key=lambda kv: kv[1])
    return [BetOdds(bet_type=bet_type, key=(n,), odds=o, popularity=i)
            for i, (n, o) in enumerate(items, 1)]


def _rank(out: dict, bet_type: str) -> list[BetOdds]:
    bets = [BetOdds(bet_type=bet_type, key=k, odds=v)
            for k, v in sorted(out.items(), key=lambda kv: kv[1])]
    for i, b in enumerate(bets, 1):
        b.popularity = i
    return bets


# --------------------------------------------------------------- discovery --

def _parse_racelist_token(tok: str) -> dict | None:
    """段1 (開催) token pw15orl<flag><vv><yyyy><kk><dd><yyyymmdd>/CK → fields。

    flag は開催の世代印 (2026-05 時点: 今週=0 / 過去=1)、桁数も venue 桁数も
    JRA 側でしれっと変わる (旧: orl1+vv3, 現: orl0+vv3=実質 orl+00+vv2)。
    `(20\\d{2})` で年をアンカーし、その直前2桁を venue として flag 幅に依存せず拾う。
    """
    m = re.match(r"pw15orl.*?(\d{2})(20\d{2})(\d{2})(\d{2})(\d{8})/", tok)
    if not m:
        return None
    return {"venue": f"{int(m.group(1)):02d}", "year": m.group(2),
            "kai": m.group(3), "day": m.group(4), "date": m.group(5), "token": tok}


def _parse_odds_token(tok: str) -> dict | None:
    """段2 token pw15<bt>ou<flag><vv><yyyy><kk><dd><RR><yyyymmdd>Z?/CK → fields。

    旧 `ou1<vv3>` / 現 `ouS3<vv2>` どちらも flag・venue 桁が違うので、年アンカー
    `(20\\d{2})` の直前2桁を venue とし RR は年の後ろ2桁で拾う (flag 幅非依存)。
    """
    m = re.match(r"pw15(\d)ou.*?(\d{2})(20\d{2})(\d{2})(\d{2})(\d{2})(\d{8})", tok)
    if not m:
        return None
    return {"bt": m.group(1), "venue": f"{int(m.group(2)):02d}", "year": m.group(3),
            "kai": m.group(4), "day": m.group(5), "race_no": int(m.group(6)),
            "date": m.group(7), "token": tok}


def find_jra_race(netkeiba_rid: str) -> JraLoc | None:
    """netkeiba JRA race_id (YYYY VV KK DD RR) → JRA の開催/レース/券種トークン群。

    段0→段1 を walk して venue+kai+day 一致の開催を見つけ、段1→段2 で RR 一致の各券種
    トークンを集める。当日/直近開催 (確定オッズが残る範囲) のみ解決可。
    """
    if len(netkeiba_rid) < 12 or netkeiba_rid[4:6] not in {f"{i:02d}" for i in range(1, 11)}:
        return None  # JRA (場 01-10) のみ
    year, venue, kai, day, rr = (netkeiba_rid[:4], netkeiba_rid[4:6],
                                 netkeiba_rid[6:8], netkeiba_rid[8:10], netkeiba_rid[10:12])
    try:
        top = _post("accessO.html", _ODDS_ENTRY)
    except Exception:  # noqa: BLE001
        return None
    kaisai = None
    for tok in _tokens(top):
        f = _parse_racelist_token(tok)
        if f and f["venue"] == venue and f["year"] == year and f["kai"] == kai and f["day"] == day:
            kaisai = f
            break
    if not kaisai:
        return None
    try:
        rl = _post("accessO.html", kaisai["token"])
    except Exception:  # noqa: BLE001
        return None
    odds_tokens: dict[str, str] = {}
    bt_to_name = {v: k for k, v in _BT.items()}
    for tok in _tokens(rl):
        f = _parse_odds_token(tok)
        if f and f"{f['race_no']:02d}" == rr and f["bt"] in bt_to_name:
            odds_tokens[bt_to_name[f["bt"]]] = tok
    if not odds_tokens:
        return None
    return JraLoc(netkeiba_rid=netkeiba_rid, venue=venue, kai=kai, day=day,
                 race_no=int(rr), date=kaisai["date"],
                 racelist_token=kaisai["token"], odds_tokens=odds_tokens)


def discover_jra_races() -> list[dict]:
    """段0→段1 を walk して直近開催の全レースを列挙 → [{netkeiba_rid, venue, race_no, date}]。"""
    out: list[dict] = []
    try:
        top = _post("accessO.html", _ODDS_ENTRY)
    except Exception:  # noqa: BLE001
        return out
    for tok in _tokens(top):
        k = _parse_racelist_token(tok)
        if not k:
            continue
        try:
            rl = _post("accessO.html", k["token"])
        except Exception:  # noqa: BLE001
            continue
        seen: set[int] = set()
        for ot in _tokens(rl):
            f = _parse_odds_token(ot)
            if not f or f["race_no"] in seen:
                continue
            seen.add(f["race_no"])
            rid = f"{k['year']}{k['venue']}{k['kai']}{k['day']}{f['race_no']:02d}"
            out.append({"netkeiba_rid": rid, "venue": k["venue"],
                        "race_no": f["race_no"], "date": k["date"]})
    return out


# ------------------------------------------------------------------ fetch ---

def parse_jra_horses(html: str) -> dict[int, dict]:
    """単複ページ → {馬番: {name, sex_age, body_weight, body_weight_diff, weight_kg,
    jockey_name, trainer_name, horse_id}}。

    JRA 公式の単複オッズページは odds の他に **馬名/性齢/馬体重(増減)/斤量/騎手/調教師** を
    同じ行に持つ (実機確認 2026-05-31)。netkeiba 出馬表 cache が無い JRA レースでも、ここから
    馬名等を拾えば Claude 考察 (score ステージ) の web 検索が効くようになる。各フィールドは
    取れなければ空のままにし、name が空でも壊さない (best-effort)。
    """
    import html as _htmlmod

    def _clean(s: str) -> str:
        return _htmlmod.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()

    out: dict[int, dict] = {}
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        nm = re.search(r'<t[hd] class="num"[^>]*>\s*(\d+)\s*</t[hd]>', row)
        if not nm or 'class="horse"' not in row:
            continue
        num = int(nm.group(1))
        info: dict = {}
        hm = re.search(r'<td class="horse"[^>]*>(.*?)</td>', row, re.DOTALL)
        if hm:
            info["name"] = _clean(hm.group(1))
            idm = re.search(r"CNAME=([\w/]+)", hm.group(1))
            if idm:
                info["horse_id"] = idm.group(1)
        am = re.search(r'<td class="age"[^>]*>(.*?)</td>', row, re.DOTALL)
        if am:
            info["sex_age"] = _clean(am.group(1))
        wm = re.search(r'<td class="h_weight"[^>]*>\s*(\d+)\s*(?:<span>\s*\(([+\-]?\d+)\))?', row, re.DOTALL)
        if wm:
            info["body_weight"] = int(wm.group(1))
            if wm.group(2):
                info["body_weight_diff"] = int(wm.group(2))
        km = re.search(r'<td class="weight"[^>]*>\s*([\d.]+)\s*</td>', row, re.DOTALL)
        if km:
            try:
                info["weight_kg"] = float(km.group(1))
            except ValueError:
                pass
        jm = re.search(r'<td class="jockey"[^>]*>(.*?)</td>', row, re.DOTALL)
        if jm:
            info["jockey_name"] = _clean(jm.group(1))
        tm = re.search(r'<td class="trainer"[^>]*>(.*?)</td>', row, re.DOTALL)
        if tm:
            info["trainer_name"] = _clean(tm.group(1))
        out[num] = info
    return out


# accessU 馬詳細ページの競走成績テーブル (1 番目の table.basic.narrow-xy.striped)。
# 列: 年月日 / 場 / レース名 / 距離(芝|ダ|障+m) / 馬場 / 頭数 / 人気 / 着順 / 騎手名 /
#     負担重量 / 馬体重 / タイム(自走) / Rt / 1着馬。実機確認 2026-05-31。
_JRA_HIST_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_JRA_HIST_TABLE_RE = re.compile(
    r'<table class="basic narrow-xy striped">(.*?)</table>', re.DOTALL)


def parse_jra_past_runs(html: str, *, limit: int = 12) -> list[PastRun]:
    """accessU (馬詳細) HTML → 競走成績 [PastRun] (新しい順)。

    タイム列は馬の自走時計なので winner_time_sec=own / time_diff_sec=0 で格納
    (own_time_sec プロパティがそのまま自走時計になる、keibago 馬柱と同じ規約)。
    着順は 1/2/3 のみ int・他は None (netkeiba/keibago の学習分布と整合)。
    上3F/通過順/着差は accessU に無いので 0/""。
    """
    import html as _htmlmod

    m = _JRA_HIST_TABLE_RE.search(html)
    if not m:
        return []
    out: list[PastRun] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.DOTALL):
        cells = [_htmlmod.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.DOTALL)]
        if len(cells) < 12:
            continue
        dm = _JRA_HIST_DATE_RE.search(cells[0])
        if not dm:                       # ヘッダ行 (年月日) 等は skip
            continue
        sdm = re.match(r"(芝|ダ|障)?\s*(\d{3,4})", cells[3])
        if not sdm:
            continue
        date = f"{int(dm.group(1))}.{int(dm.group(2))}.{int(dm.group(3))}"
        fin = int(cells[7]) if cells[7] in ("1", "2", "3") else None
        out.append(PastRun(
            date=date,
            venue=cells[1],
            race_class=cells[2],
            # canonical は netkeiba/oddspark と同じ「ダート/障害」(2026-06-11 修正:
            # 旧「ダ」は speed_index/speed_chart の文字列一致に外れ、ダート走が芝の
            # 基準タイム/馬場指数で指数化され speed_v2/pace_v2 が無言で消えていた)。
            surface={"芝": "芝", "ダ": "ダート", "障": "障害"}.get(sdm.group(1) or "", "ダート"),
            distance=int(sdm.group(2)),
            going=cells[4],
            field_size=int(cells[5]) if cells[5].isdigit() else 0,
            popularity=int(cells[6]) if cells[6].isdigit() else 0,
            finish_pos=fin,
            jockey=cells[8],
            weight_kg=float(cells[9]) if re.fullmatch(r"[\d.]+", cells[9]) else 0.0,
            body_weight=int(cells[10]) if cells[10].isdigit() else 0,
            winner_time_sec=_parse_jra_time(cells[11]),
            time_diff_sec=0.0,
        ))
        if len(out) >= limit:
            break
    return out


# accessU の走破タイムは 60 秒未満 (JRA 1000m 戦等) だと "57.8" (コロン無し)。
# parse._parse_time_to_sec は M:SS.D 形式しか受けず 0.0 に落ちていた (2026-06-11 第5R:
# cache 実測で 1000m 走 14/14 が time==0 = スプリント馬の持ち時計が系統的に欠落)。
_PLAIN_SEC_RE = re.compile(r"^\s*(\d{1,2}\.\d)\s*$")


def _parse_jra_time(text: str) -> float:
    sec = _parse_time_to_sec(text)
    if sec > 0:
        return sec
    m = _PLAIN_SEC_RE.match(text or "")
    return float(m.group(1)) if m else 0.0


def fetch_jra_past_runs(horse_id: str, *, limit: int = 12) -> list[PastRun]:
    """accessU CNAME (= 単複ページの horse_id) → 競走成績 [PastRun]。失敗時 []。"""
    if not horse_id:
        return []
    try:
        html = _post("accessU.html", horse_id)
    except Exception:  # noqa: BLE001
        return []
    return parse_jra_past_runs(html, limit=limit)


def parse_jra_race_header(html: str) -> dict:
    """単複ページ → {distance, surface, race_class}。実機ヘッダ (2026-05-31):
    `<div class="txt">3歳未勝利 ... コース： 1,400 メートル （ダート・左）`。
    surface は features の past_runs と同じ語彙 (芝/ダ/障) に正規化。
    """
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    out: dict = {}
    m = re.search(r"コース：\s*([\d,]+)\s*メートル\s*（\s*(芝|ダート|障害)", text)
    if m:
        try:
            out["distance"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
        out["surface"] = m.group(2)   # canonical: 芝/ダート/障害 (2026-06-11 修正)
    # race_class = レース条件セル (例 category "3歳" + class "未勝利" → "3歳未勝利")。
    # 実機: <div class="cell category">3歳</div><div class="cell class">未勝利</div>。
    parts = []
    for cell in ("category", "class"):
        cm = re.search(rf'<div class="cell {cell}">\s*(.*?)\s*</div>', html, re.DOTALL)
        if cm:
            parts.append(re.sub(r"<[^>]+>", "", cm.group(1)).strip())
    rc = "".join(p for p in parts if p)
    if rc:
        out["race_class"] = rc
    return out


def fetch_jra_bets(loc: JraLoc) -> dict:
    """全券種を段2 POST で取得 → {other_bets, trifecta, consistency, horse_info}。"""
    win: list[BetOdds] = []
    place: list[BetOdds] = []
    other: dict[str, list[BetOdds]] = {}
    trifecta: list[TrifectaOdds] = []
    horse_info: dict[int, dict] = {}
    race_header: dict = {}
    for name, tok in loc.odds_tokens.items():
        try:
            html = _post("accessO.html", tok)
        except Exception:  # noqa: BLE001
            continue
        if name == "tanfuku":
            win, place = parse_tanfuku(html)
            try:
                horse_info = parse_jra_horses(html)
            except Exception:  # noqa: BLE001 — 馬情報パースは best-effort (odds は壊さない)
                horse_info = {}
            try:
                race_header = parse_jra_race_header(html)
            except Exception:  # noqa: BLE001
                race_header = {}
        elif name == "quinella":
            other["quinella"] = parse_quinella(html)
        elif name == "wide":
            other["wide"] = parse_wide(html)
        elif name == "exacta":
            other["exacta"] = parse_exacta(html)
        elif name == "trio":
            other["trio"] = parse_trio(html)
        elif name == "trifecta":
            trifecta = parse_trifecta(html)
    other_bets = {"win": win, "place": place, **other}
    return {"other_bets": other_bets, "trifecta": trifecta,
            "consistency": check_consistency(other_bets, trifecta),
            "horse_info": horse_info, "race_header": race_header}


def fetch_jra_win_list(netkeiba_rid: str) -> list[tuple[int, str, float]] | None:
    """JRA の **単勝のみ** を軽量取得 → [(馬番, 馬名, 単勝)]。

    勝負レース screen (src/shobu.py) 用。全券種を walk する fetch_jra_bets は重い ので、
    単複ページ 1 POST だけを引いて馬番/馬名/単勝を返す。解決不能 (当日外/token 無し) は None。
    馬名は単複ページの parse_jra_horses から拾う (best-effort、取れなくても odds は返す)。
    """
    loc = find_jra_race(netkeiba_rid)
    if loc is None or "tanfuku" not in loc.odds_tokens:
        return None
    try:
        html = _post("accessO.html", loc.odds_tokens["tanfuku"])
    except Exception:  # noqa: BLE001 — screen 用途なので例外は呑んで None
        return None
    win, _place = parse_tanfuku(html)
    if not win:
        return None
    try:
        horses = parse_jra_horses(html)
    except Exception:  # noqa: BLE001
        horses = {}
    return [(b.key[0], (horses.get(b.key[0], {}) or {}).get("name", ""), b.odds)
            for b in win if b.key]


def check_consistency(other_bets: dict, trifecta: list) -> dict:
    """ワイド ≤ 馬連 等の健全性チェック (誤オッズ早期検知)。"""
    wide = {tuple(b.key): b.odds for b in other_bets.get("wide", [])}
    quin = {tuple(b.key): b.odds for b in other_bets.get("quinella", [])}
    common = [k for k in wide if k in quin]
    wide_gt = sum(1 for k in common if wide[k] > quin[k] + 1e-9)
    return {
        "n_win": len(other_bets.get("win", [])), "n_quinella": len(quin),
        "n_wide": len(wide), "n_exacta": len(other_bets.get("exacta", [])),
        "n_trio": len(other_bets.get("trio", [])), "n_trifecta": len(trifecta),
        "wide_gt_quinella": wide_gt, "ok": wide_gt == 0 and len(common) > 0,
    }


def parse_jra_result(html: str) -> dict:
    """結果ページ → {finish_order, payout(3連単)}。

    着順は td.place(着順) + td.num(馬番)、3連単配当は li.tierce 内の 組番 + 円。
    accessS 段2 はレース別ページなので tierce はそのレースの確定配当 (組番一致で確認)。
    """
    finish: dict[int, int] = {}
    for m in re.finditer(
        r'<td class="place"[^>]*>(.*?)</td>.*?<td class="num"[^>]*>(.*?)</td>',
        html, re.DOTALL,
    ):
        pl = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        nu = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
        if pl in ("1", "2", "3") and nu.isdigit():
            finish.setdefault(int(pl), int(nu))
    order = [finish[p] for p in (1, 2, 3) if p in finish]
    payout = 0
    tm = re.search(r'class="[^"]*tierce[^"]*"(.*?)</(?:li|tr|td)>', html, re.DOTALL)
    if tm:
        seg = re.sub(r"<[^>]+>", " ", tm.group(1))
        combo = re.search(r"(\d+)\s*-\s*(\d+)\s*-\s*(\d+)", seg)
        yen = re.search(r"([\d,]{2,})\s*円", seg)
        if combo and yen and len(order) >= 3 and \
                [int(combo.group(i)) for i in (1, 2, 3)] == order[:3]:
            payout = int(yen.group(1).replace(",", ""))
    return {"finish_order": order, "payout": payout}


# accessS 結果ページの払戻 <li class="..."> → 内部 bet_type (final_odds の leg_id prefix)。
# wakuren (枠連) は扱わない。実機 DOM 確認 2026-07-04 (福島11R 6/28):
#   <li class="wide"><dl><dt>ワイド</dt><dd><div class="line">
#     <div class="num">8-13</div><div class="yen">1,520<span>円</span></div>...</dd></dl></li>
_JRA_PAYOUT_CLASSES = {
    "win": "win", "place": "place", "umaren": "quinella", "wide": "wide",
    "umatan": "exacta", "trio": "trio", "tierce": "trifecta",
}


def parse_jra_payouts(html: str) -> dict[str, float]:
    """結果ページの払戻セクション → **実払戻ベース** の {leg_id: odds} (100円払戻円 ÷ 100)。

    オッズページ snapshot と違い確定払戻そのものなので、複勝/ワイドのレンジ下限問題
    (実払戻の過小計上) が原理的に起きない。複勝/ワイドは複数 line (同着時は他券種も複数)。
    順不同券種 (place/quinella/wide/trio) の key は昇順に正規化 (final_odds 規約)、
    exacta/trifecta は着順のまま。
    """
    out: dict[str, float] = {}
    for m in re.finditer(r'<li class="([a-z]+)">(.*?)</li>', html, re.DOTALL):
        bt = _JRA_PAYOUT_CLASSES.get(m.group(1))
        if bt is None:
            continue
        expected = {"win": 1, "place": 1, "quinella": 2, "exacta": 2,
                    "wide": 2, "trio": 3, "trifecta": 3}[bt]
        for lm in re.finditer(
            r'<div class="num">\s*([\d\s-]+?)\s*</div>\s*'
            r'<div class="yen">\s*([\d,]+)', m.group(2), re.DOTALL,
        ):
            nums = [int(x) for x in re.findall(r"\d+", lm.group(1))]
            yen = int(lm.group(2).replace(",", ""))
            if yen <= 0 or len(nums) != expected:
                continue
            if bt in ("place", "quinella", "wide", "trio"):
                nums = sorted(nums)
            out[f"{bt}:{'-'.join(str(n) for n in nums)}"] = yen / 100.0
    return out


def fetch_jra_result(netkeiba_rid: str) -> dict | None:
    """netkeiba JRA race_id → JRA 公式の確定結果 {finish_order, payout} (accessS walk)。

    netkeiba block 中でも JRA の結果を取得できる (result fetch の fallback)。直近開催のみ。
    1-2-3 が揃う (len>=3) 確定結果のみ返す。`final_odds` の in-money 組は結果ページの
    **実払戻** (parse_jra_payouts) を採用し、オッズページ snapshot は lookup 被覆用の補完
    (2026-07-04 修正: 旧実装は複勝/ワイドのレンジ下限を実払戻として保存していた)。
    """
    if len(netkeiba_rid) < 12 or netkeiba_rid[4:6] not in {f"{i:02d}" for i in range(1, 11)}:
        return None
    year, venue, kai, day, rr = (netkeiba_rid[:4], netkeiba_rid[4:6],
                                 netkeiba_rid[6:8], netkeiba_rid[8:10], netkeiba_rid[10:12])
    try:
        top = _post("accessS.html", _RESULT_ENTRY)
    except Exception:  # noqa: BLE001
        return None
    # 段1/段2 の結果トークンは doAction('accessS',...) 形式とは限らないので raw 抽出する。
    # flag (今週=0 / 過去=1) と venue 桁数が token 種別ごとに違うので年アンカーで拾う。
    kaisai_tok = None
    for tok in re.findall(r"pw01srl[0-9A-Za-z/]+", top):
        m = re.match(r"pw01srl.*?(\d{2})(20\d{2})(\d{2})(\d{2})\d{8}/", tok)
        if m and f"{int(m.group(1)):02d}" == venue and m.group(2) == year \
                and m.group(3) == kai and m.group(4) == day:
            kaisai_tok = tok
            break
    if not kaisai_tok:
        return None
    try:
        rl = _post("accessS.html", kaisai_tok)
    except Exception:  # noqa: BLE001
        return None
    race_tok = None
    for tok in re.findall(r"pw01sde[0-9A-Za-z/]+", rl):
        m = re.match(r"pw01sde.*?(\d{2})(20\d{2})(\d{2})(\d{2})(\d{2})\d{8}", tok)
        if m and f"{int(m.group(5)):02d}" == rr:
            race_tok = tok
            break
    if not race_tok:
        return None
    try:
        page = _post("accessS.html", race_tok)
    except Exception:  # noqa: BLE001
        return None
    res = parse_jra_result(page)
    if len(res["finish_order"]) < 3:
        return None
    # 最終オッズ snapshot を補完として取得 (calibration の lookup 被覆用)。失敗しても result は返す。
    final: dict[str, float] = {}
    try:
        loc = find_jra_race(netkeiba_rid)
        if loc is not None:
            bets = fetch_jra_bets(loc)
            final = _flatten_final_odds_jra(bets)
    except Exception:  # noqa: BLE001
        final = {}
    # 実払戻 (結果ページの払戻セクション、追加 fetch なし) で上書き。
    try:
        final.update(parse_jra_payouts(page))
    except Exception:  # noqa: BLE001
        pass
    res["final_odds"] = final
    return res


def _flatten_final_odds_jra(bets: dict) -> dict[str, float]:
    """JRA fetch_jra_bets() の結果 → flat {leg_id: final_odds}。
    leg_id 形式は portfolio/llm.leg_id と合わせる (例 `"trifecta:1-2-3"`)。"""
    out: dict[str, float] = {}
    other = bets.get("other_bets") or {}
    for bt, items in other.items():
        for b in items:
            key_str = "-".join(str(k) for k in b.key)
            if b.odds > 0:
                out[f"{bt}:{key_str}"] = float(b.odds)
    for t in bets.get("trifecta") or []:
        if t.odds > 0:
            out[f"trifecta:{t.key[0]}-{t.key[1]}-{t.key[2]}"] = float(t.odds)
    return out


def _pastruns_cache_path(netkeiba_rid: str):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "data" / "cache" /
            "jra_pastruns" / f"{netkeiba_rid}.json")


def _load_jra_pastruns(netkeiba_rid: str) -> dict[int, list[PastRun]] | None:
    import json
    p = _pastruns_cache_path(netkeiba_rid)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        out: dict[int, list[PastRun]] = {}
        for k, runs in raw.items():
            prs = []
            for r in runs:
                # 旧語彙キャッシュの正規化 (2026-06-11 第5R): surface 修正 (「ダ」→
                # 「ダート」) 以前に保存された cache をそのまま読むと修正済バグ
                # (ダート走が芝基準で指数化) が再発する。
                s = r.get("surface")
                if s == "ダ":
                    r["surface"] = "ダート"
                elif s == "障":
                    r["surface"] = "障害"
                prs.append(PastRun(**r))
            out[int(k)] = prs
        return out
    except Exception:  # noqa: BLE001
        return None


def _save_jra_pastruns(netkeiba_rid: str, by_num: dict[int, list[PastRun]]) -> None:
    import dataclasses
    import json
    p = _pastruns_cache_path(netkeiba_rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {str(n): [dataclasses.asdict(r) for r in runs] for n, runs in by_num.items()}
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _date_key_jra(s: str) -> tuple[int, int, int]:
    parts = re.split(r"[./]", s.strip())
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (0, 0, 0)


def build_jra_racedata(netkeiba_rid: str, win_bets: list[BetOdds],
                       horse_info: dict[int, dict] | None = None,
                       race_header: dict | None = None, *,
                       fetch_past: bool = False,
                       target_date: tuple[int, int, int] | None = None) -> RaceData:
    """cache 出馬表が無い場合の RaceData (JRA 公式オッズ + accessU 由来)。

    `horse_info` (単複ページ): 馬名/騎手/性齢/馬体重/斤量 → 各馬に乗せる。
    `race_header` (単複ページ): distance/surface/race_class → Race に乗せる
      (Claude 考察の距離・馬場欠損を解消、distance/surface_fit 特徴量も効く)。
    `fetch_past=True`: accessU を馬ごとに引いて past_runs を構築・キャッシュ
      (`data/cache/jra_pastruns/<rid>.json`)。score ステージで実行 (時間に余裕)、
      bet ステージは fetch_past=False でキャッシュのみ読む (締切直前の latency 回避)。
    `target_date`: leakage 防止。この日付**以降**の run は除外 + 直近5走に制限。
    """
    venue, schedule_index, race_number, cup_id = _split_race_id(netkeiba_rid)
    info = horse_info or {}
    hdr = race_header or {}
    horses = []
    for b in win_bets:
        n = b.key[0]
        d = info.get(n, {})
        horses.append(Horse(
            number=n, name=d.get("name", ""), win_odds=b.odds,
            sex_age=d.get("sex_age", ""), weight_kg=d.get("weight_kg", 0.0),
            body_weight=d.get("body_weight", 0), body_weight_diff=d.get("body_weight_diff", 0),
            jockey_name=d.get("jockey_name", ""), trainer_name=d.get("trainer_name", ""),
            horse_id=d.get("horse_id", ""),
        ))
    # past_runs: キャッシュ → (score 帯のみ) accessU fetch → 無し
    by_num = _load_jra_pastruns(netkeiba_rid)
    if by_num is None and fetch_past:
        by_num = {}
        for h in horses:
            hid = info.get(h.number, {}).get("horse_id", "")
            if hid:
                by_num[h.number] = fetch_jra_past_runs(hid)
        # 全馬 0 走 (網羅的 fetch 失敗 / DOM 変化) は恒久キャッシュしない — 空 dict を
        # 保存すると bet 帯以降この race の馬柱が二度と再試行されない (2026-06-11 第5R)。
        if any(by_num.values()):
            _save_jra_pastruns(netkeiba_rid, by_num)
    if by_num:
        for h in horses:
            runs = by_num.get(h.number, [])
            if target_date:   # leakage: 対象 race 日以降を除外
                runs = [r for r in runs if _date_key_jra(r.date) < target_date]
            h.past_runs = runs[:5]
    race = Race(cup_id=cup_id, schedule_index=schedule_index, race_number=race_number,
                venue_id=int(netkeiba_rid[4:6]) if netkeiba_rid[4:6].isdigit() else 0,
                venue_name=venue, race_class=hdr.get("race_class", ""),
                distance=hdr.get("distance", 0), surface=hdr.get("surface", ""),
                horses=horses)
    return RaceData(race=race, trifecta=[], other_bets={})


def _tag_snapshot_source(race_id: str, source: str) -> None:
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "predictions" / f"{race_id}.json"
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d["odds_source"] = source
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        import os as _os
        _os.replace(tmp, p)   # アトミック (daemon の並行 read 対策)
    except (OSError, json.JSONDecodeError):
        pass


def analyze_jra(netkeiba_rid: str, *, save_snapshot: bool = False, start_at: int = 0,
                with_llm: bool = True, market_blend: float = MARKET_BLEND_LIVE,
                aptitude_top: int = 6, phase: str = "bet",
                llm_blend: float = LLM_BLEND_DEFAULT) -> dict:
    """JRA race を JRA 公式の全券種オッズで解析 (netkeiba 非依存)。

    出馬表/馬柱は data/raw の netkeiba cache があれば使い (確率モデルが効く)、無ければ
    単勝の馬番リストで市場ブレンド主導。consistency NG 時は pair/trio を drop。
    """
    import gzip
    from pathlib import Path

    from . import ev as ev_mod
    from . import portfolio as pf
    from .parse import parse_past_runs, parse_shutuba

    loc = find_jra_race(netkeiba_rid)
    if loc is None:
        raise JraError(f"JRA で {netkeiba_rid} の開催が見つからない (直近開催 JRA か)")
    res = fetch_jra_bets(loc)
    other, trifecta, cons = res["other_bets"], res["trifecta"], res["consistency"]
    horse_info = res.get("horse_info") or {}
    root = Path(__file__).resolve().parents[1]
    sh = root / "data" / "raw" / f"{netkeiba_rid}-shutuba.html.gz"
    # オッズが空 (前売り発売前) でも、cache 出馬表があれば score 段は Claude 指数 (市場非依存) を
    # 先行生成できる (2026-06-24 ユーザ指示)。JRA は roster を win_bets から組む (keibago の DebaTable
    # に相当する公式出馬表パースが未実装) ので、cache が無ければ roster を組めず従来通り raise。
    odds_empty = not other.get("win")
    if odds_empty and (phase != "score" or not sh.exists()):
        raise JraError("JRA オッズが空")
    if not cons["ok"]:
        for bt in ("quinella", "wide", "exacta", "trio"):
            other[bt] = []

    used_cache = False
    if sh.exists():
        rd = parse_shutuba(gzip.open(sh, "rt", encoding="utf-8").read(), race_id=netkeiba_rid)
        past = root / "data" / "raw" / f"{netkeiba_rid}-past.html.gz"
        if past.exists():
            runs = parse_past_runs(gzip.open(past, "rt", encoding="utf-8").read())
            for h in rd.race.horses:
                h.past_runs = runs.get(h.number, [])
        used_cache = True
    else:
        # leakage 防止用の対象 race 日付 (start_at から JST 日付)。live (発走前) は全履歴が
        # 過去なので実質 no-op。past_runs (accessU) は score 帯のみ fetch+cache し、bet 帯は
        # cache を読むだけ (締切直前の latency 回避)。
        target_date = None
        if start_at:
            import datetime as _dt
            d = _dt.datetime.fromtimestamp(start_at)
            target_date = (d.year, d.month, d.day)
        rd = build_jra_racedata(
            netkeiba_rid, other["win"], horse_info, res.get("race_header") or {},
            fetch_past=(phase == "score"), target_date=target_date)

    if start_at and not rd.race.start_at:
        from .parse import close_at_for_start
        rd.race.start_at = start_at
        rd.race.close_at = close_at_for_start(start_at)   # 発走 2 分前 固定

    rd.other_bets = {bt: v for bt, v in other.items() if v}
    rd.trifecta = trifecta

    # 取消/除外の二段ガード (2026-06-10 bughunt): fresh 単勝オッズ (発売中) に無い馬は
    # 取消・除外とみなして absent に昇格 (keibago 経路と同パターン)。cached netkeiba
    # 出馬表経路で stale な取消前の馬が確率・束・Claude 対象に混ざるのを防ぐ。
    _fresh_win = {b.key[0] for b in other.get("win", []) if b.odds > 0}
    if _fresh_win:
        for _h in rd.race.horses:
            if _h.number not in _fresh_win and not _h.absent:
                _h.absent = True
                _h.win_odds = 0.0

    from . import analyze as az_mod
    from .aptitude import compute_aptitudes
    from .features import build_features
    race_id = f"{rd.race.cup_id}-{rd.race.schedule_index}-{rd.race.race_number}"
    has_past = any(h.past_runs for h in rd.race.horses)
    feats = build_features(rd) if (used_cache or has_past) else None
    aptitudes = compute_aptitudes(rd, feats=feats) if feats else None
    try:
        from .market_signal import compute_market_signals
        market_signals = compute_market_signals(rd)
    except Exception:  # noqa: BLE001
        market_signals = None
    best_times = az_mod._serialize_best_times(rd, feats) if feats else []

    # 2段パイプライン score ステージ: Claude 指数をキャッシュし即 return。
    # no_llm でも呼ぶ (オッズ時系列キャプチャは LLM と独立, 2026-06-11 第5R)。
    if phase == "score":
        az_mod._run_score_stage(
            race_id, rd, aptitudes=aptitudes, market_signals=market_signals,
            horse_best_times=best_times, model="opus", no_llm=not with_llm,
            past_source=("netkeiba馬柱" if used_cache else "JRA公式(jra.go.jp)"))
        # Claude 指数が出た段階で予想履歴詳細 (snapshot) を作って表示する (ユーザ指示 2026-06-13)。
        # save_snapshot 時は early return せず下の bet 段ロジックへ fall-through し、指数つき
        # 暫定 snapshot (stage="score") を保存する。3連単買い目の Claude 選定は bet 段のみ
        # (下の claude_trifecta_select 参照)。bet 段が締切直前に fresh odds で再計算・上書きする。
        # 実弾 enqueue は auto_watch の bet phase のみが行うので score の snapshot で賭けは飛ばない。
        # odds_empty (発売前): 市場が無いので基準A/B 用 snapshot は作れない。Claude 指数だけ
        # キャッシュして早期 return。オッズ発売後の再スキャンが snapshot を作る (指数は再利用)。
        if not save_snapshot or odds_empty:
            return {"rd": rd, "loc": loc, "used_cache": used_cache, "phase": "score",
                    "odds_empty": odds_empty}

    # bet ステージ: キャッシュ指数を合成して estimate_probs。
    # score 段の snapshot 構築 (発売後の再スキャン) では朝に先行生成した指数を使うため age gate を
    # 緩める (既定 30 分だと朝の指数が stale 判定で落ちる, 2026-06-24)。
    _llm_max_age = 10**9 if phase == "score" else 1800
    (llm_index, llm_support, llm_scale, llm_scored_at,
     llm_alerts, llm_evidence, llm_paddock) = az_mod._load_llm_scores(
        race_id, max_age_sec=_llm_max_age)

    win_odds = {b.key[0]: b.odds for b in other["win"] if b.odds > 0}
    # fresh 単勝オッズを Horse.win_odds に overlay (oddspark 経路の overlay_oddspark_odds と
    # 同パターン)。cached netkeiba 出馬表利用時は h.win_odds が cache 時点の stale 値のままで、
    # market_anchor_probs が古いオッズで誤判定していた (2026-06-10 bughunt 修正)。
    for _h in rd.race.horses:
        if _h.number in win_odds:
            _h.win_odds = win_odds[_h.number]
    # 未正規化 1/odds (Σ=overround>1) を渡す — de-vig が overround を観測できるように
    # 正規化しない (正規化済みだと power_method_overround が no-op 化する)。
    mwp = {n: 1.0 / o for n, o in win_odds.items()}
    probs = ev_mod.estimate_probs(rd, market_blend=market_blend, market_win_override=mwp,
                                  speed_v2_blend=ev_mod.SPEED_V2_BLEND_LIVE,
                                  llm_win_index=llm_index, llm_blend=llm_blend,
                                  llm_support=llm_support, llm_scale=llm_scale)
    # 3連単束 (実弾) 用の market-free probs (市場無視保証。netkeiba 経路と同パターン —
    # 渡さないと snapshot 側で blended probs にフォールバックし実弾束が市場汚染される)。
    probs_t = probs if market_blend == 0 else ev_mod.estimate_probs(
        rd, market_blend=0.0, speed_v2_blend=ev_mod.SPEED_V2_BLEND_LIVE,
        llm_win_index=llm_index, llm_blend=llm_blend,
        llm_support=llm_support, llm_scale=llm_scale)
    tables = {bt: ev_mod.build_bet_table(rd.other_bets.get(bt, []), probs, bet_type=bt)
              for bt in ("win", "place", "quinella", "wide", "exacta", "trio")}
    tri_table = ev_mod.build_table(rd, probs) if rd.trifecta else []
    cands = [{"bet_type": r.bet_type, "key": list(r.key), "odds": r.odds,
              "prob": r.prob, "px_o": r.px_o, "tier": r.tier}
             for tbl in tables.values() for r in tbl]
    cands += [{"bet_type": "trifecta", "key": list(r.key), "odds": r.odds,
               "prob": r.prob, "px_o": r.px_o, "tier": r.tier} for r in tri_table]
    # CLI 表示束も production と同じ ½Kelly + env bankroll (2026-06-11 第5R)。
    bundle = pf.build_bundle(cands, probs, kelly_fraction=0.5,
                             bankroll=az_mod._ev_bankroll())
    bundle["source"] = "jra"
    tables["trifecta"] = tri_table

    if save_snapshot:
        apt_top = az_mod._aptitude_top_horses(aptitudes, n=aptitude_top) if aptitudes else None
        plan_rows = ev_mod.apply_caps(tri_table)
        snap_bet_tables = {k: v for k, v in tables.items()
                           if k in ("win", "place", "quinella", "wide", "exacta", "trio") and v}
        try:
            az_mod._save_prediction_snapshot(
                race_id, rd, tri_table, plan_rows, aptitudes, snap_bet_tables, apt_top,
                market_signals, feats=feats, lgbm_info=ev_mod.lgbm_status(),
                hit_points=3, probs=probs, probs_t=probs_t,
                llm_win_index=llm_index, llm_blend=llm_blend, llm_scored_at=llm_scored_at,
                llm_support=llm_support, llm_scale=llm_scale, llm_alerts=llm_alerts,
                llm_evidence=llm_evidence, llm_paddock=llm_paddock,
                # 3連単買い目の Claude 選定は **bet 段のみ** (score 段の暫定 snapshot は機械
                # フォーメーション)。--no-llm (with_llm=False) ではキルスイッチとして選定も止める。
                claude_trifecta_select=(with_llm and phase == "bet"),
                stage=phase)
            _tag_snapshot_source(race_id, "jra")
        except Exception as ex:  # noqa: BLE001
            print(f"[analyze_jra] snapshot 保存失敗: {ex}")
        # picks/cuts 選定は廃止 (指数ステップ一本化)。束は probs から build_bundle 済。

    return {"rd": rd, "probs": probs, "loc": loc, "used_cache": used_cache,
            "tables": tables, "bundle": bundle, "consistency": cons}


def _main() -> None:
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args and "--discover" not in sys.argv:
        print("usage: python -m src.scrape_jra <netkeiba_jra_race_id> [--snapshot] [--start-at=UNIX] "
              "[--market-blend=X] [--aptitude-top=N] [--no-llm]")
        print("       python -m src.scrape_jra --discover")
        raise SystemExit(2)
    if "--discover" in sys.argv:
        for r in discover_jra_races()[:40]:
            print(f"  {r['netkeiba_rid']} 場{r['venue']} {r['race_no']}R ({r['date']})")
        return
    if "--snapshot" in sys.argv:
        start_at = 0
        market_blend = MARKET_BLEND_LIVE
        aptitude_top = 6
        phase = "bet"
        llm_blend = LLM_BLEND_DEFAULT
        for a in sys.argv:
            if a.startswith("--start-at="):
                start_at = int(a.split("=", 1)[1] or 0)
            elif a.startswith("--market-blend="):
                try:
                    market_blend = float(a.split("=", 1)[1])
                except ValueError:
                    pass
            elif a.startswith("--llm-blend="):
                try:
                    llm_blend = float(a.split("=", 1)[1])
                except ValueError:
                    pass
            elif a.startswith("--phase="):
                phase = a.split("=", 1)[1].strip() or "bet"
            elif a.startswith("--aptitude-top="):
                try:
                    aptitude_top = int(a.split("=", 1)[1])
                except ValueError:
                    pass
        try:
            res = analyze_jra(args[0], save_snapshot=True, start_at=start_at,
                              with_llm="--no-llm" not in sys.argv,
                              market_blend=market_blend, aptitude_top=aptitude_top,
                              phase=phase, llm_blend=llm_blend)
        except JraError as ex:
            print(f"JRA 解析不能 ({args[0]}): {ex}")
            raise SystemExit(1)
        loc = res["loc"]
        if res.get("phase") == "score":
            print(f"=== JRA 場{loc.venue} {loc.race_no}R score ステージ完了 (指数キャッシュ) ===")
            return
        c = res["consistency"]
        src = "cache 出馬表+馬柱" if res["used_cache"] else "馬リストのみ(市場主導)"
        print(f"=== JRA 場{loc.venue} {loc.race_no}R snapshot 保存 ({src}) "
              f"ok={c['ok']} bundle脚={len(res['bundle'].get('legs', []))} ===")
        return
    loc = find_jra_race(args[0])
    if not loc:
        print(f"JRA で {args[0]} を解決できません (直近開催 JRA か / venue+kai+day+R)")
        raise SystemExit(1)
    print(f"=== JRA 場{loc.venue} {loc.race_no}R ({loc.date}) 券種 {list(loc.odds_tokens)} ===")
    res = fetch_jra_bets(loc)
    c = res["consistency"]
    print(f"単勝{c['n_win']} 馬連{c['n_quinella']} ワイド{c['n_wide']} 馬単{c['n_exacta']} "
          f"3連複{c['n_trio']} 3連単{c['n_trifecta']} | ワイド>馬連異常={c['wide_gt_quinella']} ok={c['ok']}")
    print("単勝人気上位:", [(b.key[0], b.odds) for b in res["other_bets"]["win"][:5]])
    print("3連単最安:", [(t.label, t.odds) for t in res["trifecta"][:3]])


if __name__ == "__main__":
    _main()
