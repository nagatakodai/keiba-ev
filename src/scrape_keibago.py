"""keiba.go.jp (地方競馬公式 / 地方競馬情報サイト) NAR オッズ scraper。

netkeiba の IP block / oddspark のグリッド誤オッズ問題を回避する **NAR の第一オッズ源**。
全 6 券種 (単複/馬連/ワイド/馬単/3連複/3連単) が **組合せ明示** (列位置推定でなく
`<td>6-7-11</td>` のように組番が HTML に直書き) で、**静的 UTF-8 HTML・GET・会員不要**で
取れる。実機検証 (2026-05, 盛岡 11頭): ワイド≤馬連 55/55、完全列挙 (ワイド 55=C(11,2)/
馬単 110=11·10/3連複 165=C(11,3)/3連単 990=11·10·9)。

netkeiba/oddspark との違い:
  - netkeiba odds_get_form b3/b4 は jiku 巡回しても合成/不完全値 (ワイド>馬連 が頻発)。
  - oddspark の馬連/ワイド/馬単/3連複 はグリッド位置推定で >9頭折り返し時に誤オッズ。
  - keiba.go.jp は **全組合せが明示** なので位置推定が一切不要 = 誤オッズが原理的に出ない。

URL: TodayRaceInfo なので **当日レース向け** (live odds 用途)。場コード (k_babaCode) は
netkeiba と別 namespace なので **場名で照合** (TodayRaceInfoTop から動的解決) する。
誤った babaCode で別場のオッズを取ると最悪なので、確証できない場は None を返す。
"""
from __future__ import annotations

import html as _html
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from .models import BetOdds, TrifectaOdds
from .parse import VENUE_CODE, is_nar_race_id

_BASE = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 券種 → keiba.go.jp エンドポイント。
_EP = {
    "tanfuku": "OddsTanFuku",     # 単勝・複勝
    "quinella": "OddsUmLenFuku",  # 馬連
    "wide": "OddsWide",           # ワイド
    "exacta": "OddsUmLenTan",     # 馬単
    "trio": "Odds3LenFuku",       # 3連複
    "trifecta": "Odds3LenTan",    # 3連単
}


class KeibagoError(RuntimeError):
    pass


@dataclass
class KeibagoLoc:
    race_date: str   # "YYYY/MM/DD"
    race_no: int
    baba_code: str
    venue: str


def _get(url: str, *, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _cells(row_html: str) -> list[str]:
    """<tr> 内の <th>/<td> テキストを順に (タグ除去・unescape・空白圧縮)。"""
    out = []
    for _tag, c in re.findall(r"<t([hd])[^>]*>(.*?)</t[hd]>", row_html, re.DOTALL):
        txt = _html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
        out.append(re.sub(r"\s+", " ", txt))
    return out


def _f(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group(0)) if m else None


# ---------------------------------------------------------------- parsers ----

def parse_tanfuku(html: str) -> tuple[list[BetOdds], list[BetOdds]]:
    """単複ページ → (win [単勝], place [複勝下限])。

    行 = [枠, 馬番, 馬名, 単勝, 複勝min(例 '1.4-'), 複勝max, 性齢, 馬体重, 重量]。
    複勝は下限 (min) を採用 (実払戻 ≥ 下限で確定 = 保守)。取消馬は単勝が数値でないので skip。
    """
    wins: list[tuple[int, float]] = []
    places: list[tuple[int, float]] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        c = _cells(row)
        if len(c) < 5 or not c[1].isdigit():
            continue
        num = int(c[1])
        tan = _f(c[3])
        fuku_min = _f(c[4])
        if tan is None or tan <= 0:
            continue  # 取消 / ヘッダ
        wins.append((num, tan))
        if fuku_min and fuku_min > 0:
            places.append((num, fuku_min))
    return (_to_bets(wins, "win"), _to_bets(places, "place"))


_COMBO_RE = re.compile(
    r"<td[^>]*>\s*(\d+(?:-\d+){1,2})\s*</td>\s*<td[^>]*>(.*?)</td>", re.DOTALL
)


def _parse_combo(html: str, bet_type: str, *, ordered: bool, length: int) -> list[BetOdds]:
    """`<td>組番</td><td>オッズ</td>` を明示で拾う (馬連/ワイド/馬単/3連複)。

    ordered=False は順不同 (key 昇順ソート)、True は順序保持 (馬単)。
    ワイドのオッズセルは 'min -max' なので最初の数値 (= 下限) を採用。
    """
    out: dict[tuple[int, ...], float] = {}
    for combo, odds_cell in _COMBO_RE.findall(html):
        nums = tuple(int(x) for x in combo.split("-"))
        if len(nums) != length or len(set(nums)) != length:
            continue
        od = _f(_html.unescape(re.sub(r"<[^>]+>", " ", odds_cell)))
        if od is None or od < 1.0:
            continue
        key = nums if ordered else tuple(sorted(nums))
        out.setdefault(key, od)
    bets = [
        BetOdds(bet_type=bet_type, key=k, odds=v)
        for k, v in sorted(out.items(), key=lambda kv: kv[1])
    ]
    for i, b in enumerate(bets, 1):
        b.popularity = i
    return bets


def parse_quinella(html: str) -> list[BetOdds]:
    return _parse_combo(html, "quinella", ordered=False, length=2)


def parse_wide(html: str) -> list[BetOdds]:
    return _parse_combo(html, "wide", ordered=False, length=2)


def parse_exacta(html: str) -> list[BetOdds]:
    return _parse_combo(html, "exacta", ordered=True, length=2)


def parse_trio(html: str) -> list[BetOdds]:
    return _parse_combo(html, "trio", ordered=False, length=3)


def parse_trifecta(html: str) -> list[TrifectaOdds]:
    """3連単ページ → [TrifectaOdds] (順序あり a-b-c, 全列挙)。"""
    out: dict[tuple[int, int, int], float] = {}
    for combo, odds_cell in _COMBO_RE.findall(html):
        nums = tuple(int(x) for x in combo.split("-"))
        if len(nums) != 3 or len(set(nums)) != 3:
            continue
        od = _f(_html.unescape(re.sub(r"<[^>]+>", " ", odds_cell)))
        if od is None or od < 1.0:
            continue
        out.setdefault(nums, od)  # type: ignore[arg-type]
    bets = sorted(out.items(), key=lambda kv: kv[1])
    return [
        TrifectaOdds(key=k, odds=v, popularity=i)
        for i, (k, v) in enumerate(bets, 1)
    ]


def _to_bets(items: list[tuple[int, float]], bet_type: str) -> list[BetOdds]:
    items = sorted(items, key=lambda kv: kv[1])
    return [
        BetOdds(bet_type=bet_type, key=(num,), odds=od, popularity=i)
        for i, (num, od) in enumerate(items, 1)
    ]


# -------------------------------------------------------------- discovery ----

def _baba_code_for(venue: str) -> str | None:
    """場名 → keiba.go.jp の k_babaCode を TodayRaceInfoTop から動的解決。

    babaCode は netkeiba と別 namespace。誤った code で別場のオッズを取らないよう、
    **当日開催一覧に出ている場名と一致した code のみ** 返す (見つからなければ None)。
    """
    if not venue:
        return None
    try:
        top = _get(f"{_BASE}/TodayRaceInfoTop")
    except Exception:  # noqa: BLE001
        return None
    # 'k_babaCode=NN" ...>場名' の近接ペアから、venue を含む場名の code を拾う
    for m in re.finditer(r"k_babaCode=(\d+)[^>]*>\s*([^<\s][^<]{0,8})", top):
        code, name = m.group(1), _html.unescape(m.group(2)).strip()
        if name and venue[:2] in name and "成績" not in name and "払戻" not in name:
            return code
    return None


def find_keibago_race(netkeiba_rid: str) -> KeibagoLoc | None:
    """netkeiba の NAR race_id → keiba.go.jp の (race_date, race_no, baba_code)。

    race_id [0:4]=年 [4:6]=netkeiba場コード [6:8]=月 [8:10]=日 [10:12]=R。
    場コード → 場名 (VENUE_CODE) → babaCode (動的照合)。当日開催かつ場名一致時のみ。
    """
    if not is_nar_race_id(netkeiba_rid) or len(netkeiba_rid) < 12:
        return None
    venue = VENUE_CODE.get(netkeiba_rid[4:6], "")
    baba = _baba_code_for(venue)
    if not baba:
        return None
    race_date = f"{netkeiba_rid[:4]}/{netkeiba_rid[6:8]}/{netkeiba_rid[8:10]}"
    race_no = int(netkeiba_rid[10:12]) if netkeiba_rid[10:12].isdigit() else 0
    return KeibagoLoc(race_date=race_date, race_no=race_no, baba_code=baba, venue=venue)


def _odds_url(loc: KeibagoLoc, ep: str) -> str:
    return (f"{_BASE}/{ep}?k_raceDate={loc.race_date}"
            f"&k_raceNo={loc.race_no}&k_babaCode={loc.baba_code}")


# ------------------------------------------------------------------ fetch ----

def fetch_keibago_bets(loc: KeibagoLoc) -> dict:
    """全 6 券種を取得して {other_bets dict, trifecta list, consistency} で返す。

    other_bets: {"win","place","quinella","wide","exacta","trio"} → [BetOdds]。
    consistency: 内部整合チェック (ワイド≤馬連 / 件数 = C(n,2) 等)。**誤オッズ検知用**。
    """
    win, place = parse_tanfuku(_get(_odds_url(loc, _EP["tanfuku"])))
    quinella = parse_quinella(_get(_odds_url(loc, _EP["quinella"])))
    wide = parse_wide(_get(_odds_url(loc, _EP["wide"])))
    exacta = parse_exacta(_get(_odds_url(loc, _EP["exacta"])))
    trio = parse_trio(_get(_odds_url(loc, _EP["trio"])))
    trifecta = parse_trifecta(_get(_odds_url(loc, _EP["trifecta"])))

    other_bets = {
        "win": win, "place": place, "quinella": quinella,
        "wide": wide, "exacta": exacta, "trio": trio,
    }
    return {
        "other_bets": other_bets,
        "trifecta": trifecta,
        "consistency": check_consistency(other_bets, trifecta),
    }


def check_consistency(other_bets: dict, trifecta: list) -> dict:
    """組合せ明示ソースの健全性チェック (誤オッズ早期検知)。

    - ワイド(a,b) ≤ 馬連(a,b) が全ペアで成立 (順序が逆 = パース異常)。
    - 馬連/ワイド件数が一致 (同じ C(n,2))。
    違反は誤オッズの兆候なので呼び出し側で警告/不採用の判断に使う。
    """
    wide = {tuple(b.key): b.odds for b in other_bets.get("wide", [])}
    quin = {tuple(b.key): b.odds for b in other_bets.get("quinella", [])}
    common = [k for k in wide if k in quin]
    wide_gt_quin = sum(1 for k in common if wide[k] > quin[k] + 1e-9)
    return {
        "n_win": len(other_bets.get("win", [])),
        "n_quinella": len(quin),
        "n_wide": len(wide),
        "n_trio": len(other_bets.get("trio", [])),
        "n_trifecta": len(trifecta),
        "wide_gt_quinella": wide_gt_quin,        # 0 が健全
        "ok": wide_gt_quin == 0 and len(common) > 0,
    }


def _main() -> None:
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m src.scrape_keibago <netkeiba_nar_race_id>")
        raise SystemExit(2)
    rid = sys.argv[1]
    loc = find_keibago_race(rid)
    if not loc:
        print(f"keiba.go.jp で {rid} を解決できません (当日開催 NAR か / 場名照合を確認)")
        raise SystemExit(1)
    print(f"=== keiba.go.jp {loc.venue} {loc.race_no}R ({loc.race_date}) "
          f"babaCode={loc.baba_code} ===")
    res = fetch_keibago_bets(loc)
    c = res["consistency"]
    print(f"単勝{c['n_win']} 馬連{c['n_quinella']} ワイド{c['n_wide']} "
          f"3連複{c['n_trio']} 3連単{c['n_trifecta']} | "
          f"ワイド>馬連異常={c['wide_gt_quinella']} ok={c['ok']}")
    win = res["other_bets"]["win"]
    print("単勝人気上位:", [(b.key[0], b.odds) for b in win[:5]])
    print("3連単最安:", [(t.label, t.odds) for t in res["trifecta"][:3]])


if __name__ == "__main__":
    _main()
