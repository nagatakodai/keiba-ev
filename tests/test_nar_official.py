"""地方競馬全国協会の公式データ (src/nar_official.py) — 解析・取得方針・ledger・当レース条件の補完。

通信はしない: tests/conftest.py が KEIBA_NAR_OFFICIAL=0 (fetch_* はキャッシュだけ・sync_due は False) +
_download を「呼ばれたら記録して失敗」に差し替え + キャッシュを tmp に隔離。取得経路のテストは
`_net_on` で通信を許可し、_download を合成 ZIP を返す関数に差し替える。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pytest

from src import nar_official as no
from src import time_model as tm

JST = no.JST

_RACE_HDR = ("競馬場,競走年月日,レース番号,発走時刻,競走種類名称,レース名," + ",".join(f"副賞名{i}" for i in range(1, 16))
             + ",芝ダート区分,回り,距離,天候,馬場,頭数,条件,1着賞金(円),2着賞金(円),3着賞金(円),4着賞金(円),5着賞金(円),"
             "上がり4F,上がり3F")
_HORSE_HDR = ("競馬場,競走年月日,レース番号,枠番,帽色,馬番,馬名,性,齢,毛色,生年月日,父馬名,母馬名,母父馬名,騎手名,"
              "騎手所属,負担重量,騎手成績,調教師,調教師所属,馬主氏名,生産牧場名,馬体重,馬体重増減,全成績,ダート左成績,"
              "ダート右成績,当競馬場成績,うち当距離成績,最高タイム,最高タイム良馬場,着順,タイム,着差,上がり3F,人気")
_PAY_HDR = "競馬場,競走年月日,レース番号,レース名,単勝組番,単勝払戻金（円）,単勝人気"
_BOM = "﻿"


def _race_row(venue, date, rn, name, surface, turn, dist, weather, going, n=6, hhmm="1500"):
    return ",".join([venue, str(date), str(rn), hhmm, "普通", name] + [""] * 15
                    + [surface, turn, str(dist), weather, going, str(n), "サラブレッド系　一般", "1", "1", "1", "1", "1",
                       "", ""])


def _horse_row(venue, date, rn, num, name, fin, time_s, margin="", pop=1):
    return ",".join([venue, str(date), str(rn), "1", "白", str(num), name, "牡", "4", "鹿毛", "20220401", "父", "母",
                     "母父", "騎手", "大井", "56", "0-0-0-0", "調教師", "大井", "馬主", "牧場", "480", "+2", "0-0-0-0",
                     "", "", "", "", "", "", fin, time_s, margin, "38.0", str(pop)])


def _zip(name_prefix: str, racelist: list[str], horselist: list[str], payback: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # 実ファイルと同じ UTF-8 (BOM 付き)
        z.writestr(f"{name_prefix}_racelist.csv", (_BOM + "\n".join([_RACE_HDR, *racelist]) + "\n").encode("utf-8"))
        z.writestr(f"{name_prefix}_horselist.csv",
                   (_BOM + "\n".join([_HORSE_HDR, *horselist]) + "\n").encode("utf-8"))
        z.writestr(f"{name_prefix}_payback.csv",
                   (_BOM + "\n".join([_PAY_HDR, *(payback or [])]) + "\n").encode("utf-8"))
    return buf.getvalue()


def _sample_month() -> bytes:
    races = [
        _race_row("大井", 20260602, 1, "Ｃ２十組", "ダート", "右", 1200, "晴", "稍重"),
        _race_row("盛岡", 20260602, 3, "芝の特別Ｂ２", "芝", "左", 1600, "曇", "良"),
        _race_row("帯広ば", 20260602, 1, "ばんえいＣ２－１", "", "直", 200, "晴", "0.8"),
    ]
    horses = [
        _horse_row("大井", 20260602, 1, 1, "アルファ", "1", "1131", "", 2),
        _horse_row("大井", 20260602, 1, 2, "ブラボー", "2", "1134", "1.1/2", 1),
        _horse_row("大井", 20260602, 1, 3, "チャーリー", "3", "1170", "大差", 3),     # 3.9 秒差 (大差負け)
        _horse_row("大井", 20260602, 1, 4, "デルタ", "", "", "出走取消", 0),
        _horse_row("大井", 20260602, 1, 5, "エコー", "", "1150", "失格", 5),          # 着順なし = 入れない
        _horse_row("盛岡", 20260602, 3, 1, "フォックス", "1", "1360", "", 1),
        _horse_row("盛岡", 20260602, 3, 2, "ゴルフ", "2", "1361", "ハナ", 2),
        _horse_row("帯広ば", 20260602, 1, 1, "ホテル", "1", "2287", "", 1),           # ばんえいは入れない
    ]
    pay = ["大井,20260602,1,Ｃ２十組,1,350,2"]
    return _zip("202606", races, horses, pay)


@pytest.fixture
def _net_on(monkeypatch):
    """取得経路を検証するテスト用: 通信スイッチを入れる (_download はテスト側で合成 ZIP に差し替える)。"""
    monkeypatch.setenv(no.ENV_SWITCH, "1")


def _counting(data_for):
    """呼ばれた URL を記録して合成 ZIP を返す _download の代役。data_for(url) → bytes or 例外。"""
    calls: list[str] = []

    def fake(url, **_kw):
        calls.append(url)
        out = data_for(url)
        if isinstance(out, Exception):
            raise out
        return out

    return fake, calls


# ---------------------------------------------------------------- 解析 ----

@pytest.mark.parametrize("s,sec", [("1259", 85.9), ("597", 59.7), ("2043", 124.3), ("1:25.9", 85.9),
                                   ("85.9", 85.9), ("１２５９", 85.9), ("", None), ("取消", None),
                                   ("1699", None), ("0", None)])
def test_parse_time(s, sec):
    got = no.parse_time(s)
    assert (got is None and sec is None) or abs(got - sec) < 1e-9


def test_read_zip_and_race_infos():
    t = no.read_zip(_sample_month())
    assert {k: len(v) for k, v in t.items()} == {"racelist": 3, "horselist": 8, "payback": 1}
    assert set(no.read_zip(_sample_month(), kinds=("racelist",))) == {"racelist"}
    infos = no.race_infos(t)
    oi = infos[("大井", 20260602, 1)]
    assert (oi["surface"], oi["direction"], oi["distance"], oi["weather"], oi["going"], oi["race_name"]) == \
        ("ダート", "右", 1200, "晴", "稍重", "Ｃ２十組")
    assert oi["start_hhmm"] == "15:00"
    banei = infos[("帯広ば", 20260602, 1)]
    assert banei["going"] == "" and banei["moisture"] == 0.8 and banei["surface"] == ""


def test_ledger_rows_winner_time_and_exclusions():
    rows = no.ledger_rows(no.read_zip(_sample_month()))
    by = {r["h"]: r for r in rows}
    # 取消 (着順・タイムなし)・失格 (着順なし)・ばんえいは入らない
    assert set(by) == {"アルファ", "ブラボー", "チャーリー", "フォックス", "ゴルフ"}
    a, b, c = by["アルファ"], by["ブラボー"], by["チャーリー"]
    assert (a["v"], a["s"], a["dist"], a["g"], a["t"], a["wt"], a["r"]) == ("大井", "ダート", 1200, "稍", 73.1, 73.1, 1)
    assert (b["t"], b["wt"]) == (73.4, 73.1)
    assert c["t"] - c["wt"] > tm.MAX_BEHIND          # 大差負けは fit で除外される (勝ち時計があるので判定できる)
    f = by["フォックス"]
    assert (f["v"], f["s"], f["g"], f["t"], f["wt"], f["r"]) == ("盛岡", "芝", "良", 96.0, 96.0, 3)


def test_ledger_rows_dead_heat_and_gjs_name():
    """1 着同着は両馬とも勝ち時計 = 自分の時計。ＧＪＳ (ゴールデンジョッキーズシリーズ) の名前は平地のまま。"""
    races = [_race_row("水沢", 20251209, 9, "オッズパークＧＪＳキャンペーン賞Ｂ２", "ダート", "右", 1400, "晴", "良")]
    horses = [_horse_row("水沢", 20251209, 9, 1, "イチ", "1", "1284", "", 1),
              _horse_row("水沢", 20251209, 9, 2, "ニイ", "1", "1284", "同着", 2),
              _horse_row("水沢", 20251209, 9, 3, "サン", "3", "1290", "1/2", 3),
              _horse_row("水沢", 20251209, 9, 4, "ヨン", "", "", "競走中止", 4)]
    rows = no.ledger_rows(no.read_zip(_zip("202512", races, horses)))
    assert {r["h"]: (r["s"], r["t"], r["wt"]) for r in rows} == {
        "イチ": ("ダート", 88.4, 88.4), "ニイ": ("ダート", 88.4, 88.4), "サン": ("ダート", 89.0, 88.4)}


def test_ledger_rows_match_ledger_row_schema():
    rows = no.ledger_rows(no.read_zip(_sample_month()))
    assert all(set(r) == {"h", "d", "v", "s", "dist", "g", "t", "wt", "r"} for r in rows)
    assert all(tm.valid_flat_time(r["s"], r["dist"], r["t"]) for r in rows)


# ---------------------------------------------------------------- 取得方針 ----

def _put_month(ym: str, downloaded_at: dt.datetime, data: bytes | None = None, *, kind="race", **meta) -> None:
    no.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    no.monthly_zip_path(ym, kind).write_bytes(data or _sample_month())
    no._write_meta(ym, {"downloaded_at": downloaded_at.isoformat(), **meta}, kind)


def test_month_needs_download_policy():
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    assert no.month_needs_download("202610", now)                       # 未取得
    assert not no.month_needs_download("202611", now)                   # 未来の月
    # 確定済み (月末の翌日の夜間更新より後に取得し中身も揃っていた) は二度と取らない
    _put_month("202606", dt.datetime(2026, 7, 2, 3, 30, tzinfo=JST), final=True)
    assert not no.month_needs_download("202606", now)
    # 月末直後に取った分 (10/1 夕方) は確定前 → 次の夜間更新の後に 1 回だけ取り直す
    _put_month("202609", dt.datetime(2026, 10, 1, 17, 0, tzinfo=JST))
    assert no.month_needs_download("202609", now)
    # 当月: 今日の夜間更新 (03:00) より後に取得済みなら取らない / 前なら取る (= 1 日 1 回まで)
    _put_month("202610", dt.datetime(2026, 10, 13, 9, 0, tzinfo=JST))
    assert not no.month_needs_download("202610", now)
    assert no.month_needs_download("202610", dt.datetime(2026, 10, 14, 3, 1, tzinfo=JST))
    assert not no.month_needs_download("202610", dt.datetime(2026, 10, 14, 2, 59, tzinfo=JST))


def test_month_needs_download_incomplete_final_and_clock_edges():
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    # 月末後に取ったのに中身が揃っていなかった月: 期限 (+7 日) までは 1 日 1 回取り直し、過ぎたら諦める
    _put_month("202609", dt.datetime(2026, 10, 3, 4, 0, tzinfo=JST), final=False)
    assert no.month_needs_download("202609", dt.datetime(2026, 10, 4, 4, 0, tzinfo=JST))
    assert not no.month_needs_download("202609", dt.datetime(2026, 10, 9, 4, 0, tzinfo=JST))
    # 取得時刻が未来 (時計が戻った) の記録は信用しない
    _put_month("202610", dt.datetime(2026, 10, 15, 9, 0, tzinfo=JST))
    assert no.month_needs_download("202610", now)
    # 月初 1 日の夜間更新前は未取得の当月を取らない (同じ日に 2 回取ることになる)
    assert not no.month_needs_download("202611", dt.datetime(2026, 11, 1, 0, 10, tzinfo=JST))
    assert no.month_needs_download("202611", dt.datetime(2026, 11, 1, 3, 10, tzinfo=JST))


def test_disabled_switch_never_touches_network():
    """KEIBA_NAR_OFFICIAL=0 (テスト既定): 取得はキャッシュだけ・同期は disabled・sync_due は常に False。"""
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    assert not no.enabled()
    assert no.fetch_monthly("202610", now=now) == (None, False)
    _put_month("202605", dt.datetime(2026, 6, 5, 4, 0, tzinfo=JST), final=True)
    res = no.sync(["202605", "202610"], now=now)
    assert res["disabled"] and res["missing"] == ["202610"] and res["cached"] == ["202605"]
    assert res["ledger_written"] == ["202605"]           # 手元の ZIP からの ledger 書き出しは止めない
    assert not no.sync_due(now)
    assert no.fetch_daily(now=now) is None


def test_fetch_monthly_downloads_once_then_uses_cache(_net_on, monkeypatch):
    fake, calls = _counting(lambda url: _sample_month())
    monkeypatch.setattr(no, "_download", fake)
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    p, fresh = no.fetch_monthly("202606", now=now)
    assert fresh and p == no.monthly_zip_path("202606") and p.exists()
    assert calls == [no.RACE_URL + "?type=monthly&k_year=2026&k_month=06"]
    meta = no._read_meta("202606")
    assert meta["final"] is True and meta["fail_count"] == 0     # 過去レースに完走記録がある = 揃っている
    p2, fresh2 = no.fetch_monthly("202606", now=now + dt.timedelta(days=5))
    assert p2 == p and not fresh2 and len(calls) == 1             # 確定月はもう取らない


def test_fetch_monthly_incomplete_content_is_not_final(_net_on, monkeypatch):
    """月末後に取っても完走記録のないレースが多い (結果未反映) ZIP は確定にしない。"""
    races = [_race_row("大井", 20260602, rn, f"C{rn}", "ダート", "右", 1200, "晴", "良") for rn in range(1, 11)]
    horses = [_horse_row("大井", 20260602, 1, 1, "アルファ", "1", "1131")]      # 10 レース中 1 レースだけ結果あり
    fake, _calls = _counting(lambda url: _zip("202606", races, horses))
    monkeypatch.setattr(no, "_download", fake)
    no.fetch_monthly("202606", now=dt.datetime(2026, 7, 5, 4, 0, tzinfo=JST))
    assert no._read_meta("202606")["final"] is False
    assert no.month_needs_download("202606", dt.datetime(2026, 7, 6, 4, 0, tzinfo=JST))


@pytest.mark.network_attempt_ok
def test_fetch_monthly_failure_keeps_cache_and_backs_off(_net_on):
    """取得失敗 (conftest の stub) でもキャッシュを返し、試行と失敗回数を記録 → 3h → 6h と間隔を空ける。"""
    now = dt.datetime(2026, 10, 14, 9, 0, tzinfo=JST)
    _put_month("202610", dt.datetime(2026, 10, 13, 9, 0, tzinfo=JST))
    _put_month("202609", dt.datetime(2026, 10, 5, 4, 0, tzinfo=JST), final=True)
    p, fresh = no.fetch_monthly("202610", now=now)
    assert p == no.monthly_zip_path("202610") and not fresh
    meta = no._read_meta("202610")
    assert meta["downloaded_at"].startswith("2026-10-13") and "last_error" in meta and meta["fail_count"] == 1
    assert meta["last_attempt_at"].startswith("2026-10-14T09:00")
    # 失敗直後は取りに行かない (sync_due も fetch_monthly も) — 毎 tick の再試行を避ける
    assert not no.sync_due(now + dt.timedelta(hours=1), since="202609")
    assert no.fetch_monthly("202610", now=now + dt.timedelta(hours=1)) == (p, False)
    assert no._read_meta("202610")["fail_count"] == 1
    assert no.sync_due(now + dt.timedelta(hours=3, minutes=1), since="202609")
    # 2 回目の失敗の後は 6 時間
    no.fetch_monthly("202610", now=now + dt.timedelta(hours=3, minutes=1))
    assert no._read_meta("202610")["fail_count"] == 2
    assert not no.sync_due(now + dt.timedelta(hours=8), since="202609")
    assert no.sync_due(now + dt.timedelta(hours=9, minutes=2), since="202609")


def test_fetch_monthly_does_not_download_when_attempt_cannot_be_recorded(_net_on, monkeypatch):
    """試行を meta に記録できない (書き込み不可・容量不足) なら取りに行かない = 再試行の嵐を作らない。"""
    fake, calls = _counting(lambda url: _sample_month())
    monkeypatch.setattr(no, "_download", fake)

    def _ro(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(no, "_write_meta", _ro)
    assert no.fetch_monthly("202606", now=dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)) == (None, False)
    assert calls == []


def test_check_zip_rejects_html_and_crc_errors():
    with pytest.raises(ValueError):
        no._check_zip(b"<html>maintenance</html>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("202606_horselist.csv", "x" * 100)
    raw = bytearray(buf.getvalue())
    raw[raw.index(b"x" * 10) + 3] = ord("y")             # 中身 1 バイト破損 (CRC 不一致)
    with pytest.raises(zipfile.BadZipFile):
        no._check_zip(bytes(raw))
    no._check_zip(_sample_month())                       # 正常な ZIP は通る


def test_fetch_monthly_rejects_empty_tables(_net_on, monkeypatch):
    """列名変更などで表が空の ZIP は保存しない (失敗扱い・既存キャッシュを残す)。"""
    good = _sample_month()
    _put_month("202610", dt.datetime(2026, 10, 12, 9, 0, tzinfo=JST), good)
    fake, _calls = _counting(lambda url: _zip("202610", [], []))
    monkeypatch.setattr(no, "_download", fake)
    p, fresh = no.fetch_monthly("202610", now=dt.datetime(2026, 10, 13, 9, 0, tzinfo=JST))
    assert not fresh and p.read_bytes() == good
    assert no._read_meta("202610")["fail_count"] == 1


def test_write_ledger_refuses_to_shrink_existing(tmp_path):
    zp = tmp_path / "m.zip"
    zp.write_bytes(_sample_month())
    assert no.write_ledger("202606", zp) == 5
    zp.write_bytes(_zip("202606", [_race_row("大井", 20260602, 1, "C", "ダート", "右", 1200, "晴", "良")], []))
    with pytest.raises(ValueError):
        no.write_ledger("202606", zp)                    # 0 行で 5 行を上書きしない
    assert len(no.ledger_path("202606").read_text(encoding="utf-8").splitlines()) == 5
    assert no.write_ledger("202606", zp, force=True) == 0


def test_sync_writes_ledger_and_summary(_net_on, monkeypatch):
    fake, _calls = _counting(lambda url: _sample_month())
    monkeypatch.setattr(no, "_download", fake)
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    res = no.sync(["202606"], now=now)
    assert res["downloaded"] == ["202606"] and res["ledger_written"] == ["202606"] and res["rows"] == 5
    lines = no.ledger_path("202606").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and {json.loads(x)["h"] for x in lines} >= {"アルファ", "フォックス"}
    # 2 回目は取得も書き直しもしない
    res2 = no.sync(["202606"], now=now)
    assert res2["cached"] == ["202606"] and res2["ledger_written"] == []
    # load_ledger に乗る (live より後・netkeiba より前のファイル順)
    assert len(tm.load_ledger()) == 5


def test_sync_isolates_a_broken_month_and_caps_downloads(_net_on, monkeypatch):
    fake, calls = _counting(lambda url: _sample_month())
    monkeypatch.setattr(no, "_download", fake)
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    no.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    no.monthly_zip_path("202605").write_bytes(b"PK broken")       # 壊れたキャッシュ (確定扱いの meta)
    no._write_meta("202605", {"downloaded_at": "2026-06-05T04:00:00+09:00", "final": True})
    res = no.sync(["202605", "202606", "202607"], now=now, max_downloads=1)
    assert res["failed"] == ["202605"]                    # 1 か月の失敗で他を止めない
    assert res["downloaded"] == ["202606"] and res["deferred"] == ["202607"] and len(calls) == 1


def test_fetch_daily_throttles_and_checks_date(_net_on, monkeypatch):
    t0 = dt.datetime(2026, 6, 2, 12, 0, tzinfo=JST)
    payload = {"data": _zip("20260602", [_race_row("大井", 20260602, 1, "C", "ダート", "右", 1200, "晴", "良")], [])}
    fake, calls = _counting(lambda url: payload["data"])
    monkeypatch.setattr(no, "_download", fake)
    assert no.fetch_daily(now=t0) == no.daily_zip_path()
    assert no.fetch_daily(now=t0 + dt.timedelta(seconds=90)) == no.daily_zip_path()
    assert len(calls) == 1
    no.fetch_daily(now=t0 + dt.timedelta(seconds=121))
    assert len(calls) == 2 and calls[0].endswith("?type=daily")
    # 時計が戻った (取得時刻が未来) ときは鮮度を信用しない (取り直す)
    assert no.fetch_daily(now=t0 - dt.timedelta(minutes=30)) == no.daily_zip_path() and len(calls) == 3
    # 日付が変わった直後に昨日の中身が返ってきたら使わない (今日のキャッシュも無いので None)
    t1 = dt.datetime(2026, 6, 3, 0, 5, tzinfo=JST)
    assert no.fetch_daily(now=t1) is None
    assert no.daily_cache_for(20260603) is None and no.daily_cache_for(20260602) == no.daily_zip_path()


# ---------------------------------------------------------------- 当レース条件 ----

def test_official_race_header_from_monthly_cache():
    _put_month("202606", dt.datetime(2026, 7, 5, 4, 0, tzinfo=JST))
    h = no.official_race_header("202644060201")          # 2026-06-02 大井 1R
    assert h == {"distance": 1200, "surface": "ダート", "direction": "右", "weather": "晴", "going": "稍重",
                 "race_name": "C2十組"}
    assert no.official_race_header("202644060209") == {}                  # 無いレース
    assert no.official_race_header("202605210601") == {}                  # JRA は対象外
    b = no.official_race_header("202665060201")                            # 帯広ばんえい: 馬場は水分量
    assert b == {"weather": "晴", "moisture": 0.8, "race_name": "ばんえいC2-1"}   # 芝ダ区分が空 = 距離も載せない


def test_race_info_today_monthly_base_with_daily_overlay(_net_on, monkeypatch):
    """今日のレース: 月次キャッシュ (出馬表のみ・天候馬場は空) を基に、天候・馬場だけ当日ファイルで上書き。"""
    today = no._now().date()
    ymd = today.year * 10000 + today.month * 100 + today.day
    ym = f"{ymd // 100:06d}"
    _put_month(ym, no._now(), _zip(ym, [_race_row("川崎", ymd, 7, "Ｂ３－５", "ダート", "左", 1500, "", "")], []))
    daily = _zip(f"{ymd}", [_race_row("川崎", ymd, 7, "Ｂ３－５", "ダート", "左", 1500, "曇", "重")], [])
    fake, calls = _counting(lambda url: daily)
    monkeypatch.setattr(no, "_download", fake)
    rid = f"{today.year:04d}45{today.month:02d}{today.day:02d}07"
    # bet 段 (allow_download=False): 今日の当日ファイルが手元に無いので天候・馬場なし・通信もしない
    assert no.official_race_header(rid, allow_download=False) == {
        "distance": 1500, "surface": "ダート", "direction": "左", "race_name": "B3-5"}
    assert calls == []
    full = {"distance": 1500, "surface": "ダート", "direction": "左", "weather": "曇", "going": "重",
            "race_name": "B3-5"}
    assert no.official_race_header(rid) == full and len(calls) == 1          # score 段: 当日ファイルを取る
    assert no.official_race_header(rid, allow_download=False) == full        # 以後は手元の当日ファイル


def test_official_race_header_today_daily_only(_net_on, monkeypatch):
    today = no._now().date()
    ymd = today.year * 10000 + today.month * 100 + today.day
    data = _zip(f"{ymd}", [_race_row("川崎", ymd, 7, "Ｂ３－５", "ダート", "左", 1500, "曇", "重")], [])
    monkeypatch.setattr(no, "_download", lambda url, **_kw: data)
    rid = f"{today.year:04d}45{today.month:02d}{today.day:02d}07"
    assert no.official_race_header(rid) == {"distance": 1500, "surface": "ダート", "direction": "左",
                                            "weather": "曇", "going": "重", "race_name": "B3-5"}


def test_fill_race_header_only_fills_missing():
    _put_month("202606", dt.datetime(2026, 7, 5, 4, 0, tzinfo=JST))
    full = {"distance": 1400, "surface": "ダート", "direction": "左", "going": "良", "race_name": "X"}
    assert no.fill_race_header(full, "202644060201") == full               # 揃っていれば引かない
    got = no.fill_race_header({"weather": "雨"}, "202644060201")            # ヘッダから距離が取れなかった
    assert (got["distance"], got["surface"], got["direction"], got["race_name"]) == (1200, "ダート", "右", "C2十組")
    assert got["weather"] == "雨" and got["going"] == "稍重"                # ヘッダ側の値は上書きしない
    named = no.fill_race_header({"distance": 1300, "surface": "ダート", "direction": "右"}, "202644060201")
    assert named["distance"] == 1300 and named["race_name"] == "C2十組"      # 距離はヘッダ優先・名前だけ補う
    assert no.fill_race_header({}, "202644060299") == {}                   # 公式にも無ければ空のまま


def test_status_reports_failures_without_network():
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    _put_month("202609", dt.datetime(2026, 10, 5, 4, 0, tzinfo=JST), final=True)
    no._write_meta("202610", {"last_attempt_at": "2026-10-13T09:00:00+09:00", "last_error": "boom", "fail_count": 2})
    st = no.status(now)
    assert st["enabled"] is False and st["months_cached"] == 1 and st["months_in_window"] >= 18
    assert st["failing"] == {"202610": {"last_error": "boom", "last_attempt_at": "2026-10-13T09:00:00+09:00",
                                        "fail_count": 2}}
    assert st["due"] is False                                              # 通信停止中は常に False


def _odds_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("202606_01_odds.csv", (_BOM + "競馬場,競走年月日,レース番号,賭式,番号1,番号2,番号3,オッズ,オッズ（最大）,人気\n"
                                          "大井,20260602,1,単勝,1,,,3.5,,1\n").encode("utf-8"))
    return buf.getvalue()


def test_odds_files_use_separate_paths(_net_on, monkeypatch):
    fake, calls = _counting(lambda url: _odds_zip())
    monkeypatch.setattr(no, "_download", fake)
    p, fresh = no.fetch_monthly("202606", kind="odds", now=dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST))
    assert fresh and p.name == "odds-202606.zip" and calls[0].startswith(no.ODDS_URL)
    assert not no.monthly_zip_path("202606").exists()                       # レース情報の月とは別管理


# ---------------------------------------------------------------- 学習窓・ledger ----

def test_fit_window_is_anchored_per_circuit():
    """地方だけ新しいデータがあっても、中央の窓は中央の最新日から切る (中央の学習データを削らない)。"""
    rows = []
    for i, h in enumerate(("A", "B", "C", "D")):
        # 中央: 2025-01 と 2026-05 (中央の最新日から 540 日以内)。全体の最新日 (2026-10) から見ると窓外。
        rows.append({"h": f"J{h}", "d": 20250110, "v": "東京", "s": "ダート", "dist": 1400, "g": "良",
                     "t": 84.0 + i * 0.1, "wt": 0.0})
        rows.append({"h": f"J{h}", "d": 20260520, "v": "中山", "s": "ダート", "dist": 1200, "g": "良",
                     "t": 72.0 + i * 0.1, "wt": 0.0})
        rows.append({"h": f"N{h}", "d": 20260901, "v": "大井", "s": "ダート", "dist": 1200, "g": "良",
                     "t": 75.0 + i * 0.1, "wt": 0.0})
        rows.append({"h": f"N{h}", "d": 20261010, "v": "川崎", "s": "ダート", "dist": 1400, "g": "良",
                     "t": 89.0 + i * 0.1, "wt": 0.0})
    m = tm.fit(rows)
    assert "ダート|1400|東京" in m["theta"]        # 全体の最新日基準なら窓外で落ちていた
    assert m["data_until_by_circuit"] == {"jra": "2026-05-20", "nar": "2026-10-10"}
    assert m["n_runs"] == 16


def test_fit_window_ignores_sparse_recent_rows(monkeypatch):
    """解析時の live 行が 1 つ (データの揃っていない日) 増えても、中央の窓の起点と表示日は動かない。"""
    monkeypatch.setattr(tm, "DENSE_MIN_ROWS", 3)
    rows = []
    for i in range(4):
        rows.append({"h": f"J{i}", "d": 20250110, "v": "東京", "s": "ダート", "dist": 1400, "g": "良",
                     "t": 84.0 + i * 0.1, "wt": 0.0})
        rows.append({"h": f"J{i}", "d": 20260520, "v": "中山", "s": "ダート", "dist": 1200, "g": "良",
                     "t": 72.0 + i * 0.1, "wt": 0.0})
    rows.append({"h": "J0", "d": 20261011, "v": "東京", "s": "ダート", "dist": 1600, "g": "良", "t": 97.0, "wt": 0.0})
    rows.append({"h": "J1", "d": 20991231, "v": "東京", "s": "ダート", "dist": 1600, "g": "良", "t": 97.0, "wt": 0.0})
    m = tm.fit(rows)
    assert m["data_until_by_circuit"] == {"jra": "2026-05-20"}
    assert "ダート|1400|東京" in m["theta"] and m["n_runs"] == 10      # 未来日付の 1 行も起点を動かさない


def test_load_ledger_keeps_winner_time_and_race_number(tmp_path):
    a = tmp_path / "nar_official-202605.jsonl"
    b = tmp_path / "netkeiba_raw.jsonl"
    a.write_text(json.dumps({"h": "X", "d": 20260510, "v": "大井", "s": "ダート", "dist": 1200, "g": "良",
                             "t": 73.0, "wt": 72.5, "r": 4}, ensure_ascii=False) + "\n", encoding="utf-8")
    b.write_text(json.dumps({"h": "X", "d": 20260510, "v": "大井", "s": "ダート", "dist": 1200, "g": "良",
                             "t": 73.0, "wt": 0.0}, ensure_ascii=False) + "\n", encoding="utf-8")
    (row,) = tm.load_ledger([a, b])
    assert (row["wt"], row["r"]) == (72.5, 4)            # 後勝ちでも勝ち時計・レース番号は失わない


def test_par_win_counts_same_time_races_separately_by_race_number():
    rows = []
    for rn in (1, 2):                                    # 同日同条件・同じ勝ち時計の別レース (公式: r で区別)
        for i in range(3):
            rows.append({"h": f"H{rn}{i}", "d": 20260601, "v": "大井", "s": "ダート", "dist": 1200, "g": "良",
                         "t": 75.0 + i * 0.2, "wt": 75.0, "r": rn})
    for d in range(20260602, 20260605):                  # r の無い行 (netkeiba) — 別日なので別レース
        rows.append({"h": f"N{d}", "d": d, "v": "大井", "s": "ダート", "dist": 1200, "g": "良", "t": 75.4, "wt": 75.4})
    # r 付きレースと同じ (場,日,芝ダ,距離,馬場,勝ち時計) の r 無し行は二重に数えない
    rows.append({"h": "dup", "d": 20260601, "v": "大井", "s": "ダート", "dist": 1200, "g": "良", "t": 75.2, "wt": 75.0})
    # fit に回すため各馬 2 走にする (勝ち時計なしの別距離の走)
    m = tm.fit(rows + [dict(r, d=r["d"] - 100, dist=1400, t=r["t"] + 12.0, wt=0.0) for r in rows])
    # 2 (r 付き 75.0×2) + 3 (r 無し・別日 75.4×3) = 5 レース (dup を数えると 6)
    assert m["par_win"]["ダート|1200|大井|"] == [75.4, 5]


def test_fetch_monthly_rejects_renamed_columns(_net_on, monkeypatch):
    """見出しが変わった (仕様変更) ZIP は失敗扱い・既存キャッシュを残す (確定扱いにして凍結しない)。"""
    good = _sample_month()
    _put_month("202609", dt.datetime(2026, 10, 1, 17, 0, tzinfo=JST), good)
    t = no.read_zip(good)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for kind, rows in t.items():
            cols = list(rows[0].keys()) if rows else []
            body = ",".join("場名" if c == "競馬場" else c for c in cols) + "\n"
            body += "".join(",".join(r[c] for c in cols) + "\n" for r in rows)
            z.writestr(f"202609_{kind}.csv", (_BOM + body).encode("utf-8"))
    fake, _calls = _counting(lambda url: buf.getvalue())
    monkeypatch.setattr(no, "_download", fake)
    p, fresh = no.fetch_monthly("202609", now=dt.datetime(2026, 10, 2, 4, 0, tzinfo=JST))
    assert not fresh and p.read_bytes() == good
    meta = no._read_meta("202609")
    assert meta["fail_count"] == 1 and "列" in meta["last_error"]


def test_fetch_monthly_rejects_truncated_month(_net_on, monkeypatch):
    """手元にあった過去の開催日が新しいファイルで消えていたら保存しない (途中で切れた応答の確定化防止)。"""
    days = [20260901, 20260902]
    full = _zip("202609", [_race_row("大井", d, 1, "C", "ダート", "右", 1200, "晴", "良") for d in days],
                [_horse_row("大井", d, 1, 1, f"H{d}", "1", "1131") for d in days])
    _put_month("202609", dt.datetime(2026, 10, 1, 5, 0, tzinfo=JST), full)
    cut = _zip("202609", [_race_row("大井", 20260901, 1, "C", "ダート", "右", 1200, "晴", "良")],
               [_horse_row("大井", 20260901, 1, 1, "H1", "1", "1131")])
    fake, _calls = _counting(lambda url: cut)
    monkeypatch.setattr(no, "_download", fake)
    p, fresh = no.fetch_monthly("202609", now=dt.datetime(2026, 10, 2, 4, 0, tzinfo=JST))
    assert not fresh and p.read_bytes() == full and no._read_meta("202609")["fail_count"] == 1


def test_attempt_blocked_ignores_future_download_time():
    now = dt.datetime(2026, 10, 13, 12, 0, tzinfo=JST)
    meta = {"downloaded_at": (now + dt.timedelta(days=2)).isoformat(),
            "last_attempt_at": (now - dt.timedelta(hours=1)).isoformat(), "fail_count": 5}
    assert no._attempt_blocked(meta, now)                     # 未来の取得時刻で「成功済み」扱いにしない


def test_sync_stops_after_network_failure_and_reports_ledger_errors(_net_on, monkeypatch):
    """1 回の同期で取得に失敗したら残りの月は取りに行かない (deferred)。ledger の書き出し失敗は status に出る。"""
    fake, calls = _counting(lambda url: OSError("down"))
    monkeypatch.setattr(no, "_download", fake)
    now = dt.datetime(2026, 10, 13, 16, 0, tzinfo=JST)
    res = no.sync(["202607", "202608", "202609"], now=now)
    assert len(calls) == 1 and res["deferred"] == ["202608", "202609"] and res["failed"] == ["202607"]
    # 確定扱いの月の ZIP が壊れていた → status の failing に ledger_error、downloaded_at を消して取り直させる
    no.monthly_zip_path("202605").write_bytes(b"PK broken")
    no._write_meta("202605", {"downloaded_at": "2026-06-05T04:00:00+09:00", "final": True})
    monkeypatch.setenv(no.ENV_SWITCH, "0")
    no.sync(["202605"], now=now)
    st = no.status(now)
    assert "ledger_error" in st["failing"]["202605"] and "downloaded_at" not in no._read_meta("202605")


def test_enabled_reads_dotenv_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(no.ENV_SWITCH, raising=False)
    monkeypatch.setattr(no, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("KEIBA_NAR_OFFICIAL=0\n", encoding="utf-8")
    assert not no.enabled()
    monkeypatch.setenv(no.ENV_SWITCH, "1")                    # シェルの値が .env より優先
    assert no.enabled()


def test_fit_anchor_ignores_live_rows(tmp_path, monkeypatch):
    """live 行 (解析時の過去走) が最近の 1 日に何十走積み上がっても、中央の窓の起点は一括データの日のまま。"""
    monkeypatch.setattr(tm, "DENSE_MIN_ROWS", 3)
    bulk = tmp_path / "netkeiba_raw.jsonl"
    live = tmp_path / "live-202610.jsonl"
    rows = []
    for i in range(4):
        rows.append({"h": f"J{i}", "d": 20250110, "v": "東京", "s": "ダート", "dist": 1400, "g": "良", "t": 84.0 + i * .1, "wt": 0})
        rows.append({"h": f"J{i}", "d": 20260520, "v": "中山", "s": "ダート", "dist": 1200, "g": "良", "t": 72.0 + i * .1, "wt": 0})
    bulk.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    live.write_text("".join(json.dumps({"h": f"L{i}", "d": 20261004, "v": "東京", "s": "ダート", "dist": 1600, "g": "良",
                                        "t": 97.0 + i * .1, "wt": 0}, ensure_ascii=False) + "\n" for i in range(10)),
                    encoding="utf-8")
    m = tm.fit(tm.load_ledger([live, bulk]))
    assert m["data_until_by_circuit"] == {"jra": "2026-05-20"}
    assert "ダート|1400|東京" in m["theta"]
