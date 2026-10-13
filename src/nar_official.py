"""地方競馬全国協会 (NAR) 公式データダウンロード (keiba.go.jp DataDownload) の取得・解析。

keiba.go.jp の「本日のレース情報」「月別開催日程」ページ下部のデータダウンロード (2026-10-13 発見)。
説明書: https://www.keiba.go.jp/pdf/manual/data_pdf_manual.pdf
  - 当日ファイル  RaceDataDownload?type=daily                            約 2 分ごと更新 (推奨: 2 分に 1 回程度)
  - 月次ファイル  RaceDataDownload?type=monthly&k_year=YYYY&k_month=MM    毎日 02:00 頃更新 (推奨: 1 日 1 回まで)
  - ZIP の中身 = racelist.csv (レース条件・ラップ・通過順) / horselist.csv (全出走馬・着順・タイム・着差・人気) /
    payback.csv (払戻)。UTF-8 (BOM 付き)。レース情報は 1998-01 以降、オッズ情報 (OddsDataDownload) は 2026-03 以降。
  - 利用規約: 異常な件数のアクセスは IP 遮断あり / 掲載情報の転載・複製は禁止 → **手元の解析にだけ使い、再配布しない**。
  - 列は見出し名で引く (「仕様変更等は事前予告なしに行う場合もあります」— 列順が変わっても壊れにくく)。

取得方針 (www.keiba.go.jp は NAR のオッズ・出馬表・結果の本命ソースでもあるので、遮断されないことを最優先):
  - 月次: 確定した月 (月末の翌日の夜間更新より後に取り、中身が揃っている) は二度と取らない。当月と月末直後は
    夜間更新 (02:00 頃 → 03:00 判定) の後に 1 日 1 回まで。月初 1 日の夜間更新前は取らない。
  - 当日: 2 分に 1 回まで (短い timeout)。中身の日付が今日でなければ使わない。
  - 試行は **取りに行く前に** meta に記録 (記録できなければ取りに行かない) → 失敗が続いても 3h→6h→12h→24h の
    間隔でしか再試行しない。プロセスをまたいでファイルロックし、ロック後に鮮度を見直す。連続取得は 5 秒空ける。
  - 環境変数 KEIBA_NAR_OFFICIAL=0 で通信を全停止 (キャッシュだけ使う)。テストは conftest がこれを立てる。

用途:
  1. タイム換算モデル (src/time_model.py) の学習 ledger — 地方の **全レース・全着** の走破時計と勝ち時計・馬場
     → data/cache/time_ledger/nar_official-YYYYMM.jsonl。netkeiba 規制後 (2026-06〜) の開催日の馬場差 δ も
     推定できるようになり、勝ち時計があるので大差負け除外 (MAX_BEHIND) も効く。
  2. 当レース条件の補完 (距離・芝ダ・回り・天候・馬場・レース名) — oddspark 経路 / DebaTable ヘッダ取得失敗時。
     月次キャッシュ (通信なし) を先に見て、当日の天候・馬場だけ当日ファイルで上書きする。bet 段は通信しない。
  3. 確定オッズ (OddsDataDownload, 検証用・手動 `python -m src.nar_official sync-odds`) — 学習・予想には使わない。

通信は _download (fetch_monthly / fetch_daily 経由) だけ。解析系 (read_zip / ledger_rows / parse_time /
race_info(allow_download=False)) は手元のファイルだけ読む。
"""
from __future__ import annotations

import contextlib
import csv
import datetime as _dt
import io
import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator

from . import time_model as tm

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache" / "nar_official"
BASE_URL = "https://www.keiba.go.jp/KeibaWeb/DataDownload"
RACE_URL = f"{BASE_URL}/RaceDataDownload"
ODDS_URL = f"{BASE_URL}/OddsDataDownload"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
JST = _dt.timezone(_dt.timedelta(hours=9))
ENV_SWITCH = "KEIBA_NAR_OFFICIAL"

# 月次ファイルの夜間更新 (02:00 頃) が終わったとみなす時刻。これより前に取った月次ファイルは「前日まで」の内容。
NIGHTLY_UPDATE_HOUR = 3
DAILY_MIN_INTERVAL_SEC = 120      # 当日ファイル: 説明書の推奨「2 分に 1 回程度」
DAILY_TIMEOUT_SEC = 10.0          # 当日ファイルは解析中の補完に使うので短く (失敗しても解析は止めない)
MONTHLY_TIMEOUT_SEC = 60.0
DOWNLOAD_GAP_SEC = 5.0            # 連続ダウンロード (backfill) の間隔
RETRY_AFTER_FAIL_SEC = 3 * 3600   # 取得失敗後の再試行間隔 (連続失敗で倍々・上限 MAX_BACKOFF_SEC)
MAX_BACKOFF_SEC = 24 * 3600
FINAL_COMPLETE_SHARE = 0.95       # 確定とみなす中身: 月の過去レースのうち完走記録のあるレースの割合
FINAL_GRACE_DAYS = 7              # 月末後も中身が揃わない月を取り直し続ける期限
MAX_DOWNLOADS_PER_SYNC = 24       # 1 回の sync で取る月数の上限 (手動 backfill の取りすぎ防止)
SYNC_BUDGET_SEC = 300.0           # 1 回の sync の所要上限 (API の再学習 subprocess は 900 秒で打ち切られる)
_CLOCK_SKEW = _dt.timedelta(hours=1)
BANEI_VENUES = {"帯広ば", "帯広"}  # ばんえい (直線 200m・馬場は水分量) は時計換算の対象外
ODDS_SINCE = "202603"             # オッズ情報の提供開始月

_DL_LOCK = threading.Lock()
_LAST_DL_AT = [0.0]


def enabled() -> bool:
    """公式データの通信を許可するか (KEIBA_NAR_OFFICIAL=0/false/off で停止)。

    シェルの環境変数が優先・無ければリポジトリ直下 .env (automation_lock と同じ流儀 — make time-model /
    make nar-official / CLI は .env を読み込まないので、.env に書いた停止がそこでも効くように)。"""
    v = os.environ.get(ENV_SWITCH)
    if v is None:
        try:
            from dotenv import dotenv_values
            v = dotenv_values(ROOT / ".env").get(ENV_SWITCH)
        except Exception:  # noqa: BLE001 — dotenv 不在/読めない .env は「未設定」= 許可
            v = None
    return (v or "1").strip().lower() not in ("0", "false", "no", "off")


# ------------------------------------------------------------ 取得 ----

def _now() -> _dt.datetime:
    return _dt.datetime.now(JST)


def _download(url: str, *, timeout: float = MONTHLY_TIMEOUT_SEC, deadline_sec: float | None = None) -> bytes:
    """URL → bytes。ZIP でない/CRC が壊れている応答は例外。連続取得は DOWNLOAD_GAP_SEC 空け、
    全体の所要は deadline_sec (既定 timeout×3) で打ち切る (urlopen の timeout は 1 回の読み書きごとなので、
    細切れに届く応答で長く待たされないように)。"""
    if not enabled():
        raise RuntimeError(f"{ENV_SWITCH}=0: 公式データの通信は停止中")
    limit = deadline_sec if deadline_sec is not None else timeout * 3
    with _DL_LOCK:
        wait = DOWNLOAD_GAP_SEC - (time.time() - _LAST_DL_AT[0])
        if wait > 0:
            time.sleep(wait)
        try:
            deadline = time.monotonic() + limit
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            chunks: list[bytes] = []
            with urllib.request.urlopen(req, timeout=timeout) as r:
                while True:
                    b = r.read1(1 << 16)   # read() は 64KiB 溜まるまで返らず、細切れの応答で deadline を見られない
                    if not b:
                        break
                    chunks.append(b)
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"download deadline exceeded ({limit:.0f}s)")
            data = b"".join(chunks)
        finally:
            _LAST_DL_AT[0] = time.time()
    _check_zip(data)
    return data


def _validate_odds(data: bytes) -> None:
    """確定オッズ ZIP は大きい (月 160 万行) ので、各 CSV の見出しと 1 行目だけ見る (全行を辞書にしない)。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("オッズ CSV が無い")
        for n in names:
            with z.open(n) as fh:
                head = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                cols = next(csv.reader(head), [])
                if "賭式" not in cols or "オッズ" not in cols or next(csv.reader(head), None) is None:
                    raise ValueError(f"{n}: 見出しが想定と違うか空")


def _check_zip(data: bytes) -> None:
    """ZIP でない応答 (エラーページ等) と CRC の壊れた ZIP を弾く (testzip は壊れたメンバー名を返すだけ)。"""
    if not data.startswith(b"PK"):
        raise ValueError(f"ZIP ではない応答 ({len(data)} bytes): {data[:80]!r}")
    bad = zipfile.ZipFile(io.BytesIO(data)).testzip()
    if bad is not None:
        raise zipfile.BadZipFile(f"CRC error in {bad}")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@contextlib.contextmanager
def _file_lock(name: str) -> Iterator[None]:
    """プロセスをまたぐ排他 (fcntl.flock)。ロックが取れない環境では排他なしで続行する。"""
    f = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = open(CACHE_DIR / f".{name}.lock", "a+")
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX)
    except (OSError, ImportError):
        pass
    try:
        yield
    finally:
        if f is not None:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass
            f.close()


def month_key(year: int, month: int) -> str:
    return f"{year:04d}{month:02d}"


def _month_bounds(ym: str) -> tuple[_dt.date, _dt.date]:
    y, m = int(ym[:4]), int(ym[4:6])
    first = _dt.date(y, m, 1)
    nxt = _dt.date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt


def months_between(start: str, end: str) -> list[str]:
    """'YYYYMM' から 'YYYYMM' まで (両端含む) の月リスト。"""
    out: list[str] = []
    y, m = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    while (y, m) <= (ey, em):
        out.append(month_key(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def monthly_zip_path(ym: str, kind: str = "race") -> Path:
    return CACHE_DIR / f"{kind}-{ym}.zip"


def _meta_path(ym: str, kind: str = "race") -> Path:
    return CACHE_DIR / f"{kind}-{ym}.meta.json"


def _read_meta(ym: str, kind: str = "race") -> dict:
    try:
        return json.loads(_meta_path(ym, kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(ym: str, meta: dict, kind: str = "race") -> None:
    _atomic_write(_meta_path(ym, kind), json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def _parse_iso(s: Any) -> _dt.datetime | None:
    try:
        d = _dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=JST)


def _last_nightly_update(now: _dt.datetime) -> _dt.datetime:
    """直近の夜間更新完了時刻 (今日の 03:00 か、まだなら昨日の 03:00)。"""
    t = now.replace(hour=NIGHTLY_UPDATE_HOUR, minute=0, second=0, microsecond=0)
    return t if now >= t else t - _dt.timedelta(days=1)


def month_final_after(ym: str) -> _dt.datetime:
    """この時刻より後に取った (かつ中身が揃った) 月次ファイルは確定 (月末の翌日の夜間更新が済んでいる)。"""
    _first, nxt = _month_bounds(ym)
    d = nxt + _dt.timedelta(days=1)
    return _dt.datetime(d.year, d.month, d.day, NIGHTLY_UPDATE_HOUR, tzinfo=JST)


def month_needs_download(ym: str, now: _dt.datetime | None = None, kind: str = "race") -> bool:
    """月次ファイルを取りに行くべきか (取得方針のみ。失敗後の待ちは _attempt_blocked)。

    - 未来の月 / 未取得の当月で月初 1 日の夜間更新前 → False
    - 未取得・取得時刻が未来 (時計ずれ) → True
    - 確定済み (月末の翌日の夜間更新より後に取得し、中身が揃っていた) → False (二度と取り直さない)
    - 月末後に取ったのに中身が揃っていない → 月末 + FINAL_GRACE_DAYS までは 1 日 1 回取り直す
    - 未確定 (当月・月末直後) → 直近の夜間更新より前に取ったものだけ True (= 1 日 1 回まで)
    """
    now = now or _now()
    first, _nxt = _month_bounds(ym)
    if first > now.date():
        return False
    meta = _read_meta(ym, kind)
    got = _parse_iso(meta.get("downloaded_at"))
    if got is None or not monthly_zip_path(ym, kind).exists() or got > now + _CLOCK_SKEW:
        if first == now.date() and now.hour < NIGHTLY_UPDATE_HOUR:
            return False          # 月初 1 日の夜間更新前に取ると同じ日にもう 1 回取ることになる
        return True
    final_after = month_final_after(ym)
    if got >= final_after:
        if meta.get("final", True):
            return False
        if now >= final_after + _dt.timedelta(days=FINAL_GRACE_DAYS):
            return False          # 揃わないまま期限切れ (中止開催などで本当に欠けている) → 諦める
    return got < _last_nightly_update(now)


def _backoff_sec(fail_count: int) -> float:
    return float(min(RETRY_AFTER_FAIL_SEC * 2 ** max(0, fail_count - 1), MAX_BACKOFF_SEC))


def _attempt_blocked(meta: dict, now: _dt.datetime) -> bool:
    """直近の試行 (取りに行く前に記録) が成功で終わっていなければ、連続失敗数に応じた間隔は取りに行かない。"""
    last = _parse_iso(meta.get("last_attempt_at"))
    if last is None or last > now + _CLOCK_SKEW:
        return False
    got = _parse_iso(meta.get("downloaded_at"))
    if got is not None and last <= got <= now + _CLOCK_SKEW:
        return False                                   # 直近の試行は成功 (未来の取得時刻 = 時計ずれは信用しない)
    wait = _backoff_sec(max(1, int(meta.get("fail_count") or 0)))
    return (now - last).total_seconds() < wait


# 解析に使う列 (見出し名)。説明書の「仕様変更等は事前予告なしに行う場合も」に備え、無ければ保存しない。
REQUIRED_COLUMNS = {
    "racelist": ("競馬場", "競走年月日", "レース番号", "レース名", "芝ダート区分", "距離", "天候", "馬場"),
    "horselist": ("競馬場", "競走年月日", "レース番号", "馬名", "着順", "タイム"),
}


def _validate_tables(data: bytes, *, need: tuple[str, ...]) -> dict[str, list[dict[str, str]]]:
    """ZIP の中身に必要な表が 1 行以上あり、解析に使う列が揃っているか (列名変更・空の応答を保存しない)。"""
    tables = read_zip(data)
    for kind in need:
        rows = tables.get(kind)
        if not rows:
            raise ValueError(f"{kind}.csv が無いか空 (仕様変更/メンテナンス?) — 保存しない")
        missing = [c for c in REQUIRED_COLUMNS.get(kind, ()) if c not in rows[0]]
        if missing:
            raise ValueError(f"{kind}.csv に列 {missing} が無い (仕様変更?) — 保存しない")
    return tables


def _race_dates(tables: dict[str, list[dict[str, str]]]) -> set[int]:
    return {_int(r.get("競走年月日", "")) for r in tables.get("racelist", [])} - {0}


def _check_not_shrunk(old_path: Path, tables: dict[str, list[dict[str, str]]], now: _dt.datetime) -> None:
    """手元の同じ月のファイルにあった過去の開催日が新しいファイルで消えていたら保存しない (途中で切れた応答を
    確定扱いにして、揃っていた月を上書きしないため)。"""
    if not old_path.exists():
        return
    try:
        old = read_zip(old_path, kinds=("racelist",))
    except (OSError, zipfile.BadZipFile, ValueError, csv.Error):
        return                                         # 手元が壊れているなら新しい方を採る
    today = _today_int(now)
    lost = {d for d in _race_dates(old) if d < today} - _race_dates(tables)
    if lost:
        raise ValueError(f"開催日 {sorted(lost)[:5]} が新しいファイルから消えている — 保存しない")


def _month_complete(tables: dict[str, list[dict[str, str]]], now: _dt.datetime) -> bool:
    """月の過去レース (今日より前) のうち、完走記録 (着順+タイム) のあるレースが FINAL_COMPLETE_SHARE 以上か。"""
    today = now.year * 10000 + now.month * 100 + now.day
    past = {(r.get("競馬場", ""), _int(r.get("競走年月日", "")), _int(r.get("レース番号", "")))
            for r in tables.get("racelist", []) if 0 < _int(r.get("競走年月日", "")) < today}
    if not past:
        return False
    done = {(r.get("競馬場", ""), _int(r.get("競走年月日", "")), _int(r.get("レース番号", "")))
            for r in tables.get("horselist", [])
            if unicodedata.normalize("NFKC", r.get("着順", "")).strip().isdigit() and parse_time(r.get("タイム", ""))}
    return len(past & done) / len(past) >= FINAL_COMPLETE_SHARE


def fetch_monthly(ym: str, *, kind: str = "race", force: bool = False,
                  now: _dt.datetime | None = None) -> tuple[Path | None, bool]:
    """月次 ZIP (kind="race" レース情報 / "odds" 確定オッズ) を (必要なら) 取得
    → (キャッシュのパス or None, 今回ダウンロードしたか)。失敗してもキャッシュがあればそれを返す。"""
    now = now or _now()
    path = monthly_zip_path(ym, kind)
    cached = (path if path.exists() else None), False
    if not enabled():
        return cached
    if not force and not month_needs_download(ym, now, kind):
        return cached
    url = (f"{RACE_URL if kind == 'race' else ODDS_URL}?type=monthly"
           f"&k_year={int(ym[:4])}&k_month={int(ym[4:6]):02d}")
    with _file_lock(f"monthly-{kind}"):
        meta = _read_meta(ym, kind)
        if not force and (not month_needs_download(ym, now, kind) or _attempt_blocked(meta, now)):
            return (path if path.exists() else None), False     # 別プロセスが取った / 失敗直後
        meta["last_attempt_at"] = now.isoformat(timespec="seconds")
        try:
            _write_meta(ym, meta, kind)       # 試行を記録できない (書き込み不可・容量不足) なら取りに行かない
        except OSError:
            return cached
        try:
            data = _download(url, timeout=MONTHLY_TIMEOUT_SEC)
            if kind == "race":
                tables = _validate_tables(data, need=("racelist", "horselist"))
                _check_not_shrunk(path, tables, now)
            else:
                _validate_odds(data)
                tables = {}
            _atomic_write(path, data)
        except Exception as e:  # noqa: BLE001 — 取得失敗は呼び出し側を止めない
            meta.update({"last_error": repr(e)[:300], "fail_count": int(meta.get("fail_count") or 0) + 1})
            try:
                _write_meta(ym, meta, kind)
            except OSError:
                pass
            return (path if path.exists() else None), False
        final = now >= month_final_after(ym) and (kind != "race" or _month_complete(tables, now))
        try:
            _write_meta(ym, {"downloaded_at": now.isoformat(timespec="seconds"),
                             "last_attempt_at": now.isoformat(timespec="seconds"),
                             "bytes": len(data), "url": url, "final": final, "fail_count": 0}, kind)
        except OSError:
            pass
    return path, True


def daily_zip_path() -> Path:
    return CACHE_DIR / "daily-race.zip"


def _daily_meta_path() -> Path:
    return CACHE_DIR / "daily-race.meta.json"


def _read_daily_meta() -> dict:
    try:
        return json.loads(_daily_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _today_int(now: _dt.datetime) -> int:
    return now.year * 10000 + now.month * 100 + now.day


def _zip_date(data: bytes | Path) -> int:
    """当日 ZIP のメンバー名の日付 (20261013_racelist.csv → 20261013)。読めなければ 0。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data) as z:
            for n in z.namelist():
                head = n.rsplit("/", 1)[-1].split("_", 1)[0]
                if head.isdigit() and len(head) == 8:
                    return int(head)
    except (OSError, zipfile.BadZipFile):
        pass
    return 0


def daily_cache_for(date: int) -> Path | None:
    """手元の当日ファイルが date (YYYYMMDD) のものならそのパス (通信しない)。"""
    meta = _read_daily_meta()
    p = daily_zip_path()
    if int(meta.get("date") or 0) == date and p.exists():
        return p
    return None


def fetch_daily(*, now: _dt.datetime | None = None, min_interval_sec: float = DAILY_MIN_INTERVAL_SEC,
                timeout: float = DAILY_TIMEOUT_SEC) -> Path | None:
    """当日ファイル (今日の全レース: 条件・天候・馬場・確定済みの着順/タイム) を取得 (2 分に 1 回まで)。

    今日の日付のキャッシュが min_interval 以内ならそのまま返す。取得失敗・中身の日付が今日でない (日付が
    変わった直後の古い応答) ときは、今日のキャッシュがあればそれ、無ければ None。
    """
    now = now or _now()
    today = _today_int(now)

    def _fresh(meta: dict) -> bool:
        got = _parse_iso(meta.get("downloaded_at"))
        return (got is not None and int(meta.get("date") or 0) == today and daily_zip_path().exists()
                and 0 <= (now - got).total_seconds() < min_interval_sec)

    if _fresh(_read_daily_meta()):
        return daily_zip_path()
    if not enabled():
        return daily_cache_for(today)
    with _file_lock("daily"):
        meta = _read_daily_meta()
        if _fresh(meta):
            return daily_zip_path()                     # 別プロセスが取ったばかり
        last_try = _parse_iso(meta.get("last_attempt_at"))
        if last_try is not None and 0 <= (now - last_try).total_seconds() < min_interval_sec:
            return daily_cache_for(today)               # 直前に試したばかり (失敗含む)
        meta["last_attempt_at"] = now.isoformat(timespec="seconds")
        try:
            _atomic_write(_daily_meta_path(), json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        except OSError:
            return daily_cache_for(today)
        try:
            data = _download(f"{RACE_URL}?type=daily", timeout=timeout, deadline_sec=timeout * 1.5)
            _validate_tables(data, need=("racelist",))
            d = _zip_date(data)
            if d != today:
                raise ValueError(f"当日ファイルの日付が今日ではない ({d})")
            _atomic_write(daily_zip_path(), data)
        except Exception as e:  # noqa: BLE001
            meta.update({"last_error": repr(e)[:300]})
            try:
                _atomic_write(_daily_meta_path(), json.dumps(meta, ensure_ascii=False).encode("utf-8"))
            except OSError:
                pass
            return daily_cache_for(today)
        try:
            _atomic_write(_daily_meta_path(), json.dumps(
                {"downloaded_at": now.isoformat(timespec="seconds"),
                 "last_attempt_at": now.isoformat(timespec="seconds"),
                 "date": today, "bytes": len(data)}, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass
    return daily_zip_path()


# ------------------------------------------------------------ 解析 ----

def read_zip(path: Path | str | bytes, kinds: tuple[str, ...] | None = None) -> dict[str, list[dict[str, str]]]:
    """ZIP → {"racelist": [...], "horselist": [...], "payback": [...] (オッズ ZIP は "odds")}。

    各行は見出し名 → 値。kinds を渡すとその表だけ読む (racelist だけなら horselist の解析を省ける)。
    """
    src: Any = io.BytesIO(path) if isinstance(path, (bytes, bytearray)) else path
    out: dict[str, list[dict[str, str]]] = {}
    with zipfile.ZipFile(src) as z:
        for name in z.namelist():
            stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            kind = stem.split("_")[-1]          # 202606_racelist → racelist / 202606_01_odds → odds
            if kinds is not None and kind not in kinds:
                continue
            text = z.read(name).decode("utf-8-sig", "replace")
            rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items() if isinstance(v, str) or v is None}
                    for r in csv.DictReader(io.StringIO(text))]
            out.setdefault(kind, []).extend(rows)
    return out


def parse_time(s: str) -> float | None:
    """走破タイム → 秒。公式 CSV は "MSSd" (1259 = 1:25.9) / "SSd" (597 = 59.7) の数字列。"1:25.9" も可。"""
    s = unicodedata.normalize("NFKC", s or "").strip()
    if not s:
        return None
    try:
        if ":" in s:
            m, rest = s.split(":", 1)
            sec = int(m) * 60 + float(rest)
        elif "." in s:
            sec = float(s)
        elif s.isdigit():
            if len(s) <= 3:
                sec = int(s) / 10.0
            else:
                mins, rest = int(s[:-3]), int(s[-3:])
                if rest >= 600:          # 秒の部分が 60.0 以上 = 形式違い
                    return None
                sec = mins * 60 + rest / 10.0
        else:
            return None
    except ValueError:
        return None
    return sec if sec > 0 else None


def _int(s: str) -> int:
    s = unicodedata.normalize("NFKC", s or "").strip()
    return int(s) if s.isdigit() else 0


def _surface(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw or "").strip()
    return {"ダート": "ダート", "ダ": "ダート", "芝": "芝", "障害": "障害", "障": "障害"}.get(s, "")


_GOING_WORDS = ("稍重", "不良", "良", "重")


def race_infos(tables: dict[str, list[dict[str, str]]]) -> dict[tuple[str, int, int], dict]:
    """racelist → {(場, 日付 YYYYMMDD int, R): 条件 dict}。場は CSV の表記のまま (ばんえい = "帯広ば")。"""
    out: dict[tuple[str, int, int], dict] = {}
    for r in tables.get("racelist", []):
        venue = r.get("競馬場", "")
        d, rn = _int(r.get("競走年月日", "")), _int(r.get("レース番号", ""))
        if not venue or not d or not rn:
            continue
        going = unicodedata.normalize("NFKC", r.get("馬場", "")).strip()
        hhmm = _int(r.get("発走時刻", ""))
        out[(venue, d, rn)] = {
            "venue": venue, "date": d, "race_no": rn,
            "start_hhmm": f"{hhmm // 100:02d}:{hhmm % 100:02d}" if hhmm else "",
            "kind": r.get("競走種類名称", ""), "race_name": r.get("レース名", ""),
            "surface": _surface(r.get("芝ダート区分", "")), "direction": r.get("回り", ""),
            "distance": _int(r.get("距離", "")), "weather": r.get("天候", ""),
            # 馬場はばんえいだと水分量 (数値) → going は空、moisture に入れる
            "going": going if going in _GOING_WORDS else "",
            "moisture": float(going) if going.replace(".", "", 1).isdigit() else None,
            "field_size": _int(r.get("頭数", "")), "conditions": r.get("条件", ""),
        }
    return out


def ledger_rows(tables: dict[str, list[dict[str, str]]]) -> list[dict]:
    """全着の走破時計 → time_model の ledger 行 (平地・時計が妥当なもののみ・勝ち時計とレース番号付き)。

    勝ち時計 = 同じレースの 1 着馬のタイム (同着は同時計)。取消・除外・中止・失格 (着順が空) とばんえいは
    入れない。r (レース番号) は参考勝ち時計 par_win を同日同条件の別レースと区別するため。
    """
    infos = race_infos(tables)
    by_race: dict[tuple[str, int, int], list[tuple[str, float, str]]] = {}
    for r in tables.get("horselist", []):
        venue = r.get("競馬場", "")
        if venue in BANEI_VENUES:
            continue
        key = (venue, _int(r.get("競走年月日", "")), _int(r.get("レース番号", "")))
        fin = unicodedata.normalize("NFKC", r.get("着順", "")).strip()
        sec = parse_time(r.get("タイム", ""))
        if key not in infos or not fin.isdigit() or sec is None:
            continue
        by_race.setdefault(key, []).append((r.get("馬名", ""), sec, fin))
    rows: list[dict] = []
    for key, runners in by_race.items():
        info = infos[key]
        distance = int(info["distance"])
        surface = tm.norm_surface(info["surface"], info["race_name"], distance)
        venue = tm.norm_venue(info["venue"])
        if not tm.is_known_venue(venue):
            continue
        wins = [sec for _n, sec, fin in runners if fin == "1"]
        win = min(wins) if wins else 0.0
        going = tm.norm_going(info["going"])
        for name, sec, _fin in runners:
            h = tm.norm_horse(name)
            if not h or not tm.valid_flat_time(surface, distance, sec):
                continue
            rows.append({"h": h, "d": info["date"], "v": venue, "s": surface, "dist": distance,
                         "g": going, "t": round(sec, 2),
                         "wt": round(win, 2) if win and win <= sec + 1e-9 else 0.0, "r": info["race_no"]})
    return rows


# ------------------------------------------------------------ ledger 同期 ----

def ledger_path(ym: str) -> Path:
    return tm.LEDGER_DIR / f"nar_official-{ym}.jsonl"


def write_ledger(ym: str, zip_path: Path, *, force: bool = False) -> int:
    """月次 ZIP → time_ledger/nar_official-YYYYMM.jsonl を丸ごと書き直す (原子的)。返り値は行数。

    既存の ledger より極端に少ない (半分未満 = 列名変更などの異常) か、既存にあった開催日が消えるときは
    書き換えずに ValueError (force=True で強制: 解析ロジックを直して全月を作り直すとき)。
    """
    rows = ledger_rows(read_zip(zip_path, kinds=("racelist", "horselist")))
    lp = ledger_path(ym)
    if not force and lp.exists():
        old_n, old_days = 0, set()
        try:
            with open(lp, encoding="utf-8") as f:
                for line in f:
                    old_n += 1
                    try:
                        old_days.add(int(json.loads(line).get("d") or 0))
                    except (ValueError, AttributeError):
                        pass
        except OSError:
            pass
        lost = old_days - {r["d"] for r in rows} - {0}
        if old_n and (len(rows) < old_n * 0.5 or lost):
            raise ValueError(f"{ym}: 新しい ledger が {len(rows)} 行 (既存 {old_n} 行)"
                             f"{f'・開催日 {sorted(lost)[:5]} が消える' if lost else ''} — 書き換えない")
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    _atomic_write(lp, body.encode("utf-8"))
    return len(rows)


def default_since(now: _dt.datetime | None = None) -> str:
    """学習窓 (time_model.WINDOW_DAYS) の始まりの月。窓の外の月は取りに行かない。"""
    now = now or _now()
    d = now.date() - _dt.timedelta(days=tm.WINDOW_DAYS)
    return month_key(d.year, d.month)


def _note_ledger_error(ym: str, err: Exception, now: _dt.datetime, *, corrupt_zip: bool) -> None:
    """ledger 書き出しの失敗を meta に残す (status の failing に出す)。手元の ZIP が壊れているなら
    downloaded_at を消して、通常の取得方針 (失敗間隔つき) で取り直させる。"""
    meta = _read_meta(ym)
    meta.update({"ledger_error": repr(err)[:300], "ledger_error_at": now.isoformat(timespec="seconds")})
    if corrupt_zip:
        meta.pop("downloaded_at", None)
        meta["final"] = False
    try:
        _write_meta(ym, meta)
    except OSError:
        pass


def sync(months: list[str] | None = None, *, since: str | None = None, now: _dt.datetime | None = None,
         log: Callable[[str], None] | None = None, max_downloads: int = MAX_DOWNLOADS_PER_SYNC,
         budget_sec: float = SYNC_BUDGET_SEC) -> dict:
    """月次レース情報を必要な分だけ取得し、ledger を更新する (ネットワーク)。

    months 省略時は since (既定 = 学習窓の始まり) から今月まで。取得方針は month_needs_download /
    _attempt_blocked。ZIP が ledger より新しい月だけ ledger を書き直す。1 か月の失敗は他の月を止めない。
    取りに行くのは 1 回に max_downloads か月まで・最初の取得失敗の後は打ち切り・所要 budget_sec まで
    (残りは deferred = 次回。相手が落ちている/遅いときに 1 回の同期で全月ぶん待たない)。
    """
    now = now or _now()
    t_start = time.monotonic()
    out: dict[str, Any] = {"downloaded": [], "cached": [], "missing": [], "failed": [], "deferred": [],
                           "ledger_written": [], "rows": 0, "disabled": not enabled()}
    months = months or months_between(since or default_since(now), month_key(now.year, now.month))
    n_dl = 0
    stop_network = False
    for ym in months:
        try:
            over = n_dl >= max_downloads or stop_network or time.monotonic() - t_start > budget_sec
            if over and enabled() and month_needs_download(ym, now):
                out["deferred"].append(ym)
                if not monthly_zip_path(ym).exists():
                    continue
                path, fresh = monthly_zip_path(ym), False
            else:
                before = int(_read_meta(ym).get("fail_count") or 0)
                path, fresh = fetch_monthly(ym, now=now)
                n_dl += int(fresh)
                if not fresh and int(_read_meta(ym).get("fail_count") or 0) > before:
                    stop_network = True        # 取得に失敗した = 相手が落ちている/遅い → この回はもう取りに行かない
            if path is None:
                out["missing"].append(ym)
                continue
            (out["downloaded"] if fresh else out["cached"]).append(ym)
            lp = ledger_path(ym)
            if fresh or not lp.exists() or lp.stat().st_mtime < path.stat().st_mtime:
                try:
                    n = write_ledger(ym, path)
                except (zipfile.BadZipFile, csv.Error) as e:
                    _note_ledger_error(ym, e, now, corrupt_zip=True)
                    raise
                except Exception as e:  # noqa: BLE001
                    _note_ledger_error(ym, e, now, corrupt_zip=False)
                    raise
                meta = _read_meta(ym)
                if meta.pop("ledger_error", None) is not None:
                    meta.pop("ledger_error_at", None)
                    try:
                        _write_meta(ym, meta)
                    except OSError:
                        pass
                out["ledger_written"].append(ym)
                out["rows"] += n
                if log:
                    log(f"nar_official {ym}: {'downloaded' if fresh else 'cached'} → {n:,} ledger rows")
        except Exception as e:  # noqa: BLE001 — 1 か月の失敗 (壊れたキャッシュ・書き込み不可など) で他を止めない
            out["failed"].append(ym)
            if log:
                log(f"nar_official {ym}: 失敗 {e!r}")
    for ym in months:
        meta = _read_meta(ym)
        if meta.get("last_error") and int(meta.get("fail_count") or 0) > 0 and ym not in out["failed"]:
            out["failed"].append(ym)       # 取得失敗 (古いキャッシュで続行した月) も見えるように
    return out


def sync_due(now: _dt.datetime | None = None, *, since: str | None = None) -> bool:
    """取りに行くべき月があり、失敗後の待ちも明けているか (API 常駐ループの判定用・通信なし)。"""
    if not enabled():
        return False
    now = now or _now()
    for ym in months_between(since or default_since(now), month_key(now.year, now.month)):
        if month_needs_download(ym, now) and not _attempt_blocked(_read_meta(ym), now):
            return True
    return False


def status(now: _dt.datetime | None = None) -> dict:
    """GET /api/results/auto 用の同期状態 (通信なし)。"""
    now = now or _now()
    months = months_between(default_since(now), month_key(now.year, now.month))
    metas = {ym: _read_meta(ym) for ym in months}
    failing = {ym: {"last_error": m.get("last_error"), "last_attempt_at": m.get("last_attempt_at"),
                    "fail_count": m.get("fail_count")}
               for ym, m in metas.items() if m.get("last_error") and int(m.get("fail_count") or 0) > 0}
    for ym, m in metas.items():
        if m.get("ledger_error"):
            failing.setdefault(ym, {})["ledger_error"] = m["ledger_error"]
            failing[ym]["ledger_error_at"] = m.get("ledger_error_at")
    daily = _read_daily_meta()
    return {"enabled": enabled(), "months_in_window": len(months),
            "months_cached": sum(1 for ym in months if monthly_zip_path(ym).exists()),
            "last_downloaded_at": max((m.get("downloaded_at") or "" for m in metas.values()), default="") or None,
            "due": sync_due(now), "failing": failing,
            "daily": {"downloaded_at": daily.get("downloaded_at"), "date": daily.get("date"),
                      "last_error": daily.get("last_error") if daily.get("last_attempt_at", "") >
                      (daily.get("downloaded_at") or "") else None}}


def sync_odds(months: list[str] | None = None, *, since: str = ODDS_SINCE, now: _dt.datetime | None = None,
              log: Callable[[str], None] | None = None) -> dict:
    """確定オッズ (全券種, 2026-03〜) の月次 ZIP を取得 (検証用・手動のみ。API や学習からは呼ばない)。"""
    now = now or _now()
    months = months or months_between(max(since, ODDS_SINCE), month_key(now.year, now.month))
    out: dict[str, Any] = {"downloaded": [], "cached": [], "missing": []}
    for ym in months:
        path, fresh = fetch_monthly(ym, kind="odds", now=now)
        (out["missing"] if path is None else out["downloaded"] if fresh else out["cached"]).append(ym)
        if log and fresh:
            log(f"nar_official odds {ym}: downloaded")
    return out


# ------------------------------------------------------------ 当レース条件 ----

_INFO_CACHE: dict[str, tuple[float, dict]] = {}


def _infos_of(path: Path) -> dict[tuple[str, int, int], dict]:
    """ZIP の racelist → race_infos (mtime が同じ間はプロセス内で使い回す)。"""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return {}
    hit = _INFO_CACHE.get(str(path))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        infos = race_infos(read_zip(path, kinds=("racelist",)))
    except (OSError, zipfile.BadZipFile, ValueError, csv.Error):
        return {}
    _INFO_CACHE[str(path)] = (mt, infos)
    return infos


def _lookup(infos: dict[tuple[str, int, int], dict], venue: str, date: int, race_no: int) -> dict | None:
    for (cv, cd, rn), info in infos.items():
        if cd == date and rn == race_no and venue and cv[:2] == venue[:2]:
            return info
    return None


def race_info(venue: str, date: int | str, race_no: int, *, allow_download: bool = True,
              now: _dt.datetime | None = None) -> dict | None:
    """(場名, YYYYMMDD, R) の公式レース条件 (距離・芝ダ・回り・天候・馬場・レース名 等)。無ければ None。

    1) 手元の月次キャッシュ (通信なし。翌日以降の出馬表も載っている — 天候・馬場は空)
    2) 今日のレースで天候・馬場が無いときだけ当日ファイルで上書き (allow_download=False は手元の今日の
       当日ファイルだけ。bet 段・オフライン用)。場名は CSV 表記 (ばんえいは "帯広ば") と前方 2 文字で照合。
    """
    try:
        now = now or _now()
        d = int(str(date).replace("-", "").replace("/", "").replace(".", "")[:8])
        rn = int(race_no)
        v = unicodedata.normalize("NFKC", venue or "").strip()
        base = None
        mp = monthly_zip_path(f"{d // 100:06d}")
        if mp.exists():
            base = _lookup(_infos_of(mp), v, d, rn)
        need_day = base is None or not base.get("weather") or (not base.get("going") and base.get("moisture") is None)
        if d == _today_int(now) and need_day:
            p = fetch_daily(now=now) if allow_download else daily_cache_for(d)
            day = _lookup(_infos_of(p), v, d, rn) if p is not None else None
            if day is not None:
                if base is None:
                    base = day
                else:
                    base = dict(base)
                    for k in ("weather", "going", "moisture"):
                        if day.get(k) not in ("", None):
                            base[k] = day[k]
        return base
    except Exception:  # noqa: BLE001 — 補完は best-effort (解析を止めない)
        return None


def official_race_header(netkeiba_rid: str, *, allow_download: bool = True) -> dict:
    """netkeiba の地方 race_id → parse_deba_header と同じ形の当レース条件 dict (取れた項目のみ・無ければ {})。

    keys: distance / surface / direction / weather / going (全語 "良"|"稍重"|"重"|"不良") / moisture (ばんえい) /
    race_name (レース名 = クラス表記を含む, NFKC)。
    DebaTable ヘッダが取れない keiba.go.jp 経路と、当レース条件を持たない oddspark 経路の補完用。
    """
    try:
        from .parse import VENUE_CODE, is_nar_race_id, normalize_going_word
        rid = str(netkeiba_rid or "")
        if len(rid) < 12 or not rid.isdigit() or not is_nar_race_id(rid):
            return {}
        venue = VENUE_CODE.get(rid[4:6], "")
        info = race_info(venue, int(rid[:4] + rid[6:10]), int(rid[10:12]), allow_download=allow_download)
        if not info:
            return {}
        out: dict = {}
        if info["distance"] and info["surface"]:
            out["distance"] = int(info["distance"])
            out["surface"] = info["surface"]
            out["direction"] = info["direction"] if info["direction"] in ("右", "左", "直") else ""
        if info["weather"]:
            out["weather"] = info["weather"]
        going = normalize_going_word(info["going"])
        if going:
            out["going"] = going
        elif info["moisture"] is not None:
            out["moisture"] = info["moisture"]
        name = " ".join(unicodedata.normalize("NFKC", info["race_name"] or "").split())
        if name:
            out["race_name"] = name
        return out
    except Exception:  # noqa: BLE001 — 補完は best-effort
        return {}


def fill_race_header(hdr: dict | None, netkeiba_rid: str, *, allow_download: bool = True) -> dict:
    """DebaTable ヘッダに距離かレース名が無いときだけ、公式データの値で **欠けている項目だけ** 補う。"""
    hdr = dict(hdr or {})
    if hdr.get("distance") and hdr.get("race_name"):
        return hdr
    off = official_race_header(netkeiba_rid, allow_download=allow_download)
    take_course = not hdr.get("distance")      # 距離・芝ダ・回りは組で採用 (ヘッダ側に距離が無いときだけ)
    for k, v in off.items():
        if k in ("distance", "surface", "direction"):
            if take_course:
                hdr[k] = v
        elif not hdr.get(k) and hdr.get(k) != 0:
            hdr[k] = v
    return hdr


# ------------------------------------------------------------ 過去走・出走馬の補完 ----
# keiba.go.jp の馬柱 (HorseMarkInfo) / oddspark の HorseDetail は自走時計しか持たず、着差・斤量・上がり・通過順・
# 騎手・馬体重が無い。出馬表 (DebaTable) にも騎手・斤量・性齢が無い。学習データ (netkeiba 馬柱・出馬表) は全部
# 持っているので、LightGBM の特徴量・仮指数・適性・Claude に渡す前走表がその分だけ学習時とずれていた。
# 公式データ (手元のキャッシュ。score 段だけ当日ファイルを 2 分に 1 回まで取る) から同じ走 (馬名・日付・場・R) の
# 値を埋める。公式データ再現 (2025-10〜12 + 2026-06〜09, 8,392R・実装そのもの・リークなし):
# LightGBM NAR 勝ち馬 AUC 0.741 → 0.762・1 位勝率 +2.7pt・log-loss −0.072 (全月・全場で正、主役は上がり>通過>当日の
# 斤量)、本番の fundamental +0.012 AUC・+0.9pt。仮指数は +0.028 AUC・+1.4pt (主役は着差。地方の SI は斤量の項を使わない —
# 入れると +0.019 AUC・1 位勝率は変わらず)。適性は地方の総合から上がりを外して補完前と同値。
# ただし市場 (確定単勝オッズ) を超える情報は補完前後とも無い (α<0) — ランキングと較正の改善であって賭けの edge ではない。
# 値の形は netkeiba 馬柱と同じにする (同じ走 85,086 件で突き合わせ: 着差=自走−勝ち時計 は完全一致、
# 通過順は「並走は同じ位置」「直線の地点を除き最後の 4 か所」で一致、騎手は公式の 3 文字略称)。

_STRAIGHT_WORDS = ("正面", "直線")      # 公式のコーナー名称のうち netkeiba の通過順に入らない地点 (向正面/最初の直線 等)
_SEX = {"セン": "セ"}                   # 性齢は netkeiba 出馬表と同じ表記 (セン馬は「セ」)
_MARK_RE = re.compile(r"[^\d.]")


def _corner_ranks(s: str) -> dict[int, int]:
    """コーナー通過順 '1,10,(5,9)-3,4' → {馬番: 位置}。( ) の並走は同じ位置 (netkeiba 馬柱と同じ)、
    次の馬は並走の頭数ぶん後ろ ((5,9) の次は 5 番手)。"""
    pos: dict[int, int] = {}
    k = 0
    for grp, single in re.findall(r"\(([^)]*)\)|(\d+)", unicodedata.normalize("NFKC", s or "")):
        members = [int(x) for x in re.findall(r"\d+", grp)] if grp else [int(single)]
        for m in members:
            pos.setdefault(m, k + 1)
        k += len(members)
    return pos


def passing_of(corners: list[tuple[str, str]], num: int) -> str:
    """[(コーナー名称, 通過順)] と馬番 → netkeiba 馬柱と同じ通過順 ('3-3-2-1')。直線の地点を除いた最後の 4 か所。"""
    out = []
    for name, s in corners:
        if not s or any(w in (name or "") for w in _STRAIGHT_WORDS):
            continue
        p = _corner_ranks(s).get(num)
        if p:
            out.append(p)
    return "-".join(str(p) for p in out[-4:])


def _num(s: str) -> float:
    v = _MARK_RE.sub("", unicodedata.normalize("NFKC", s or ""))
    try:
        return float(v) if v else 0.0
    except ValueError:
        return 0.0


def _signed(s: str) -> int:
    t = unicodedata.normalize("NFKC", s or "").replace("±", "").strip()
    try:
        return int(t) if t not in ("", "+", "-") else 0
    except ValueError:
        return 0


def _scan_zip(path: Path, names: set[str] | None) -> dict[tuple[str, int, str], dict]:
    """ZIP (月次 or 当日) → {(馬名キー, 日付, 場の前方 2 文字): 1 走の記録}。names を渡すとその馬だけ (メモリ節約)。

    記録 = レース番号・馬番・枠番・自走時計・勝ち時計・斤量・上がり・騎手・馬体重(増減)・通過順・頭数・人気・着順・
    性・齢・調教師 (結果の無い = これからのレースは自走時計なし)。読めなければ {}。
    """
    try:
        with zipfile.ZipFile(path) as z:
            members = {n.rsplit("/", 1)[-1].rsplit(".", 1)[0].split("_")[-1]: n for n in z.namelist()}
            if "horselist" not in members:
                return {}
            race_rows = list(csv.reader(io.StringIO(z.read(members["racelist"]).decode("utf-8-sig", "replace")))) \
                if "racelist" in members else []
            horse_rows = list(csv.reader(io.StringIO(z.read(members["horselist"]).decode("utf-8-sig", "replace"))))
    except (OSError, zipfile.BadZipFile, KeyError, csv.Error):
        return {}
    races: dict[tuple[str, int, int], tuple[list[tuple[str, str]], int]] = {}
    if race_rows:
        hd = {c.strip(): i for i, c in enumerate(race_rows[0])}
        iv, idt, irn = hd.get("競馬場"), hd.get("競走年月日"), hd.get("レース番号")
        ifs = hd.get("頭数")
        corner_cols = [(hd.get(f"コーナー名称{i}"), hd.get(f"コーナー通過順{i}")) for i in range(1, 9)]
        if None not in (iv, idt, irn):
            for r in race_rows[1:]:
                if len(r) < len(hd):
                    continue
                corners = [(r[a].strip(), r[b].strip()) for a, b in corner_cols if a is not None and b is not None
                           and r[b].strip()]
                races[(r[iv].strip(), _int(r[idt]), _int(r[irn]))] = (corners, _int(r[ifs]) if ifs is not None else 0)
    hd = {c.strip(): i for i, c in enumerate(horse_rows[0])} if horse_rows else {}
    need = ("競馬場", "競走年月日", "レース番号", "馬番", "馬名", "着順", "タイム")
    if any(c not in hd for c in need):
        return {}
    g = lambda r, c: (r[hd[c]].strip() if c in hd and hd[c] < len(r) else "")  # noqa: E731
    winners: dict[tuple[str, int, int], float] = {}
    for r in horse_rows[1:]:
        if g(r, "着順") == "1":
            sec = parse_time(g(r, "タイム"))
            if sec:
                key = (g(r, "競馬場"), _int(g(r, "競走年月日")), _int(g(r, "レース番号")))
                winners[key] = min(sec, winners.get(key, sec))
    out: dict[tuple[str, int, str], dict] = {}
    for r in horse_rows[1:]:
        h = tm.norm_horse(g(r, "馬名"))
        if not h or (names is not None and h not in names):
            continue
        venue, d, rn = g(r, "競馬場"), _int(g(r, "競走年月日")), _int(g(r, "レース番号"))
        num = _int(g(r, "馬番"))
        corners, field = races.get((venue, d, rn), ([], 0))
        fin = unicodedata.normalize("NFKC", g(r, "着順"))
        out[(h, d, venue[:2])] = {
            "venue": venue, "date": d, "race_no": rn, "num": num, "bracket": _int(g(r, "枠番")),
            "own": parse_time(g(r, "タイム")), "winner": winners.get((venue, d, rn)),
            "weight": _num(g(r, "負担重量")), "last3f": _num(g(r, "上がり3F")),
            "jockey": " ".join(g(r, "騎手名").split()), "trainer": " ".join(g(r, "調教師").split()),
            "bw": _int(g(r, "馬体重")), "bwd": _signed(g(r, "馬体重増減")),
            "passing": passing_of(corners, num) if corners else "", "field": field,
            "pop": _int(g(r, "人気")), "fin": int(fin) if fin.isdigit() else None,
            "sex": g(r, "性"), "age": _int(g(r, "齢")), "name": g(r, "馬名"),
        }
    return out


def _records_for(names: set[str], dates: set[int]) -> dict[tuple[str, int, str], dict]:
    """必要な月 (dates の月) の月次キャッシュ + 当日キャッシュから、names の馬の記録を集める (通信なし)。

    当日ファイル (2 分ごと更新) の日付の行は **当日ファイルの値を優先** する (月次は前夜 02:00 時点なので、当日の
    騎手変更・斤量・馬体重・今日のレースの結果が無い)。空の項目だけは月次の値を残す。
    """
    recs: dict[tuple[str, int, str], dict] = {}
    for ym in sorted({f"{d // 100:06d}" for d in dates if d}):
        p = monthly_zip_path(ym)
        if p.exists():
            recs.update(_scan_zip(p, names))
    for d in sorted(dates):
        p = daily_cache_for(d)
        if p is None:
            continue
        for k, v in _scan_zip(p, names).items():
            if k not in recs:
                recs[k] = v
                continue
            merged = dict(recs[k])
            for f, val in v.items():
                if val not in ("", None, 0):
                    merged[f] = val
            recs[k] = merged
    return recs


def enrich_racedata(rd: Any, netkeiba_rid: str = "", *, banei: bool = False, allow_download: bool = False) -> dict:
    """公式データ (手元のキャッシュだけ・通信なし) で地方の RaceData を補う。返り値は件数の内訳 (ログ・テスト用)。

    - 過去走 (past_runs / past_runs_ext、地方の走だけ): 勝ち時計・着差 (= 自走 − 勝ち時計)・斤量・上がり・騎手・
      馬体重(増減)・通過順・頭数・人気・馬番。**空/0 の項目だけ**埋め、自走時計が公式と 0.15 秒以上ずれる・R が違う
      走は別の走とみなして触らない (同名馬の取り違え防止)。中央の走 (Ｊ函館 等) は公式データに無いので触らない。
    - 当レースの出走馬: 騎手・斤量・性齢・調教師・枠番・馬体重 (発表済みのとき) を、馬番と馬名が一致する行から
      空/0 の項目だけ。結果 (着順・タイム) は使わない (= 発走前に分かる情報だけ・リークしない)。
    - ばんえいは既定で触らない (banei=False): 本番の keiba.go.jp 経路はばんえいの過去走を持たない (200m を拾わない)
      ので、足せるのは当日のそり重量だけで効果なし (再現 −0.007 AUC, n.s.)。公式とnetkeibaのばんえいの項目は一致。
    - allow_download=True (score 段) は当レースが今日なら当日ファイルを先に更新する (2 分に 1 回まで)。bet 段は
      手元のキャッシュだけ。
    """
    st = {"past_total": 0, "past_filled": 0, "past_mismatch": 0, "entries_filled": 0}
    try:
        from .parse import VENUE_CODE, is_nar_race_id
        race = getattr(rd, "race", rd)
        rid = str(netkeiba_rid or "")
        is_banei = rid[4:6] == "65" or tm.norm_venue(getattr(race, "venue_name", "")) == "帯広"
        if is_banei and not banei:
            return st
        horses = [h for h in (getattr(race, "horses", None) or []) if not getattr(h, "absent", False)]
        names = {tm.norm_horse(getattr(h, "name", "")) for h in horses} - {""}
        runs_by_h: list[tuple[str, list]] = []
        dates: set[int] = set()
        for h in horses:
            seen: dict[int, Any] = {}
            for pr in list(getattr(h, "past_runs_ext", None) or []) + list(getattr(h, "past_runs", None) or []):
                seen.setdefault(id(pr), pr)
            runs = [pr for pr in seen.values() if tm.norm_venue(getattr(pr, "venue", "")) not in tm.JRA_VENUES]
            runs_by_h.append((tm.norm_horse(getattr(h, "name", "")), runs))
            dates.update(tm.date_key(getattr(pr, "date", "")) for pr in runs)
        target_date = int(rid[:4] + rid[6:10]) if len(rid) >= 12 and rid.isdigit() and is_nar_race_id(rid) else 0
        if allow_download and target_date and target_date == _today_int(_now()):
            fetch_daily()
        recs = _records_for(names, dates | ({target_date} if target_date else set()))
        for hname, runs in runs_by_h:
            for pr in runs:
                st["past_total"] += 1
                d = tm.date_key(getattr(pr, "date", ""))
                rec = recs.get((hname, d, tm.norm_venue(getattr(pr, "venue", ""))[:2]))
                if rec is None or not rec.get("own"):
                    continue
                own = float(getattr(pr, "own_time_sec", 0.0) or 0.0)
                rno = int(getattr(pr, "race_no", 0) or 0)
                if (own > 0 and abs(own - rec["own"]) > 0.15) or (rno and rec["race_no"] and rno != rec["race_no"]):
                    st["past_mismatch"] += 1
                    continue
                if rec.get("winner") and rec["winner"] <= rec["own"] + 1e-9 and not getattr(pr, "time_diff_sec", 0):
                    pr.winner_time_sec = round(rec["winner"], 2)
                    pr.time_diff_sec = round(max(rec["own"] - rec["winner"], 0.0), 2)
                for attr, key in (("weight_kg", "weight"), ("last_3f_sec", "last3f"), ("body_weight", "bw"),
                                  ("field_size", "field"), ("popularity", "pop"), ("horse_number", "num")):
                    if not getattr(pr, attr, 0) and rec.get(key):
                        setattr(pr, attr, rec[key])
                if not getattr(pr, "body_weight_diff", 0) and rec.get("bw") and rec.get("bwd"):
                    pr.body_weight_diff = rec["bwd"]
                for attr, key in (("jockey", "jockey"), ("passing", "passing")):
                    if not (getattr(pr, attr, "") or "").strip() and rec.get(key):
                        setattr(pr, attr, rec[key])
                st["past_filled"] += 1
        # 当レースの出走馬 (発走前に分かる項目だけ)
        if target_date:
            venue = VENUE_CODE.get(rid[4:6], "")
            rn = int(rid[10:12])
            by_num = {rec["num"]: rec for (h, d, _v), rec in recs.items()
                      if d == target_date and rec["race_no"] == rn and rec["venue"][:2] == venue[:2]}
            for h in horses:
                rec = by_num.get(int(getattr(h, "number", 0) or 0))
                if rec is None or tm.norm_horse(rec["name"]) != tm.norm_horse(getattr(h, "name", "")):
                    continue
                filled = False
                for attr, val in (("jockey_name", rec["jockey"]), ("trainer_name", rec["trainer"]),
                                  ("sex_age", f"{_SEX.get(rec['sex'], rec['sex'])}{rec['age']}"
                                   if rec["sex"] and rec["age"] else "")):
                    if not (getattr(h, attr, "") or "").strip() and val:
                        setattr(h, attr, val)
                        filled = True
                for attr, val in (("weight_kg", rec["weight"]), ("bracket", rec["bracket"]),
                                  ("body_weight", rec["bw"])):
                    if not getattr(h, attr, 0) and val:
                        setattr(h, attr, val)
                        filled = True
                if not getattr(h, "body_weight_diff", 0) and rec["bw"] and rec["bwd"] and h.body_weight == rec["bw"]:
                    h.body_weight_diff = rec["bwd"]
                st["entries_filled"] += int(filled)
    except Exception:  # noqa: BLE001 — 補完は best-effort (解析を止めない)
        return st
    return st


def _cli(argv: list[str]) -> int:
    """python -m src.nar_official sync [--since YYYYMM] | sync-odds [--since YYYYMM] | status | info <場> <YYYYMMDD> <R>"""
    if not argv:
        print(_cli.__doc__)
        return 2
    since = argv[argv.index("--since") + 1] if "--since" in argv else None
    if argv[0] == "sync":
        res = sync(since=since, log=print)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if not (res["missing"] or res["failed"]) else 1
    if argv[0] == "sync-odds":
        res = sync_odds(since=since or ODDS_SINCE, log=print)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if not res["missing"] else 1
    if argv[0] == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
        return 0
    if argv[0] == "info" and len(argv) >= 4:
        print(json.dumps(race_info(argv[1], argv[2], int(argv[3])), ensure_ascii=False, indent=1))
        return 0
    print(_cli.__doc__)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
