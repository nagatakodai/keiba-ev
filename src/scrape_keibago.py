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


def parse_deba_table(html: str) -> list[tuple[int, str, str]]:
    """出馬表 (DebaTable) → [(馬番, 馬名, k_lineageLoginCode)]。

    各馬は `<td rowspan="5" class="horseNum">N</td>` に続く
    `<a class="horseName" href="../DataRoom/HorseMarkInfo?k_lineageLoginCode=CODE">名</a>`。
    CODE で競走成績 (HorseMarkInfo) を引いて馬柱 (past_runs) を構築する。

    **馬ブロック単位で探す**: 取消等でリンクが無い馬があっても、その馬の馬番を
    **次の馬の CODE と誤ってペアにしない** (= 馬柱を別馬に付ける最悪の取り違えを防ぐ)。
    リンクが無い馬は code 空で残す (馬は落とさない)。
    """
    out: list[tuple[int, str, str]] = []
    marks = list(re.finditer(r'class="horseNum"[^>]*>\s*(\d+)\s*</td>', html))
    for i, m in enumerate(marks):
        num = int(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        block = html[m.end():end]   # この馬のブロック内だけを探索
        lm = re.search(r'k_lineageLoginCode=(\d+)"[^>]*>\s*([^<]+?)\s*</a>', block)
        out.append((num, lm.group(2).strip() if lm else "", lm.group(1) if lm else ""))
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
        surface = {"芝": "芝", "ダ": "ダ", "障": "障"}.get(dm.group(1) or "", "ダ")
        # 馬場 (距離の後 / タイムの前) と頭数 (馬場の直後)
        going, field_size = "", 0
        for i in range(2, ti):
            if c[i] in _GOING_SET:
                going = c[i]
                if i + 1 < len(c) and c[i + 1].isdigit():
                    field_size = int(c[i + 1])
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
    deba: list[tuple[int, str, str]],
    win_odds: dict[int, float],
    *,
    fetch_past: bool = True,
) -> RaceData:
    """cache 出馬表が無い場合の RaceData を keiba.go.jp の出馬表(DebaTable)から構築。

    deba = [(馬番, 馬名, k_lineageLoginCode)]。fetch_past=True で各馬の HorseMarkInfo
    (競走成績) から **馬柱 (past_runs) を取得**して付与 → build_features が効き、
    estimate_probs が市場主導でなくモデルの edge を反映できる (= netkeiba/oddspark 非依存)。
    leakage 防止: HorseMarkInfo は対象 race 自身も含むので **対象 race 日付以降を除外** +
    直近5走に制限 (netkeiba 馬柱の窓に合わせる)。live (発走前) では対象 race は未走で no-op。
    """
    venue, schedule_index, race_number, cup_id = _split_race_id(netkeiba_rid)
    horse_rows = [Horse(number=n, name=nm, win_odds=win_odds.get(n, 0.0))
                  for n, nm, _ln in deba]
    if fetch_past:
        race_date = (f"{netkeiba_rid[:4]}.{netkeiba_rid[6:8]}.{netkeiba_rid[8:10]}"
                     if is_nar_race_id(netkeiba_rid) else "")
        ln_by_num = {n: ln for n, _nm, ln in deba}
        for h in horse_rows:
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
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def analyze_keibago(netkeiba_rid: str, *, save_snapshot: bool = False, start_at: int = 0,
                    with_llm: bool = True) -> dict:
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
            # DebaTable 取れず → 単複の馬リストのみ (past_runs なし=市場ブレンド主導)
            hl = parse_horse_list(_get(_odds_url(loc, _EP["tanfuku"])))
            rd = build_keibago_racedata(
                netkeiba_rid, [(n, nm, "") for n, nm, _od in hl], win_odds, fetch_past=False)

    if start_at and not rd.race.start_at:
        rd.race.start_at = start_at
        rd.race.close_at = start_at

    rd.other_bets = {bt: v for bt, v in other.items() if v}
    rd.trifecta = trifecta

    win_odds = {b.key[0]: b.odds for b in other["win"] if b.odds > 0}
    s = sum(1.0 / o for o in win_odds.values()) or 1.0
    mwp = {n: (1.0 / o) / s for n, o in win_odds.items()}
    probs = ev_mod.estimate_probs(rd, market_blend=0.78, market_win_override=mwp)
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
    bundle = pf.build_bundle(cands, probs)
    bundle["source"] = "keibago"
    tables["trifecta"] = tri_table

    if save_snapshot:
        from . import analyze as az_mod
        from .aptitude import compute_aptitudes
        from .features import build_features
        race_id = f"{rd.race.cup_id}-{rd.race.schedule_index}-{rd.race.race_number}"
        has_past = any(h.past_runs for h in rd.race.horses)
        feats = build_features(rd) if (used_cache or has_past) else None
        aptitudes = compute_aptitudes(rd, feats=feats) if feats else None
        apt_top = az_mod._aptitude_top_horses(aptitudes, n=6) if aptitudes else None
        plan_rows = ev_mod.apply_caps(tri_table)
        # keiba.go.jp の馬連/ワイド/馬単/3連複は組合せ明示で信頼できる (consistency NG 時は
        # 既に [] に落としてある) ので、束だけでなく EV table も snapshot に載せる
        # (単複のみだった oddspark の制限は流用しない。netkeiba 経路と同じく全券種表示)。
        snap_bet_tables = {k: v for k, v in tables.items()
                           if k in ("win", "place", "quinella", "wide", "exacta", "trio") and v}
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
            _tag_snapshot_source(race_id, "keibago")
        except Exception as ex:  # noqa: BLE001
            print(f"[analyze_keibago] snapshot 保存失敗: {ex}")
        if with_llm:
            # claude 評価は **総合オススメ束に対する web 検索補強のみ**
            # (3連単単独の evidence は廃止、1 race = 1 claude call に集約)。
            try:
                az_mod._validate_and_update_bundle(
                    race_id, rd, probs, tri_table, snap_bet_tables,
                    aptitudes=aptitudes, market_signals=market_signals,
                    horse_best_times=best_times, model="opus",
                )
            except Exception as ex:  # noqa: BLE001
                print(f"[analyze_keibago] bundle 検証失敗: {ex}")

    return {"rd": rd, "probs": probs, "loc": loc, "used_cache": used_cache,
            "tables": tables, "bundle": bundle, "consistency": cons}


def parse_keibago_result(racemark_html: str, refund_html: str = "") -> dict:
    """結果ページ → {finish_order, payout(3連単)}。

    finish_order は RaceMarkTable (レース別の着順表) から (着順→馬番)。
    3連単配当は RefundMoneyList (当日全レース集約) から **finish_order の組番に一致する
    三連単行** を引く (combo マッチで別レースの配当を拾わない)。
    """
    finish = _parse_finish_order(racemark_html)
    payout = 0
    if finish and len(finish) >= 3 and refund_html:
        combo = "-".join(str(x) for x in finish[:3])
        cells = [c for c in (
            re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x)).strip())
            for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", refund_html, re.DOTALL)
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
    """netkeiba NAR race_id → keiba.go.jp の確定結果 {finish_order, payout}。

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
    res = parse_keibago_result(rm, rf)
    # 3連単として有効なのは 1-2-3 が揃う (len>=3) 確定結果のみ。同着等で着順が
    # 揃わない / 未確定は None を返し、不完全な結果を save しない (loop は pending 維持)。
    return res if len(res["finish_order"]) >= 3 else None


def _main() -> None:
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python -m src.scrape_keibago <netkeiba_nar_race_id> "
              "[--snapshot] [--start-at=UNIX] [--no-llm]")
        raise SystemExit(2)
    rid = args[0]
    save = "--snapshot" in sys.argv
    start_at = 0
    for a in sys.argv:
        if a.startswith("--start-at="):
            start_at = int(a.split("=", 1)[1] or 0)
    if save:
        try:
            res = analyze_keibago(rid, save_snapshot=True, start_at=start_at,
                                  with_llm="--no-llm" not in sys.argv)
        except KeibagoError as ex:
            print(f"keiba.go.jp 解析不能 ({rid}): {ex}")
            raise SystemExit(1)
        loc, c = res["loc"], res["consistency"]
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
