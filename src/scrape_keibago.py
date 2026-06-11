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

from .ev import LLM_BLEND_DEFAULT, MARKET_BLEND_LIVE
from .models import BetOdds, Horse, PastRun, Race, RaceData, TrifectaOdds
from .parse import VENUE_CODE, _split_race_id, is_nar_race_id

_BASE = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo"
_DATAROOM = "https://www.keiba.go.jp/KeibaWeb/DataRoom"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_DATE_RE = re.compile(r"20\d\d[./]\d{1,2}[./]\d{1,2}")
# 競走成績の行は NAR / JRA で列レイアウトが違う (JRA 履歴は距離が surface 付き・列数も多い)。
# 固定 index でなく値パターンで錨を打つ: タイム (M:SS.D / SS.D, 40秒以上) と
# 距離 (surface 任意 + 800-4000) を見つけ、着順/人気/馬番はタイム直前から相対で読む。
_GOING_SET = {"良", "稍重", "重", "不良"}
_TIME_CELL_RE = re.compile(r"(?:\d+:)?\d{1,2}\.\d\s*$")
_DIST_CELL_RE = re.compile(r"(芝|ダ|障)?\s*(\d{3,4})\s*$")

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


def _min_odds(s: str) -> float | None:
    """セル内の全数値の最小を返す。ワイドは "min - max" のレンジ表示なので
    **常に下限を採用** (HTML の min/max の並び順に依存しない保証)。"""
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s.replace(",", ""))]
    return min(nums) if nums else None


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
        if not all(1 <= n <= 30 for n in nums):
            continue  # 馬番域外 (例: 日付 2026-05-26) を弾く
        cell = _html.unescape(re.sub(r"<[^>]+>", " ", odds_cell))
        od = _min_odds(cell) if bet_type == "wide" else _f(cell)  # ワイドは常に下限
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
        if not all(1 <= n <= 30 for n in nums):
            continue  # 馬番域外 (例: 日付 2026-05-26) を弾く
        od = _f(_html.unescape(re.sub(r"<[^>]+>", " ", odds_cell)))
        if od is None or od < 1.0:
            continue
        out.setdefault(nums, od)  # type: ignore[arg-type]
    bets = sorted(out.items(), key=lambda kv: kv[1])
    return [
        TrifectaOdds(key=k, odds=v, popularity=i)
        for i, (k, v) in enumerate(bets, 1)
    ]


def parse_horse_list(html: str) -> list[tuple[int, str, float]]:
    """単複ページ → [(馬番, 馬名, 単勝)]。cache 出馬表が無い時の出走馬ソース。"""
    out: list[tuple[int, str, float]] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        c = _cells(row)
        if len(c) < 4 or not c[1].isdigit():
            continue
        tan = _f(c[3])
        if tan is None or tan <= 0:
            continue
        out.append((int(c[1]), c[2], tan))
    return out


def _time_sec(s: str) -> float:
    """'1:27.4' / '41.9' → 秒。"""
    m = re.match(r"(?:(\d+):)?(\d+(?:\.\d+)?)\s*$", s.strip())
    if not m:
        return 0.0
    return (int(m.group(1)) if m.group(1) else 0) * 60 + float(m.group(2))


def _date_key(s: str) -> tuple[int, int, int]:
    """'YYYY.M.D' / 'YYYY/M/D' → (y,m,d)。文字列比較の非ゼロ詰めバグ回避 (タプル比較)。"""
    parts = re.split(r"[./]", s.strip())
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (0, 0, 0)


def parse_deba_table(html: str) -> list[tuple[int, str, str, bool]]:
    """出馬表 (DebaTable) → [(馬番, 馬名, k_lineageLoginCode, absent)]。

    各馬は `<td rowspan="5" class="horseNum">N</td>` に続く
    `<a class="horseName" href="../DataRoom/HorseMarkInfo?k_lineageLoginCode=CODE">名</a>`。
    CODE で競走成績 (HorseMarkInfo) を引いて馬柱 (past_runs) を構築する。

    **馬ブロック単位で探す**: 取消等でリンクが無い馬があっても、その馬の馬番を
    **次の馬の CODE と誤ってペアにしない** (= 馬柱を別馬に付ける最悪の取り違えを防ぐ)。
    リンクが無い馬は code 空で残す (馬は落とさない)。

    **absent (2026-06-10 bughunt 修正, 第2版)**: ブロック内の **全文語**
    (出走取消/競走除外/発走除外) を検出して absent=True を返す。旧実装は取消馬を
    出走馬として返しており、estimate_probs (absent でしかフィルタしない) で取消馬に
    確率が割り当てられ、実弾3連単束のフォーメーションが崩壊していた (実測: 笠松6R で
    取消馬が rank 2位)。
    第1版の「</table> で打ち切ってから検出」は**実ページで常に no-op だった**:
    各馬ブロックには馬柱ミニテーブルの </table> が必ず含まれ、取消表示
    (`<td class="info">出走取消</td>`) はその後に来るため検出されなかった。
    打ち切りは撤去し、ブロック全体を全文語のみで検索する — bare「取消」を使うと
    現役馬の馬柱の過去走取消歴 (pastRank) に誤反応する (実測: 本日 12 レース 13 頭)
    ため全文語限定が必須。全文語はページ凡例にも pastRank にも現れないことを
    本日の全取消ありレース (5/5) で実測確認済。
    """
    out: list[tuple[int, str, str, bool]] = []
    marks = list(re.finditer(r'class="horseNum"[^>]*>\s*(\d+)\s*</td>', html))
    for i, m in enumerate(marks):
        num = int(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        block = html[m.end():end]   # この馬のブロック内だけを探索
        lm = re.search(r'k_lineageLoginCode=(\d+)"[^>]*>\s*([^<]+?)\s*</a>', block)
        absent = bool(re.search(r"出走取消|競走除外|発走除外", block))
        out.append((num, lm.group(2).strip() if lm else "",
                    lm.group(1) if lm else "", absent))
    return out


def parse_horse_history(html: str, *, limit: int = 12) -> list[PastRun]:
    """競走成績 (HorseMarkInfo) → [PastRun] (新しい順)。

    成績表 = 日付が最も多く並ぶ <table>。**1頭の履歴に NAR 行と JRA 行が混在し列
    レイアウトが異なる** (JRA 履歴は距離が "芝2000" 形式で列数も多い) ため、固定 index
    でなく**値パターンで錨**を打つ:
      - タイム = M:SS.D / SS.D 形式かつ 40 秒以上のセル (着差 <10 や 上3F と区別、
        かつタイムは着差/上3Fより前にあるので左から最初の一致)。
      - 距離+馬場 = surface(芝/ダ/障)任意 + 3-4桁(800-4000) のセル。surface 無しは NAR=ダ。
      - 着順/人気/馬番 = タイム直前から相対 (…馬番, 人気, 着順, タイム)。
      - 頭数 = 馬場セルの直後。
    タイムは馬の自走時計 (winner_time_sec=own, time_diff_sec=0)。着順は 1/2/3 のみ int。
    """
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    best = max(tables, key=lambda x: len(_DATE_RE.findall(x)), default="")
    if len(_DATE_RE.findall(best)) < 1:
        return []
    out: list[PastRun] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", best, re.DOTALL):
        raw = [_html.unescape(re.sub(r"<[^>]+>", " ", x)).strip()
               for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.DOTALL)]
        c = [x for x in raw if x]   # 空セル (spacer) を除去
        if len(c) < 10 or not _DATE_RE.match(c[0]):
            continue
        # タイム錨 (レース時計): 左から最初の time 形式かつ ≥40 秒
        ti = next((i for i in range(2, len(c))
                   if _TIME_CELL_RE.match(c[i]) and _time_sec(c[i]) >= 40.0), None)
        if ti is None or ti < 4:
            continue
        # 距離+surface (タイムより前のセルから)
        dm = next((m for cell in c[2:ti]
                   if (m := _DIST_CELL_RE.match(cell)) and 800 <= int(m.group(2)) <= 4000), None)
        if dm is None:
            continue
        # canonical は「ダート/障害」(netkeiba/oddspark と同語彙, 2026-06-11 修正)
        surface = {"芝": "芝", "ダ": "ダート", "障": "障害"}.get(dm.group(1) or "", "ダート")
        # 馬場 (距離の後 / タイムの前) と頭数 (馬場の直後)。ナイター開催は馬場と頭数の
        # 間に「ナ」印セルが挟まるので 1 つだけ読み飛ばす (2026-06-11 bughunt 第5R:
        # 旧実装は直後セル固定で、ナイター走の field_size が全て 0 に落ちていた)。
        going, field_size = "", 0
        for i in range(2, ti):
            if c[i] in _GOING_SET:
                going = c[i]
                j = i + 1
                if j < ti and c[j] == "ナ":
                    j += 1
                if j < ti and c[j].isdigit():
                    field_size = int(c[j])
                break
        fin = int(c[ti - 1]) if c[ti - 1] in ("1", "2", "3") else None
        out.append(PastRun(
            date=c[0].replace("/", "."),
            venue=c[1],
            race_no=int(c[2]) if c[2].isdigit() else 0,
            race_class=c[3] if len(c) > 3 else "",
            surface=surface,
            distance=int(dm.group(2)),
            going=going,
            field_size=field_size,
            horse_number=int(c[ti - 3]) if c[ti - 3].isdigit() else 0,
            popularity=int(c[ti - 2]) if c[ti - 2].isdigit() else 0,
            finish_pos=fin,
            winner_time_sec=_time_sec(c[ti]),
            time_diff_sec=0.0,
        ))
        if len(out) >= limit:
            break
    return out


def fetch_horse_past_runs(lineage_code: str) -> list[PastRun]:
    if not lineage_code:
        return []
    try:
        return parse_horse_history(
            _get(f"{_DATAROOM}/HorseMarkInfo?k_lineageLoginCode={lineage_code}"))
    except Exception:  # noqa: BLE001
        return []


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


def build_keibago_racedata(
    netkeiba_rid: str,
    deba: list[tuple[int, str, str, bool]],
    win_odds: dict[int, float],
    *,
    fetch_past: bool = True,
) -> RaceData:
    """cache 出馬表が無い場合の RaceData を keiba.go.jp の出馬表(DebaTable)から構築。

    deba = [(馬番, 馬名, k_lineageLoginCode, absent)]。fetch_past=True で各馬の HorseMarkInfo
    (競走成績) から **馬柱 (past_runs) を取得**して付与 → build_features が効き、
    estimate_probs が市場主導でなくモデルの edge を反映できる (= netkeiba/oddspark 非依存)。
    leakage 防止: HorseMarkInfo は対象 race 自身も含むので **対象 race 日付以降を除外** +
    直近5走に制限 (netkeiba 馬柱の窓に合わせる)。live (発走前) では対象 race は未走で no-op。
    取消馬 (absent=True) は Horse.absent を立て、馬柱 fetch も skip する。
    """
    venue, schedule_index, race_number, cup_id = _split_race_id(netkeiba_rid)
    horse_rows = [Horse(number=n, name=nm, win_odds=win_odds.get(n, 0.0), absent=ab)
                  for n, nm, _ln, ab in deba]
    if fetch_past:
        race_date = (f"{netkeiba_rid[:4]}.{netkeiba_rid[6:8]}.{netkeiba_rid[8:10]}"
                     if is_nar_race_id(netkeiba_rid) else "")
        ln_by_num = {n: ln for n, _nm, ln, _ab in deba}
        for h in horse_rows:
            if h.absent:
                continue   # 取消馬の馬柱は取らない (fundamental 汚染 + 無駄 fetch 防止)
            ln = ln_by_num.get(h.number, "")
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


def analyze_keibago(netkeiba_rid: str, *, save_snapshot: bool = False, start_at: int = 0,
                    with_llm: bool = True, market_blend: float = MARKET_BLEND_LIVE,
                    aptitude_top: int = 6, phase: str = "bet",
                    llm_blend: float = LLM_BLEND_DEFAULT) -> dict:
    """NAR race を keiba.go.jp の全6券種オッズで解析 (netkeiba/oddspark の上位互換)。

    出馬表/馬柱は data/raw の netkeiba cache があれば使い (確率モデルが効く)、無ければ
    keiba.go.jp 単複の馬リストで市場ブレンド主導の確率を出す。**check_consistency で
    ワイド>馬連 等を検知したら pair/trio 系を drop** (誤オッズより見送り)。
    save_snapshot=True で data/predictions/<race_id>.json を保存。
    """
    import gzip
    from pathlib import Path

    from . import ev as ev_mod
    from . import portfolio as pf
    from .parse import parse_past_runs, parse_shutuba

    loc = find_keibago_race(netkeiba_rid)
    if loc is None:
        raise KeibagoError(f"keiba.go.jp で {netkeiba_rid} の開催が見つからない (当日 NAR/場名)")
    res = fetch_keibago_bets(loc)
    other = res["other_bets"]
    trifecta = res["trifecta"]
    cons = res["consistency"]
    if not other.get("win"):
        raise KeibagoError("keiba.go.jp オッズが空")
    # 安全ゲート: ワイド>馬連 等の異常を検知したら pair/trio を捨てる (誤オッズを出さない)
    if not cons["ok"]:
        for bt in ("quinella", "wide", "exacta", "trio"):
            other[bt] = []

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
        used_cache = True
    else:
        win_odds = {b.key[0]: b.odds for b in other["win"] if b.odds > 0}
        deba = parse_deba_table(_get(_odds_url(loc, "DebaTable")))
        if deba:
            # 出馬表 + HorseMarkInfo の馬柱で確率モデルをフル稼働 (公式自給)
            rd = build_keibago_racedata(netkeiba_rid, deba, win_odds, fetch_past=True)
        else:
            # DebaTable 取れず → 単複の馬リストのみ (past_runs なし=市場ブレンド主導)。
            # 単複ページは発売対象馬のみ (取消馬は単勝が数値でなく載らない) → absent=False。
            hl = parse_horse_list(_get(_odds_url(loc, _EP["tanfuku"])))
            rd = build_keibago_racedata(
                netkeiba_rid, [(n, nm, "", False) for n, nm, _od in hl], win_odds,
                fetch_past=False)

    if start_at and not rd.race.start_at:
        from .parse import close_at_for_start
        rd.race.start_at = start_at
        rd.race.close_at = close_at_for_start(start_at)   # 発走 2 分前 固定

    rd.other_bets = {bt: v for bt, v in other.items() if v}
    rd.trifecta = trifecta

    # 取消/除外の二段ガード (2026-06-10 bughunt): fresh 単勝オッズ (発売中) に無い馬は
    # 取消・除外とみなして absent に昇格する。DebaTable の absent 検出 (一段目) の保険 +
    # cached netkeiba 出馬表経路 (stale な取消前の馬が残る) も塞ぐ。score の Claude 対象・
    # 確率 (estimate_probs は absent でフィルタ)・束から外れる。
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
            horse_best_times=best_times, model="opus", no_llm=not with_llm)
        return {"rd": rd, "loc": loc, "used_cache": used_cache, "phase": "score"}

    # bet ステージ: キャッシュ指数を合成して estimate_probs。
    llm_index, llm_support, llm_scale, llm_scored_at, llm_alerts = az_mod._load_llm_scores(race_id)

    win_odds = {b.key[0]: b.odds for b in other["win"] if b.odds > 0}
    # fresh 単勝オッズを Horse.win_odds に overlay (oddspark 経路の overlay_oddspark_odds と
    # 同パターン)。cached netkeiba 出馬表利用時は h.win_odds が cache 時点の stale 値のままで、
    # _market_favorite (recovery モードの1番人気特定・1.5倍帯ゲート) と market_anchor_probs が
    # 古いオッズで誤判定していた (2026-06-10 bughunt 修正)。
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
    # 3連単束 (実弾) 用の market-free probs。market_blend>0 (live 既定 0.78) のとき
    # probs は市場ブレンド済みなので、市場無視保証のため別計算して snapshot へ渡す
    # (netkeiba 経路 analyze.py と同パターン。渡さないと probs にフォールバックして
    # 実弾束の配分・トリガミ判定が市場汚染される)。
    probs_t = probs if market_blend == 0 else ev_mod.estimate_probs(
        rd, market_blend=0.0, speed_v2_blend=ev_mod.SPEED_V2_BLEND_LIVE,
        llm_win_index=llm_index, llm_blend=llm_blend,
        llm_support=llm_support, llm_scale=llm_scale)
    tables = {bt: ev_mod.build_bet_table(rd.other_bets.get(bt, []), probs, bet_type=bt)
              for bt in ("win", "place", "quinella", "wide", "exacta", "trio")}
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
    # CLI 表示束も production (analyze._save_prediction_snapshot) と同じ ½Kelly +
    # env bankroll で組む — full Kelly 既定のままだと手動投票を2倍サイズに誘導する
    # (2026-06-11 bughunt 第5R)。
    bundle = pf.build_bundle(cands, probs, kelly_fraction=0.5,
                             bankroll=az_mod._ev_bankroll())
    bundle["source"] = "keibago"
    tables["trifecta"] = tri_table

    if save_snapshot:
        apt_top = az_mod._aptitude_top_horses(aptitudes, n=aptitude_top) if aptitudes else None
        plan_rows = ev_mod.apply_caps(tri_table)
        # keiba.go.jp の馬連/ワイド/馬単/3連複は組合せ明示で信頼できる (consistency NG 時は
        # 既に [] に落としてある) ので、束だけでなく EV table も snapshot に載せる。
        snap_bet_tables = {k: v for k, v in tables.items()
                           if k in ("win", "place", "quinella", "wide", "exacta", "trio") and v}
        try:
            az_mod._save_prediction_snapshot(
                race_id, rd, tri_table, plan_rows, aptitudes, snap_bet_tables, apt_top,
                market_signals, feats=feats, lgbm_info=ev_mod.lgbm_status(),
                hit_points=3, probs=probs, probs_t=probs_t,
                llm_win_index=llm_index, llm_blend=llm_blend, llm_scored_at=llm_scored_at,
                llm_support=llm_support, llm_scale=llm_scale, llm_alerts=llm_alerts,
                # この保存は bet 段のみ到達 (score 段は上で early return) なので 3連単買い目選定を
                # Claude に任せる。指数キャッシュが無ければ内部で機械フォーメーションへフォールバック。
                # --no-llm (with_llm=False) ではキルスイッチとして選定も止める (2026-06-11 第5R)。
                claude_trifecta_select=with_llm,
            )
            _tag_snapshot_source(race_id, "keibago")
        except Exception as ex:  # noqa: BLE001
            print(f"[analyze_keibago] snapshot 保存失敗: {ex}")
        # picks/cuts 選定は廃止 (指数ステップ一本化)。束は probs から build_bundle 済。

    return {"rd": rd, "probs": probs, "loc": loc, "used_cache": used_cache,
            "tables": tables, "bundle": bundle, "consistency": cons}


def _refund_segment_for_race(refund_html: str, race_no: int) -> str | None:
    """RefundMoneyList (当日全レース集約) から当該レースのセクションだけを切り出す。

    各レース block の直前に `RaceMarkTable?...&k_raceNo=N&...` リンクがあるので、
    その位置で分割する (2026-06-11 bughunt 第5R: flat 検索だと別レースが同一組番で
    決着した場合に先勝ちで誤った配当を保存し得る)。リンクが見つからなければ None
    (呼び出し側が従来の combo 一致 flat 検索に fallback)。
    """
    marks = [(m.start(), int(m.group(1)))
             for m in re.finditer(r"RaceMarkTable\?[^\"'>]*k_raceNo=(\d+)", refund_html)]
    if not marks:
        return None
    for i, (pos, rn) in enumerate(marks):
        if rn == race_no:
            end = marks[i + 1][0] if i + 1 < len(marks) else len(refund_html)
            return refund_html[pos:end]
    return None


def parse_keibago_result(racemark_html: str, refund_html: str = "",
                         race_no: int | None = None) -> dict:
    """結果ページ → {finish_order, payout(3連単)}。

    finish_order は RaceMarkTable (レース別の着順表) から (着順→馬番)。
    3連単配当は RefundMoneyList (当日全レース集約) から引く: race_no があれば当該
    レースのセクションに限定し (同一組番の別レース衝突を排除)、無ければ従来どおり
    **finish_order の組番に一致する三連単行** を flat 検索する。
    """
    finish = _parse_finish_order(racemark_html)
    payout = 0
    if finish and len(finish) >= 3 and refund_html:
        search_html = refund_html
        if race_no is not None:
            seg = _refund_segment_for_race(refund_html, race_no)
            if seg is not None:
                search_html = seg
        combo = "-".join(str(x) for x in finish[:3])
        cells = [c for c in (
            re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x)).strip())
            for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", search_html, re.DOTALL)
        ) if c]
        for i, c in enumerate(cells):
            if c in ("三連単", "３連単", "3連単") and i + 2 < len(cells) and cells[i + 1] == combo:
                payout = int(re.sub(r"\D", "", cells[i + 2]) or 0)
                break
    return {"finish_order": finish, "payout": payout}


def _parse_finish_order(html: str) -> list[int]:
    """着順表 (ヘッダに 着順/馬番) → 着順 1,2,3 の馬番リスト。

    着順/馬番ヘッダを持つ表が複数ある (印/予想表が先行する) ことがあるので、**最初の
    1つでなく 1-2-3 が揃う表を優先**して選ぶ (先行する decoy 表の誤採用を防ぐ)。
    揃う表が無ければ最も着順エントリの多い表に fallback。
    """
    best: dict[int, int] = {}
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.DOTALL)
        if not rows:
            continue
        head = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x)).strip())
                for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[0], re.DOTALL)]
        head = [h for h in head if h]
        if "着順" not in head or "馬番" not in head:
            continue
        ci, ui = head.index("着順"), head.index("馬番")
        order: dict[int, int] = {}
        for r in rows[1:]:
            c = [x for x in (re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x)).strip())
                             for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.DOTALL)) if x]
            if len(c) > max(ci, ui) and c[ci] in ("1", "2", "3") and c[ui].isdigit():
                order.setdefault(int(c[ci]), int(c[ui]))  # 同着は先勝ち
        if {1, 2, 3} <= set(order):
            return [order[1], order[2], order[3]]   # 1-2-3 完全な表を即採用
        if len(order) > len(best):
            best = order
    return [best[p] for p in sorted(best)] if best else []


def fetch_keibago_result(netkeiba_rid: str) -> dict | None:
    """netkeiba NAR race_id → keiba.go.jp の確定結果 {finish_order, payout, final_odds}。

    `final_odds` は各 bet type の **最終確定オッズ** を `bet_type:key` 形式 (例
    `"trifecta:6-7-11"`) で flat dict 化したもの。calibration で実払戻ベースの ROI を
    計算するために使う。取得失敗時は最終オッズだけ {} で続行。
    netkeiba block 中でも NAR の結果を取得できる (result fetch の fallback)。当日確定
    レースのみ (find_keibago_race が TodayRaceInfo ベース)。未確定/未解決は None。
    """
    loc = find_keibago_race(netkeiba_rid)
    if loc is None:
        return None
    try:
        rm = _get(_odds_url(loc, "RaceMarkTable"))
        rf = _get(_odds_url(loc, "RefundMoneyList"))
    except Exception:  # noqa: BLE001
        return None
    res = parse_keibago_result(rm, rf, race_no=loc.race_no)
    # 3連単として有効なのは 1-2-3 が揃う (len>=3) 確定結果のみ。同着等で着順が
    # 揃わない / 未確定は None を返し、不完全な結果を save しない (loop は pending 維持)。
    if len(res["finish_order"]) < 3:
        return None
    # 最終オッズ (全6券種) を追加取得。失敗しても結果自体は返す (finish_order があれば save 可)。
    try:
        bets = fetch_keibago_bets(loc)
        res["final_odds"] = _flatten_final_odds(bets)
    except Exception:  # noqa: BLE001
        res["final_odds"] = {}
    return res


def _flatten_final_odds(bets: dict) -> dict[str, float]:
    """{other_bets: {bet_type: [BetOdds]}, trifecta: [TrifectaOdds]} → flat {leg_id: odds}。

    leg_id 形式は portfolio / llm.leg_id と合わせる: `"<bet_type>:<key-joined-by-->"`。
    例: `"trifecta:1-2-3"` / `"wide:3-7"` / `"win:5"`。
    """
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


def _main() -> None:
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python -m src.scrape_keibago <netkeiba_nar_race_id> "
              "[--snapshot] [--start-at=UNIX] [--market-blend=X] [--aptitude-top=N] [--no-llm]")
        raise SystemExit(2)
    rid = args[0]
    save = "--snapshot" in sys.argv
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
    if save:
        try:
            res = analyze_keibago(rid, save_snapshot=True, start_at=start_at,
                                  with_llm="--no-llm" not in sys.argv,
                                  market_blend=market_blend, aptitude_top=aptitude_top,
                                  phase=phase, llm_blend=llm_blend)
        except KeibagoError as ex:
            print(f"keiba.go.jp 解析不能 ({rid}): {ex}")
            raise SystemExit(1)
        loc = res["loc"]
        if res.get("phase") == "score":
            print(f"=== keiba.go.jp {loc.venue} {loc.race_no}R score ステージ完了 (指数キャッシュ) ===")
            return
        c = res["consistency"]
        has_past = any(h.past_runs for h in res["rd"].race.horses)
        src = ("cache 出馬表+馬柱" if res["used_cache"]
               else "公式出馬表+馬柱" if has_past else "馬リストのみ(市場主導)")
        print(f"=== keiba.go.jp {loc.venue} {loc.race_no}R snapshot 保存 "
              f"({src}) ok={c['ok']} bundle脚={len(res['bundle'].get('legs', []))} ===")
        return
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
