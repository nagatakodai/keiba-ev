"""JRA 即PAT (IPAT) 自動投票 Playwright scaffold。oddspark_bet.py の JRA 版。

**重要 / 安全方針 (oddspark_bet と同一思想):**
  - 既定は **半自動 (B案)**: 買い目を購入予定リストに積むところまで自動化し、**購入確定ボタンは
    人が headful ブラウザで目視して押す**。`--auto-purchase` で全自動 (実弾) に切替えられるが、
    確認画面の DOM が実機検証されるまで `AUTO_PURCHASE_VERIFIED=False` で **実弾は撃たない**
    (fail-safe)。oddspark とまったく同じ four-gate (per-race 上限 / daily_cap / 検証フラグ /
    success marker 検出後に加算) で守る。
  - 認証は **環境変数のみ** から読む。コード/ログ/コミットには絶対残さない:
        IPAT_INETID      INET-ID (発行された加入者用ネット ID)
        IPAT_SUBSCRIBER  加入者番号 (subscriber number)
        IPAT_PARS        P-ARS番号
        IPAT_PIN         暗証番号 (投票時 PIN)
  - JRA / IPAT の利用規約は自動化を制限している可能性がある。**自己責任**。

**未検証部分 (要実機調整):** ログインの先 (投票画面) の HTML は当方からは不可視。下記
`SELECTORS` / URL / 式別コードは **best-effort の placeholder**。実際に IPAT にログインして
DevTools で確認し、ここだけ直せば動くよう 1 箇所に集約してある。各ステップで
`data/cache/ipat_step_*.png` にスクショを残すのでそれを見て調整する。oddspark_bet が辿った
「placeholder → 実機検証で SELECTORS 確定 → 検証フラグ True」の道を JRA でも踏襲する。

使い方:
  # one-shot (1 レースをカート投入して人が確定):
  IPAT_INETID=... IPAT_SUBSCRIBER=... IPAT_PARS=... IPAT_PIN=... \
    python -m src.ipat_bet <netkeiba_jra_race_id> [--manual-login]

  # 常駐 daemon (watch-auto --bet-ipat と連携):
  python -m src.ipat_bet --session [--auto-login] [--auto-purchase] [--daily-cap=50000]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 認証情報は .env から読む (analyze.py / oddspark_bet と同じ流儀)。OS env が優先。
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

_SHOT_DIR = ROOT / "data" / "cache"
# watch-auto → 常駐 betting セッション の受け渡しキュー (oddspark とは別 namespace)。
# watch-auto が JRA レースの <netkeiba_rid>.req を置き、--session daemon が拾って snapshot の
# 束を購入予定リストに投入する (処理後 .done に rename)。
QUEUE_DIR = ROOT / "data" / "cache" / "ipat_bet_queue"

_BASE = "https://www.ipat.jra.go.jp"
# 即PAT ログイン入口 (公開ページ)。実機 DOM は要確認だが入口 URL は確定。
_LOGIN_URL = f"{_BASE}/"

# ── 認証後 (投票) 画面のセレクタ。**placeholder = 要実機調整** ───────────────────────
# IPAT 通常投票フロー (一般に): ログイン → メニュー → 通常投票 → 開催(場)選択 → R 選択 →
# 式別選択 → 馬番選択 → 金額入力 → セット → 入力終了 → 購入予定リスト → (合計金額/件数を
# 確認用に再入力) → 購入する → 完了。下記は best-effort。実機ログイン後に DevTools で確定する。
SELECTORS = {
    # --- ログイン (2 段: INET-ID 画面 → 加入者番号/P-ARS/暗証番号 画面) ---
    # 段1 INET-ID 画面 (実機 DOM 確認済 2026-05-31): form name=FORM1 onsubmit=fSend()。
    #   <input type="text" name="inetid" maxlength="12"> + ログインは
    #   <a onclick="javascript:send();return false;" title="ログイン"> (submit ボタンではない)。
    #   send() が inetid 検証 → FORM1.submit()。
    "login_inetid": 'input[name="inetid"]',          # 確定 (実機DOM)
    "login_inetid_submit": 'a[title="ログイン"]',    # 確定 (<a onclick=send()>)、JS fallback send()
    # 段2 加入者情報入力 画面 (実機 DOM 確認済 2026-05-31)。フィールド名に注意:
    #   name="i" = 加入者番号(8桁) / name="p" = 暗証番号(password,4桁) / name="r" = P-ARS番号(4桁)。
    #   ログインは <a onclick="ToModernMenu()" title="ネット投票メニューへ"> (submit ボタンではない)。
    #   ToModernMenu() が FORM1.i / FORM2.p / FORM3.r を読んで送信。
    "login_subscriber": 'input[name="i"]',           # 確定: 加入者番号
    "login_pin": 'input[name="p"]',                  # 確定: 暗証番号 (type=password)
    "login_pars": 'input[name="r"]',                 # 確定: P-ARS番号
    "login_submit": 'a[title="ネット投票メニューへ"]',  # 確定 (<a onclick=ToModernMenu()>)
    # ログイン後メニュー (実機 DOM 確認済 2026-05-31)。投票画面は AngularJS SPA
    # (ui-router, hash route #!/...)。ログイン判定はメニューの <a ui-sref="logout">ログアウト</a>。
    "logged_in_marker": "text=ログアウト",            # 確定 (a[ui-sref=logout])
    # --- 通常投票への遷移 (実機 DOM 確認済): <button ui-sref="bet.basic" href="#!/bet/basic"> ---
    # ※ vm.bIsBetInActiveOrZeroAvailableNum で disabled になる (発売前 / 購入可能件数0 / 未入金)。
    #    IPAT は **事前に銀行口座から入金 (チャージ) しないと投票不可** (購入限度額が0だと不可)。
    "normal_vote_link": 'button[ui-sref="bet.basic"]',  # 確定 (通常投票)、text=通常 は fallback
    # --- 開催(場) / レース 選択 ---
    # IPAT は 場名ボタン (例 "東京") + R 番号ボタン。値は場名/数字でマッチさせる想定。
    "venue_button": 'text="{venue}"',                # 要確認 (場名でマッチ)
    "race_button": 'text="{race_no}R"',              # 要確認
    # --- 式別 (bet type) ---
    "bet_type_radio": 'input[name="bet_type"][value="{code}"]',  # 要確認
    # --- 馬番 / 金額 / セット ---
    # 馬番は着順列 (1着/2着/3着) のグリッド。oddspark と同様 pos=1/2/3 で着順列を表す想定。
    "horse_cell": 'td[name="horse{pos}"] a, [data-pos="{pos}"] [data-umaban="{umaban}"]',  # 要確認
    "umaban_reset": 'text=クリア',                   # 各脚前にクリア (累積防止) 要確認
    "amount_input": 'input[name="amount"]',          # 金額 (100円単位 = 1 で 100円の系もある) 要確認
    "set_button": 'text=セット',                     # 買い目を購入予定リストに積む 要確認
    "finish_input": 'text=入力終了',                 # 購入予定リストへ 要確認
    # --- 購入予定リスト (人が目視する画面) ---
    "buylist": '#purchaseList',                      # 要確認
    "delete_all": 'text=全て取消',                   # 開始時クリア用 要確認
    "combo_count_text": 'text=/合計\\s*([0-9,]+)\\s*件/',  # 件数読み取り 要確認
    # --- 全自動 (実弾) のみ: 購入確定 ---
    # IPAT は購入確定前に「合計金額」「合計件数」を確認入力させる二重確認がある (誤発注防止)。
    # placeholder。実機で確定したら confirm_amount/confirm_count/confirm_purchase を直す。
    "confirm_amount": 'input[name="totalAmount"]',   # 要確認 (合計金額の確認入力)
    "confirm_count": 'input[name="totalCount"]',     # 要確認 (合計件数の確認入力)
    "confirm_purchase": 'text=購入する',             # 半自動はここで人が止まる 要確認
    "confirm_final_candidates": [                    # 確認ダイアログ/画面の最終ボタン 要確認
        'text=OK', 'text=購入を確定', 'button#submit',
    ],
    # 購入成功の証跡 (要確認)。複数 marker で OR 判定。
    "purchase_success_markers": ("投票を受け付けました", "受け付けました", "購入完了", "投票完了"),
}

# 当方 bet_type → IPAT 式別コード (要実機調整。表示名は確定)。
_BET_TYPE_CODE = {
    "win": "1",        # 単勝
    "place": "2",      # 複勝
    "quinella": "5",   # 馬連
    "exacta": "6",     # 馬単
    "wide": "7",       # ワイド
    "trio": "8",       # 3連複
    "trifecta": "9",   # 3連単
}
_SHIKIBETSU = {
    "win": "単勝", "place": "複勝", "quinella": "馬連", "wide": "ワイド",
    "exacta": "馬単", "trio": "3連複", "trifecta": "3連単",
}
# 順序付き (馬単/3連単) の自動投入可否。**実機未検証なので False** (fail-safe)。
# IPAT の馬単/3連単 は 1着/2着(/3着) 列に置けば 1 順列のはず。実機で確認したら True に。
_ORDERED_BETS_VERIFIED = False

# 全自動 (購入確定まで自動) の安全フラグ。**実機 DOM 未検証なので False** (fail-safe)。
# oddspark と同じく、確認画面の最終ボタン DOM / 二重確認入力 / success marker を 1 度実機で
# 検証してからフラグを True にする。False の間 `_confirm_purchase` は "skipped" を返し実弾は出ない。
AUTO_PURCHASE_VERIFIED = False

# デイリー上限の既定値 (円)。1 日の累計賭金がここを超えると _confirm_purchase が "skipped"。
# JST 00:00 で counter リセット。oddspark とは別ファイルで独立管理。
DAILY_CAP_DEFAULT = 50_000
DAILY_STAKE_FILE = ROOT / "data" / "cache" / "ipat_daily_stake.json"

# IPAT 投票対象は JRA 10 場のみ (netkeiba venue code 01-10)。
JRA_VENUE_CODES = {"01", "02", "03", "04", "05", "06", "07", "08", "09", "10"}


def _is_jra_rid(netkeiba_rid: str) -> bool:
    """netkeiba 12桁 rid が JRA 開催か (venue code 01-10)。IPAT 投票対象判定。"""
    return (netkeiba_rid.isdigit() and len(netkeiba_rid) == 12
            and netkeiba_rid[4:6] in JRA_VENUE_CODES)


def _jra_venue_name(netkeiba_rid: str) -> str | None:
    """netkeiba rid → JRA 場名 (IPAT の場選択に使う)。JRA でなければ None。"""
    if not _is_jra_rid(netkeiba_rid):
        return None
    from .parse import VENUE_CODE
    return VENUE_CODE.get(netkeiba_rid[4:6])


class IpatBetError(RuntimeError):
    pass


@dataclass
class CartLeg:
    bet_type: str
    key: list[int]
    stake: int   # 円 (100円単位前提)


def _env_any(*names: str) -> str:
    """複数の env 名を順に試し最初に見つかった非空値を返す (.env の別名表記を吸収)。"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


def _creds() -> dict:
    """環境変数 (.env 含む) から認証情報を取得 (無ければエラー)。コードには残さない。

    .env 例:
        IPAT_INETID=...        (INET-ID)
        IPAT_SUBSCRIBER=...    (加入者番号、別名 IPAT_SUBSCRIBER_NO)
        IPAT_PARS=...          (P-ARS番号、別名 IPAT_PARS_NO)
        IPAT_PIN=...           (暗証番号)
    """
    inetid = _env_any("IPAT_INETID", "IPAT_INET_ID")
    sub = _env_any("IPAT_SUBSCRIBER", "IPAT_SUBSCRIBER_NO", "IPAT_KANYUSHA")
    pars = _env_any("IPAT_PARS", "IPAT_PARS_NO", "IPAT_PARS_NUMBER")
    pin = _env_any("IPAT_PIN", "IPAT_PASSWORD")
    missing = [k for k, v in
               (("IPAT_INETID", inetid), ("IPAT_SUBSCRIBER", sub),
                ("IPAT_PARS", pars), ("IPAT_PIN", pin)) if not v]
    if missing:
        raise IpatBetError(
            f"認証情報が不足: {', '.join(missing)} を .env か環境変数で渡してください (コミット禁止)")
    return {"inetid": inetid, "subscriber": sub, "pars": pars, "pin": pin}


def _race_no(netkeiba_rid: str) -> int:
    """netkeiba 12桁 rid の末尾 2桁 = R 番号。"""
    return int(netkeiba_rid[10:12])


def _legs_from_snapshot(netkeiba_rid: str) -> tuple[list[CartLeg], str]:
    """snapshot の recommended_bundle.legs → CartLeg。(legs, race_label) を返す。

    snapshot ファイル名は内部 race_id `<cup>-<si>-<rn>` (odds 源非依存で共通)。netkeiba 12桁 rid →
    内部 race_id 変換は `parse._split_race_id` を使う (oddspark_bet._legs_from_snapshot と同形)。
    """
    from .parse import _split_race_id
    venue, si, rn, cup = _split_race_id(netkeiba_rid)
    rid = f"{cup}-{si}-{rn}"
    path = ROOT / "data" / "predictions" / f"{rid}.json"
    if not path.exists():
        raise IpatBetError(f"snapshot が無い: {path} (先に analyze で生成)")
    snap = json.loads(path.read_text(encoding="utf-8"))
    bundle = snap.get("recommended_bundle") or {}
    legs = [CartLeg(bet_type=l["bet_type"], key=list(l["key"]), stake=int(l.get("stake", 0)))
            for l in (bundle.get("legs") or []) if int(l.get("stake", 0)) > 0]
    if not legs:
        raise IpatBetError("recommended_bundle に脚が無い (見送り or 未生成)")
    return legs, f"{venue} {rn}R"


def _shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(_SHOT_DIR / f"ipat_step_{name}.png"))
    except Exception:  # noqa: BLE001
        pass


# ── daily_cap (JST 日次累計) ───────────────────────────────────────────────────────
def _today_jst() -> str:
    """JST の YYYY-MM-DD。日跨ぎで daily_stake をリセットするためのキー。"""
    import datetime
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(tz=jst).strftime("%Y-%m-%d")


def _load_daily_stake_map() -> dict:
    if not DAILY_STAKE_FILE.exists():
        return {}
    try:
        return json.loads(DAILY_STAKE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_daily_stake_map(d: dict) -> None:
    DAILY_STAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_STAKE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.rename(DAILY_STAKE_FILE)


def get_today_stake() -> int:
    return int(_load_daily_stake_map().get(_today_jst(), 0))


def record_daily_stake(amount: int) -> int:
    today = _today_jst()
    m = _load_daily_stake_map()
    # 当日キーだけ残す (古い日付は掃除)。
    new_total = int(m.get(today, 0)) + int(amount)
    _save_daily_stake_map({today: new_total})
    return new_total


def check_daily_cap(prospective_stake: int, daily_cap: int) -> tuple[bool, str]:
    """この race の stake を加えても daily_cap 以内か。daily_cap<=0 は無効化 (常に allowed)。"""
    if daily_cap <= 0:
        return (True, "daily_cap 無効 (≤0)")
    projected = get_today_stake() + prospective_stake
    if projected > daily_cap:
        return (False,
                f"daily_cap 超過: 本日累計¥{get_today_stake():,} + ¥{prospective_stake:,} "
                f"= ¥{projected:,} > 上限¥{daily_cap:,}")
    return (True, f"daily_cap OK: → ¥{projected:,} / 上限¥{daily_cap:,}")


def safe_dialog_accept(d) -> None:
    """confirm ダイアログを自動承認 (削除確認等)。購入確定は別ロジックが制御する。"""
    try:
        d.accept()
    except Exception:  # noqa: BLE001
        pass


# ── betting session ─────────────────────────────────────────────────────────────────
class BettingSession:
    """常駐 betting セッション。oddspark_bet.BettingSession の IPAT 版。

    起動時に人がログイン (または env 自動ログイン) → 投票画面で待機し、add_race で
    snapshot 束を購入予定リストへ投入。auto_purchase=True かつ検証フラグが True のときのみ
    実際に購入確定する (四段ガード付き)。
    """

    def __init__(self, *, headful: bool = True, manual_login: bool = True,
                 max_total_stake: int = 10_000, login_wait_sec: int = 600,
                 auto_purchase: bool = False, daily_cap: int = DAILY_CAP_DEFAULT,
                 stake_multiplier: float = 1.0):
        self.headful = headful
        self.manual_login = manual_login
        self.max_total_stake = max_total_stake
        self.login_wait_sec = login_wait_sec
        self.auto_purchase = auto_purchase
        self.daily_cap = daily_cap
        self.stake_multiplier = stake_multiplier if stake_multiplier > 0 else 1.0
        self._pw = None
        self.browser = None
        self.page = None
        self._added: set[str] = set()
        self._session_staked = 0   # 本セッション投入累計 (未確定含む)

    def start(self, *, clear_existing: bool = False) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not self.headful)
        self.page = self.browser.new_page()
        self.page.on("dialog", safe_dialog_accept)
        self.page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        if self.manual_login:
            self._wait_manual_login()
        else:
            self._auto_login()
        self._goto_vote_top()
        if clear_existing:
            self._clear_buylist()

    def _auto_login(self) -> None:
        """env 認証で自動ログイン。**実機 DOM 未検証** — 失敗したら manual_login を使うこと。"""
        creds = _creds()
        p = self.page
        try:
            # 段1: INET-ID (送信は <a onclick=send()>。click → 失敗時 JS send() fallback)
            if p.locator(SELECTORS["login_inetid"]).count() > 0:
                p.fill(SELECTORS["login_inetid"], creds["inetid"])
                _shot(p, "login_inetid")
                try:
                    p.click(SELECTORS["login_inetid_submit"], timeout=4000)
                except Exception:  # noqa: BLE001
                    # link click が効かない場合は page の send() を直接呼ぶ (FORM1.submit())
                    p.evaluate("() => (typeof send === 'function') && send()")
                try:
                    p.wait_for_load_state("networkidle", timeout=15000)
                except Exception:  # noqa: BLE001
                    p.wait_for_timeout(2500)
            # 段2: 加入者番号 / P-ARS / 暗証番号
            if p.locator(SELECTORS["login_subscriber"]).count() > 0:
                p.fill(SELECTORS["login_subscriber"], creds["subscriber"])
            if p.locator(SELECTORS["login_pars"]).count() > 0:
                p.fill(SELECTORS["login_pars"], creds["pars"])
            if p.locator(SELECTORS["login_pin"]).count() > 0:
                p.fill(SELECTORS["login_pin"], creds["pin"])
            _shot(p, "login_filled")
            p.click(SELECTORS["login_submit"])
            p.wait_for_timeout(2500)
        except Exception as ex:  # noqa: BLE001
            _shot(p, "login_failed")
            raise IpatBetError(
                f"自動ログインに失敗 (DOM 未検証の可能性大 — --manual-login 推奨): {ex}")
        # ログイン成否を marker で確認
        if p.locator(SELECTORS["logged_in_marker"]).count() == 0:
            _shot(p, "login_no_marker")
            raise IpatBetError(
                "ログイン marker 未検出。SELECTORS['logged_in_marker'] を実機で確認のこと")

    def _wait_manual_login(self) -> None:
        """人が手でログインするのを polling 待機 (背景起動でも input() 非依存)。"""
        print(f"[ipat_bet] ブラウザでログインしてください (最大 {self.login_wait_sec}s 待機)…")
        deadline = time.time() + self.login_wait_sec
        while time.time() < deadline:
            try:
                if self.page.locator(SELECTORS["logged_in_marker"]).count() > 0:
                    print("[ipat_bet] ログイン検出。")
                    return
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(1500)
        raise IpatBetError("ログインが時間内に完了しませんでした")

    def _goto_vote_top(self) -> None:
        """通常投票画面へ。失敗しても致命でないので best-effort。"""
        try:
            link = self.page.locator(SELECTORS["normal_vote_link"])
            if link.count() > 0:
                link.first.click()
                self.page.wait_for_timeout(1500)
            _shot(self.page, "vote_top")
        except Exception as ex:  # noqa: BLE001
            print(f"[ipat_bet] 通常投票画面遷移に失敗 (要 SELECTORS 確認): {ex}")

    def _clear_buylist(self) -> None:
        try:
            da = self.page.locator(SELECTORS["delete_all"])
            if da.count() > 0:
                da.first.click()
                self.page.wait_for_timeout(500)
                print("[ipat_bet] 購入予定リストを全削除しました")
        except Exception:  # noqa: BLE001
            pass

    def add_race(self, netkeiba_rid: str, legs: list[CartLeg],
                 label: str = "") -> tuple[str, int]:
        """1 レースの束を購入予定リストへ投入。戻り値 (status, ok点数)。status: ok/dup。"""
        if netkeiba_rid in self._added:
            return ("dup", 0)
        if not _is_jra_rid(netkeiba_rid):
            raise IpatBetError(f"JRA レースではない (IPAT 投票対象外): {netkeiba_rid}")
        if self.stake_multiplier != 1.0:
            legs = _apply_stake_multiplier(legs, self.stake_multiplier)
        total = sum(l.stake for l in legs)
        if total > self.max_total_stake:
            raise IpatBetError(
                f"レース合計 ¥{total:,} > 上限 ¥{self.max_total_stake:,} — 投入しない (誤入力防止)")
        venue = _jra_venue_name(netkeiba_rid)
        rno = _race_no(netkeiba_rid)
        ok = 0
        staked = 0
        for i, leg in enumerate(legs, 1):
            try:
                _add_leg_to_buylist(self.page, leg, venue, rno)
                print(f"  + [{label or netkeiba_rid}] {leg.bet_type} "
                      f"{'-'.join(map(str, leg.key))} ¥{leg.stake:,}")
                ok += 1
                staked += leg.stake
            except Exception as ex:  # noqa: BLE001
                _shot(self.page, f"bet_{netkeiba_rid}_{i}_FAILED")
                print(f"  ! [{label or netkeiba_rid}] 脚{i} "
                      f"({leg.bet_type} {leg.key}) スキップ: {ex}")
        # 入力終了 → 購入予定リスト
        try:
            fin = self.page.locator(SELECTORS["finish_input"])
            if fin.count() > 0:
                fin.first.click()
                self.page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass
        _shot(self.page, f"bet_{netkeiba_rid}_filled")
        self._added.add(netkeiba_rid)
        self._session_staked += staked
        if self.auto_purchase and staked > 0:
            p_status, p_msg = self._confirm_purchase(staked)
            tag = "[magenta]" if p_status == "ok" else "[yellow]" if p_status == "skipped" else "[red]"
            print(f"  {tag}→ 自動購入 {p_status}:[/] {p_msg}")
            mode_note = f"自動購入 ({p_status})"
        else:
            mode_note = "**購入確定は人が押す**"
        print(f"[ipat_bet] {label or netkeiba_rid}: {ok}/{len(legs)} 点 投入。{mode_note} "
              f"(本セッション投入累計 ¥{self._session_staked:,} 未確定含む)")
        if not self.auto_purchase and self._session_staked > self.max_total_stake:
            print(f"[ipat_bet] ⚠ 累計 ¥{self._session_staked:,} が per-race 上限 "
                  f"¥{self.max_total_stake:,} 超。**購入確定は購入予定リスト全体を一括購入する**ので、"
                  "レースごとに確定/クリアして溜め過ぎに注意。")
        return ("ok", ok)

    def _confirm_purchase(self, race_stake: int) -> tuple[str, str]:
        """購入予定リストを 購入する → 二重確認 → 確定 で **実際に購入** する。**実弾**。

        戻り値 (status, message)。status: ok / skipped / failed。
        四段の安全:
        ① auto_purchase=False or AUTO_PURCHASE_VERIFIED=False → skipped (実弾撃たない)
        ② daily_cap 超過 → skipped (リスト残し人が判断)
        ③ 確定ボタンを候補から検出して click + (合計金額/件数の二重確認入力)
        ④ success marker 検出時のみ daily_stake を加算 (失敗で二重購入しない)
        """
        if not self.auto_purchase:
            return ("skipped", "auto_purchase=False")
        if not AUTO_PURCHASE_VERIFIED:
            return ("skipped",
                    "AUTO_PURCHASE_VERIFIED=False (確認画面 DOM 未検証なので fail-safe で実弾停止)")
        allowed, msg = check_daily_cap(race_stake, self.daily_cap)
        if not allowed:
            return ("skipped", msg)
        p = self.page
        # 1) IPAT の購入確認の二重入力 (合計金額/件数) があれば埋める
        try:
            if p.locator(SELECTORS["confirm_amount"]).count() > 0:
                p.fill(SELECTORS["confirm_amount"], str(race_stake))
            # 件数は厳密に算出できないと危険 → 入力欄があっても自動では空に留め、人手前提。
            # (誤った件数で確定が通ると過剰購入のため、実機検証時に件数計算を確定する)
        except Exception:  # noqa: BLE001
            pass
        # 2) 購入する
        try:
            p.click(SELECTORS["confirm_purchase"])
            p.wait_for_timeout(2500)
        except Exception as ex:  # noqa: BLE001
            _shot(p, "purchase_gotobuy_failed")
            return ("failed", f"購入するボタン click 失敗: {ex}")
        _shot(p, "purchase_review")
        # 3) 最終確定ボタン (候補を順に試す)
        clicked = None
        for sel in SELECTORS["confirm_final_candidates"]:
            try:
                loc = p.locator(sel)
                if loc.count() > 0:
                    loc.first.click()
                    clicked = sel
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            _shot(p, "purchase_review_no_button")
            return ("failed", "確定ボタンが見つからない (DOM 未検証 — purchase_review screenshot 参照)")
        p.wait_for_timeout(3000)
        _shot(p, "purchase_after_click")
        # 4) success marker 検出
        success = False
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                body = p.evaluate("() => document.body ? document.body.innerText || '' : ''")
                if any(m in body for m in SELECTORS["purchase_success_markers"]):
                    success = True
                    break
            except Exception:  # noqa: BLE001
                pass
            p.wait_for_timeout(500)
        if not success:
            return ("failed", "success marker 未検出 — ブラウザで購入状況を目視確認推奨")
        new_total = record_daily_stake(race_stake)
        return ("ok", f"購入完了 ¥{race_stake:,} (clicked={clicked}, 本日累計 ¥{new_total:,})")

    def close(self) -> None:
        for fn in (lambda: self.browser and self.browser.close(),
                   lambda: self._pw and self._pw.stop()):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def _apply_stake_multiplier(legs: list[CartLeg], multiplier: float) -> list[CartLeg]:
    """leg.stake を multiplier 倍して 100 円単位に丸める (最低 ¥100)。"""
    if multiplier == 1.0:
        return legs
    out = []
    for l in legs:
        amt = max(100, int(round(l.stake * multiplier / 100.0)) * 100)
        out.append(CartLeg(bet_type=l.bet_type, key=l.key, stake=amt))
    return out


def _reset_umaban(page) -> None:
    """各脚の前に馬番選択をクリア (累積/トグル防止)。oddspark の教訓を IPAT でも適用。"""
    try:
        r = page.locator(SELECTORS["umaban_reset"])
        if r.count() > 0:
            r.first.click()
            page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass


def _add_leg_to_buylist(page, leg: CartLeg, venue: str | None, race_no: int) -> None:
    """1 脚を IPAT 通常投票フローで購入予定リストに積む。**SELECTORS placeholder = 要実機調整**。

    順序付き (馬単/3連単) は _ORDERED_BETS_VERIFIED=False の間は中止 (誤発注防止)。
    """
    if leg.bet_type in ("exacta", "trifecta") and not _ORDERED_BETS_VERIFIED:
        raise IpatBetError(
            f"{leg.bet_type} は順序付きだが _ORDERED_BETS_VERIFIED=False (実機検証まで中止)")
    code = _BET_TYPE_CODE.get(leg.bet_type)
    if code is None:
        raise IpatBetError(f"未対応 bet_type: {leg.bet_type}")
    # 場 / R 選択
    if venue:
        try:
            vb = page.locator(SELECTORS["venue_button"].format(venue=venue))
            if vb.count() > 0:
                vb.first.click()
                page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
    try:
        rb = page.locator(SELECTORS["race_button"].format(race_no=race_no))
        if rb.count() > 0:
            rb.first.click()
            page.wait_for_timeout(400)
    except Exception:  # noqa: BLE001
        pass
    # 式別
    try:
        bt = page.locator(SELECTORS["bet_type_radio"].format(code=code))
        if bt.count() > 0:
            bt.first.check()
            page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass
    # 馬番 (順不同は着順列に各1頭ずつ = 1組、順序付きは key 順に 1/2/3着列)
    _reset_umaban(page)
    ordered = leg.bet_type in ("exacta", "trifecta")
    for idx, umaban in enumerate(leg.key, 1):
        pos = idx if ordered else 1  # 順不同は全部 1着列扱い (1組), 順序付きは 1/2/3着列
        sel = SELECTORS["horse_cell"].format(pos=pos, umaban=umaban)
        cell = page.locator(sel)
        if cell.count() == 0:
            raise IpatBetError(f"馬番セル不検出 (pos={pos} umaban={umaban}) — SELECTORS 要確認")
        cell.first.click()
        page.wait_for_timeout(200)
    # 金額 (IPAT は 100円単位。"amount" の単位が円か100円かは実機で確認)
    page.fill(SELECTORS["amount_input"], str(leg.stake // 100))
    page.wait_for_timeout(200)
    # セット
    page.click(SELECTORS["set_button"])
    page.wait_for_timeout(500)


# ── one-shot ─────────────────────────────────────────────────────────────────────────
def fill_cart(netkeiba_rid: str, legs: list[CartLeg], *,
              headful: bool = True, manual_login: bool = True,
              max_total_stake: int = 10_000) -> None:
    """1 レースをカート投入して人が確定するまでブラウザを開いたまま待機 (one-shot)。"""
    total = sum(l.stake for l in legs)
    if total > max_total_stake:
        raise IpatBetError(
            f"合計賭金 ¥{total:,} が上限 ¥{max_total_stake:,} を超過 — 中止 (誤入力防止)")
    venue = _jra_venue_name(netkeiba_rid) or "?"
    rno = _race_no(netkeiba_rid)
    print(f"[ipat_bet] {venue} {rno}R / {len(legs)}点 合計¥{total:,} をカート投入します。")
    sess = BettingSession(headful=headful, manual_login=manual_login,
                          max_total_stake=max_total_stake, auto_purchase=False)
    try:
        sess.start()
        sess.add_race(netkeiba_rid, legs, label=f"{venue} {rno}R")
        print("[ipat_bet] 投入完了。**購入確定は人が目視で押してください**。Enter で終了…")
        try:
            input()
        except EOFError:
            pass
    finally:
        sess.close()


# ── 常駐 daemon ───────────────────────────────────────────────────────────────────
def run_session(*, headful: bool = True, manual_login: bool = True,
                max_total_stake: int = 10_000, poll_sec: int = 5,
                clear_existing: bool = False,
                auto_purchase: bool = False, daily_cap: int = DAILY_CAP_DEFAULT,
                stake_multiplier: float = 1.0) -> None:
    """常駐 betting セッション: 起動時にログイン → queue を監視し、watch-auto が積んだ
    JRA レースの <netkeiba_rid>.req の snapshot 束を同じブラウザに投入し続ける。Ctrl-C 終了。
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    sess = BettingSession(headful=headful, manual_login=manual_login,
                          max_total_stake=max_total_stake,
                          auto_purchase=auto_purchase, daily_cap=daily_cap,
                          stake_multiplier=stake_multiplier)
    mode = "**自動購入 (実弾)**" if auto_purchase else "半自動 (人が確定)"
    print(f"[ipat_bet] 常駐セッション開始 ({mode})。")
    if auto_purchase:
        print(f"[ipat_bet] daily_cap: 本日累計 ¥{get_today_stake():,} / 上限 ¥{daily_cap:,}")
        if not AUTO_PURCHASE_VERIFIED:
            print("[ipat_bet] ⚠ AUTO_PURCHASE_VERIFIED=False — 確認画面 DOM 未検証なので "
                  "実弾は撃たれません (fail-safe)。1 度実機で confirm 画面を検証してフラグを True に。")
    try:
        sess.start(clear_existing=clear_existing)
    except Exception as ex:  # noqa: BLE001
        print(f"[ipat_bet] セッション開始失敗: {ex}")
        sess.close()
        return
    print(f"[ipat_bet] ログイン完了。queue 監視開始: {QUEUE_DIR}")
    print("[ipat_bet] watch-auto を --bet-ipat で回すと発走前 JRA レースが積まれます。")
    if not auto_purchase:
        print("[ipat_bet] **購入確定は常に人が目視で押します** (自動では絶対に押しません)。Ctrl-C で終了。")
    attempts: dict = {}
    try:
        while True:
            _process_bet_queue_once(sess, attempts)
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("\n[ipat_bet] 終了 (Ctrl-C)。ブラウザを閉じます。")
    finally:
        sess.close()


def _process_bet_queue_once(sess, attempts: dict, max_attempts: int = 3) -> None:
    """queue を1巡: 各 <rid>.req の snapshot 束を投入し、処理確定なら .done に rename。"""
    for req in sorted(QUEUE_DIR.glob("*.req")):
        rid = req.stem
        terminal = True
        try:
            legs, label = _legs_from_snapshot(rid)
            status, _ok = sess.add_race(rid, legs, label=label)
            if status == "dup":
                print(f"[ipat_bet] {rid} は投入済 (skip)")
        except IpatBetError as ex:
            print(f"[ipat_bet] {rid} skip: {ex}")
        except Exception as ex:  # noqa: BLE001 — ブラウザ/通信 glitch は一過性
            attempts[rid] = attempts.get(rid, 0) + 1
            terminal = attempts[rid] >= max_attempts
            note = "上限到達→打ち切り" if terminal else f"再試行 {attempts[rid]}/{max_attempts}"
            print(f"[ipat_bet] {rid} 失敗 ({note}): {ex}")
        if terminal:
            try:
                req.rename(req.with_suffix(".done"))
            except Exception:  # noqa: BLE001
                pass


def _to_netkeiba_rid(arg: str) -> str:
    """入力を netkeiba rid (12桁) に正規化。内部 race_id <cup10>-<si>-<rn> も受ける。"""
    a = arg.strip()
    if re.fullmatch(r"\d{12}", a):
        return a
    m = re.fullmatch(r"(\d{10})-(\d+)-(\d+)", a)
    if m:
        cup, _si, rn = m.group(1), m.group(2), int(m.group(3))
        return f"{cup}{rn:02d}"
    raise IpatBetError(
        f"race_id 形式不正: {arg!r} (netkeiba rid 12桁 か 内部 race_id <cup>-<si>-<rn>)")


def _main() -> None:
    argv = sys.argv[1:]
    if "--session" in argv:
        poll = 5
        daily_cap = DAILY_CAP_DEFAULT
        stake_multiplier = 1.0
        for a in argv:
            if a.startswith("--poll="):
                try:
                    poll = max(1, int(a.split("=", 1)[1]))
                except ValueError:
                    pass
            elif a.startswith("--daily-cap="):
                try:
                    daily_cap = max(0, int(a.split("=", 1)[1]))
                except ValueError:
                    pass
            elif a.startswith("--stake-multiplier="):
                try:
                    v = float(a.split("=", 1)[1])
                    stake_multiplier = v if 0 < v <= 100 else 1.0
                except ValueError:
                    pass
        run_session(
            headful="--headless" not in argv,
            auto_purchase="--auto-purchase" in argv,
            daily_cap=daily_cap,
            stake_multiplier=stake_multiplier,
            manual_login="--auto-login" not in argv,
            poll_sec=poll,
            clear_existing="--clear" in argv,
        )
        return
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print("usage:\n"
              "  one-shot: python -m src.ipat_bet <netkeiba_jra_race_id|race_id> [--manual-login]\n"
              "  常駐    : python -m src.ipat_bet --session [--auto-login] [--auto-purchase] "
              "[--poll=5] [--clear] [--daily-cap=50000]")
        raise SystemExit(2)
    try:
        rid = _to_netkeiba_rid(args[0])
        if not _is_jra_rid(rid):
            raise IpatBetError(f"JRA レースではありません (IPAT 投票対象外): {rid}")
        legs, label = _legs_from_snapshot(rid)
        print(f"[ipat_bet] {label}: snapshot から {len(legs)} 点")
        fill_cart(rid, legs,
                  headful="--headless" not in sys.argv,
                  manual_login="--manual-login" in sys.argv)
    except IpatBetError as ex:
        print(f"[ipat_bet] {ex}")
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
