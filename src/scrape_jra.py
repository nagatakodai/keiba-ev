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

from .models import BetOdds, TrifectaOdds

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
            od = _f(re.sub(r"<[^>]+>", " ", odds_cell))   # ワイドは "min - max" の先頭=min
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
    """段1 (開催) token pw15orl1<vv3><yyyy><kk><dd><yyyymmdd>/CK → fields。"""
    m = re.match(r"pw15orl1(\d{3})(\d{4})(\d{2})(\d{2})(\d{8})/", tok)
    if not m:
        return None
    return {"venue": f"{int(m.group(1)):02d}", "year": m.group(2),
            "kai": m.group(3), "day": m.group(4), "date": m.group(5), "token": tok}


def _parse_odds_token(tok: str) -> dict | None:
    """段2 token pw15<bt>ou1<vv3><yyyy><kk><dd><RR><yyyymmdd>Z?/CK → fields。"""
    m = re.match(r"pw15(\d)ou1(\d{3})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})", tok)
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

def fetch_jra_bets(loc: JraLoc) -> dict:
    """全券種を段2 POST で取得 → {other_bets, trifecta, consistency}。"""
    win: list[BetOdds] = []
    place: list[BetOdds] = []
    other: dict[str, list[BetOdds]] = {}
    trifecta: list[TrifectaOdds] = []
    for name, tok in loc.odds_tokens.items():
        try:
            html = _post("accessO.html", tok)
        except Exception:  # noqa: BLE001
            continue
        if name == "tanfuku":
            win, place = parse_tanfuku(html)
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
            "consistency": check_consistency(other_bets, trifecta)}


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


def _main() -> None:
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m src.scrape_jra <netkeiba_jra_race_id>")
        print("       python -m src.scrape_jra --discover")
        raise SystemExit(2)
    if sys.argv[1] == "--discover":
        for r in discover_jra_races()[:40]:
            print(f"  {r['netkeiba_rid']} 場{r['venue']} {r['race_no']}R ({r['date']})")
        return
    loc = find_jra_race(sys.argv[1])
    if not loc:
        print(f"JRA で {sys.argv[1]} を解決できません (直近開催 JRA か / venue+kai+day+R)")
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
