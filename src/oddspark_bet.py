"""オッズパーク 半自動投票 (B案: カート投入まで自動・購入確定は人) の Playwright scaffold。

**重要 / 安全方針:**
  - これは「買い目をカートに入れる」までを自動化し、**購入確定ボタンは絶対に押さない**。
    最終確定は必ず人間が headful ブラウザで目視して行う (誤発注の最終ゲート)。
  - ログイン情報は **環境変数のみ** から読む。コード/ログ/コミットには絶対残さない:
        ODDSPARK_ID        オッズパーク会員ID (加入者番号)
        ODDSPARK_PASSWORD  パスワード
        ODDSPARK_PIN       投票暗証番号 (要る場合のみ)
  - オッズパークの利用規約は自動化を制限している可能性がある。**自己責任**で、
    あくまで「カート投入支援 + 人が確定」の半自動に留める。

**未検証部分 (要実機調整):** 認証後 (投票) 画面の HTML はログインの先で当方からは
不可視。下記 `SELECTORS` / URL / 式別コードは **best-effort の placeholder**。実際に
ログインして DevTools で確認し、ここだけ直せば動くよう 1 箇所に集約してある。
各ステップで data/cache/oddspark_step_*.png にスクショを残すのでそれを見て調整する。

使い方:
  ODDSPARK_ID=... ODDSPARK_PASSWORD=... python -m src.oddspark_bet <netkeiba_nar_rid>
  → 該当 race の snapshot (recommended_bundle.legs) をカート投入手前まで入力し、
    headful ブラウザを開いたまま待機 (人が内容を確認して確定)。
"""
from __future__ import annotations

import gzip  # noqa: F401  (将来 cache 参照用)
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SHOT_DIR = ROOT / "data" / "cache"
# watch-auto → 常駐 betting セッション の受け渡しキュー。watch-auto が <netkeiba_rid>.req を
# 置き、--session daemon が拾って snapshot の束をカート投入する (処理後 .done に rename)。
QUEUE_DIR = ROOT / "data" / "cache" / "oddspark_bet_queue"

_BASE = "https://www.oddspark.com"
# ログイン: 実機HTML確認済 (2026-05)。フォーム name=loginForm、隠し _csrf/SSO_* は
# フォーム送信で自動付与。送信は <a id="btn-login" href="javascript:formSubmit()"> をclick。
_LOGIN_URL = f"{_BASE}/user/my/Index.do"
# 投票エントリ (マイページの「投票する」リンク先, 実機確認済)。
_VOTE_TOP_URL = f"{_BASE}/keiba/auth/VoteKeibaTop.do?gamenId=P901&gamenKoumokuId=topVote"
# レースまとめ投票画面の要素 (実機HTML確認済 2026-05)。login/賭式/レース選択/金額/セットは
# 確定。**馬番セルのクリック (着順列の意味) だけ bet type により挙動が異なり要目視検証**。
SELECTORS = {
    "login_id": 'input[name="SSO_ACCOUNTID"]',       # 確定
    "login_password": 'input[name="SSO_PASSWORD"]',  # 確定
    "login_submit": '#btn-login',                    # 確定 (JSリンク formSubmit())
    "logged_in_marker": "text=ログアウト",            # 確定 (マイページに存在)
    "matome_link": '#todayMultiRace',                # 確定 (レースまとめ投票へ)
    # まとめ投票画面 (確定):
    "race_checkbox": 'input[type="checkbox"][value="{race_val}"]',  # value=kaisaiBi_joCode_raceNo
    "bet_type_checkbox": 'input[name="betTypeSelect"][value="{code}"]',  # 1-9
    "amount_input": '#textfield11',                  # 金額 (100円単位 / "00円" 接尾)
    "set_button": '#multiSet',                       # セット (買い目を #buylist に積む)
    "payment_opcoin": '#paymentMethodOpCoin',        # 支払=OPコイン (口座は OPコイン残)
    "buylist": '#buylist',                           # 積まれた買い目一覧 (人が目視)
    "delete_selected": '#choice a',                  # 選択項目削除 (誤りの訂正)
    "delete_all": '#all a',                          # 全買い目削除 (開始時クリア用)
    "horse_cell": '#horseArea td[name="horse{pos}"] a',  # pos=1/2/3 (着順列), text=馬番
    # 馬番グリッドのリセット (各脚の前に押して累積/トグルを防ぐ)。実機 DOM 確認済:
    # <a id="reset">リセット</a> (div.control 内なので 馬番/枠番 のみクリア)。
    "umaban_reset": '#reset',
    # 購入手続は **絶対に押さない** (人が #buylist 確認後に押す)。
    "confirm_purchase": '#gotobuy',
}
# 当方 bet_type → オッズパーク betType コード (betTypeSelect value, 実機確認済)。
_BET_TYPE_CODE = {
    "win": "1", "place": "2", "quinella": "5", "exacta": "6",
    "wide": "7", "trio": "8", "trifecta": "9",
}
# 当方 bet_type → オッズパーク式別の表示値/コード (要確認)。
_SHIKIBETSU = {
    "win": "単勝", "place": "複勝", "quinella": "馬連", "wide": "ワイド",
    "exacta": "馬単", "trio": "3連複", "trifecta": "3連単",
}
# 投票システムの joCode (VoteKeibaTop の #joListHidden, 実機確認済)。
# **オッズ取得側 (scrape_oddspark の opTrackCd) とは別 namespace** なので場名で対応づける。
VOTE_JO_CODE = {
    "門別": "06", "帯広": "03", "盛岡": "11", "水沢": "12",
    "浦和": "31", "船橋": "32", "大井": "33", "川崎": "34",
    "金沢": "41", "笠松": "42", "名古屋": "43", "園田": "51",
    "姫路": "52", "高知": "55", "佐賀": "61",
    # JRA (オッズパークは通常 NAR のみだが list に存在): 札幌04 函館91 福島92 新潟93
    # 東京94 中山95 京都96 阪神97 小倉98 中京44
}
# ↑↑↑ ここまで要実機調整 (joCode/login は確定、bet 入力は placeholder) ↑↑↑


def _vote_jo_code(netkeiba_rid: str) -> str | None:
    """netkeiba race_id → 場名 (VENUE_CODE) → 投票 joCode。"""
    from .parse import VENUE_CODE
    return VOTE_JO_CODE.get(VENUE_CODE.get(netkeiba_rid[4:6], ""))


class OddsparkBetError(RuntimeError):
    pass


@dataclass
class CartLeg:
    bet_type: str
    key: list[int]
    stake: int   # 円 (100円単位前提)


def _creds() -> dict:
    """環境変数から認証情報を取得 (無ければエラー)。コードには残さない。"""
    cid = os.environ.get("ODDSPARK_ID")
    pw = os.environ.get("ODDSPARK_PASSWORD")
    if not cid or not pw:
        raise OddsparkBetError(
            "ODDSPARK_ID / ODDSPARK_PASSWORD を環境変数で渡してください (コミット禁止)")
    return {"id": cid, "password": pw, "pin": os.environ.get("ODDSPARK_PIN", "")}


def _legs_from_snapshot(netkeiba_rid: str) -> tuple[list[CartLeg], str]:
    """snapshot の recommended_bundle.legs → CartLeg。(legs, race_label) を返す。"""
    from .parse import _split_race_id
    venue, si, rn, cup = _split_race_id(netkeiba_rid)
    rid = f"{cup}-{si}-{rn}"
    path = ROOT / "data" / "predictions" / f"{rid}.json"
    if not path.exists():
        raise OddsparkBetError(f"snapshot が無い: {path} (先に analyze で生成)")
    snap = json.loads(path.read_text(encoding="utf-8"))
    bundle = snap.get("recommended_bundle") or {}
    legs = [CartLeg(bet_type=l["bet_type"], key=list(l["key"]), stake=int(l.get("stake", 0)))
            for l in (bundle.get("legs") or []) if int(l.get("stake", 0)) > 0]
    if not legs:
        raise OddsparkBetError("recommended_bundle に脚が無い (見送り or 未生成)")
    return legs, f"{venue} {rn}R"


def _shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(_SHOT_DIR / f"oddspark_step_{name}.png"))
    except Exception:  # noqa: BLE001
        pass


def fill_cart(
    netkeiba_rid: str,
    legs: list[CartLeg],
    *,
    headful: bool = True,
    manual_login: bool = False,
    max_total_stake: int = 10_000,
) -> None:
    """ログイン → 対象 race の投票 → 各買い目をカート投入手前まで。**確定は押さない**。

    headful=True で人が目視確認できるよう可視ブラウザで起動し、入力後も開いたまま待機。
    manual_login=True なら認証情報を使わず人がログインを手で済ませる (最も安全)。
    max_total_stake で合計賭金にハードリミット (誤入力の暴走防止)。
    """
    from .scrape_oddspark import find_oddspark_race

    total = sum(l.stake for l in legs)
    if total > max_total_stake:
        raise OddsparkBetError(
            f"合計賭金 ¥{total:,} が上限 ¥{max_total_stake:,} を超過 — 中止 (誤入力防止)")

    loc = find_oddspark_race(netkeiba_rid)
    if loc is None:
        raise OddsparkBetError(f"オッズパークで {netkeiba_rid} の開催が見つからない")

    _SHOT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[oddspark_bet] {loc.venue} {loc.race_nb}R / {len(legs)}点 合計¥{total:,} "
          f"→ カート投入手前まで (確定は人)")

    sess = BettingSession(headful=headful, manual_login=manual_login,
                          max_total_stake=max_total_stake)
    try:
        sess.start(clear_existing=True)
        sess.add_race(netkeiba_rid, legs, label=f"{loc.venue} {loc.race_nb}R")
        _shot(sess.page, "5_cart_filled")
        print("[oddspark_bet] **購入確定は押していません**。右の買い目一覧 (#buylist) を\n"
              "  目視確認 (組数/組番/金額・順不同の馬番) してから人が確定。")
    except Exception as ex:  # noqa: BLE001 — ブラウザは残して調査可能に
        if sess.page is not None:
            _shot(sess.page, "error")
        print(f"[oddspark_bet] 中断: {ex}")
    finally:
        print("[oddspark_bet] ブラウザは開いたまま。確認後 Enter で閉じる。")
        try:
            input()
        except Exception:  # noqa: BLE001
            pass
        sess.close()


def _login(page, creds: dict) -> None:
    """ログイン (login セレクタは実機確認済)。送信は JS formSubmit() なので #btn-login click。"""
    page.goto(_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    try:
        page.fill(SELECTORS["login_id"], creds["id"])
        page.fill(SELECTORS["login_password"], creds["password"])
        page.click(SELECTORS["login_submit"])   # <a id=btn-login> → formSubmit() → LoginPc.jsp
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(3000)
    except Exception as ex:  # noqa: BLE001
        _shot(page, "login_failed")
        raise OddsparkBetError(
            f"ログイン送信に失敗 (login セレクタ確認): {ex}") from ex
    # ログイン成否判定: ログインフォームがまだ見える = 失敗 (ID/PW 誤り or ロック)
    if page.locator(SELECTORS["login_id"]).count() > 0:
        _shot(page, "login_failed")
        raise OddsparkBetError(
            "ログイン後もログインフォームが残存 — ID/PW 誤り・ロック・要セレクタ確認")


def _select_payment_opcoin(page) -> None:
    """支払方法を OPコインに (口座は OPコイン残)。一度だけ呼ぶ。"""
    try:
        page.check(SELECTORS["payment_opcoin"])
    except Exception:  # noqa: BLE001
        pass   # 既選択/不可視でも続行 (人が最終確認)


def _reset_umaban(page) -> None:
    """馬番グリッドの選択をクリア (**各脚の前に必須**)。

    まとめ画面のグリッドは セット しても 馬番選択が残るため、リセットしないと次脚の
    click が累積し、共有馬番の再 click はトグル OFF になって組番が壊れる
    (実機: 馬連が「フォーメーション3通り」化、ワイドが重複/欠落)。
    id 不明なので候補を順に試す (実機確認後 SELECTORS['umaban_reset'] を確定)。
    """
    for sel in (SELECTORS.get("umaban_reset"),
                'input[type="button"][value="リセット"]', 'input[value="リセット"]',
                'button:has-text("リセット")', 'a:has-text("リセット")',
                '#horseArea ~ * :text-is("リセット")', ':text-is("リセット")'):
        if not sel:
            continue
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                page.wait_for_timeout(200)
                return
        except Exception:  # noqa: BLE001
            continue
    # フォールバック: 選択済セル (class に on/selected 等) を JS で全 click 解除
    try:
        page.evaluate(
            "document.querySelectorAll('#horseArea td a.on, #horseArea td a.selected,"
            " #horseArea td.on a, #horseArea td.selected a').forEach(function(a){a.click();});")
    except Exception:  # noqa: BLE001
        pass


def _select_umaban(page, leg: CartLeg) -> None:
    """馬番選択: 着順列 (horse1=1着, horse2=2着, horse3=3着) の該当馬番セルを click。

    単勝/複勝(1頭)=1着列のみ。馬単/3連単(順序)=key 順に 1/2/3着列。
    馬連/ワイド/3連複 (順不同) は key を先頭列から順に置く (1 組 = 各列1頭ずつ)。
    **呼び出し前に _reset_umaban で必ずグリッドをクリアすること** (累積防止)。
    """
    import re as _re
    for i, num in enumerate(leg.key):
        pos = min(i + 1, 3)
        cell = page.locator(SELECTORS["horse_cell"].format(pos=pos),
                            has_text=_re.compile(rf"^\s*{num}\s*$"))
        if cell.count() == 0:
            raise OddsparkBetError(f"馬番セル不在: horse{pos}={num} (頭数/締切確認)")
        cell.first.click()
        page.wait_for_timeout(200)
    # 検証: 選択済セル (a.on) 数が馬番数と一致するか (リセット漏れ/クリック漏れ/
    # 共有馬番のトグル OFF を捕捉)。一致しないと セット で組番が壊れるので中止。
    try:
        on = page.locator("#horseArea td a.on").count()
    except Exception:  # noqa: BLE001
        on = -1
    if on >= 0 and on != len(leg.key):
        raise OddsparkBetError(
            f"馬番選択数 不一致: 選択 {on} 頭 ≠ 期待 {len(leg.key)} 頭 "
            f"({leg.bet_type} {leg.key}) — リセット/トグル要確認")


def _add_leg_to_cart(page, leg: CartLeg, race_val: str) -> None:
    """1 買い目を「まとめ投票」画面でセット (#buylist へ)。**確定は押さない**。

    順序 (実機準拠): 金額入力 → レースチェック → 賭式選択 → 馬番選択 → セット。
    """
    import re as _re
    try:
        # 1) 金額 (100円単位。"00円" 接尾なので stake//100 を入力)
        page.fill(SELECTORS["amount_input"], str(leg.stake // 100))
        # 2) レースチェック (value = kaisaiBi_joCode_raceNo)
        cb = page.locator(SELECTORS["race_checkbox"].format(race_val=race_val))
        if cb.count() == 0:
            raise OddsparkBetError(f"レース checkbox 不在: {race_val} (締切/未発売?)")
        if not cb.first.is_checked():
            cb.first.check()
        # 3) 賭式選択 (他の betType を外して当該のみ)
        page.evaluate(
            "document.querySelectorAll('input[name=betTypeSelect]:checked')"
            ".forEach(function(c){c.checked=false;});")
        page.check(SELECTORS["bet_type_checkbox"].format(code=_BET_TYPE_CODE[leg.bet_type]))
        # 4) 馬番選択 — **前脚の選択が残るので必ずリセットしてから** (累積/トグル防止)
        _reset_umaban(page)
        _select_umaban(page, leg)
        # 5) セット (#buylist へ積む)
        page.click(SELECTORS["set_button"])
        page.wait_for_timeout(1000)
    except OddsparkBetError:
        raise
    except Exception as ex:  # noqa: BLE001
        _shot(page, f"addleg_failed_{leg.bet_type}")
        raise OddsparkBetError(
            f"買い目セット失敗 ({leg.bet_type} {leg.key}) — SELECTORS/馬番選択を実機調整: {ex}"
        ) from ex


def _race_meta(netkeiba_rid: str) -> str:
    """netkeiba rid → レース選択 checkbox value (YYYYMMDD_joCode_raceNo非ゼロ詰め)。"""
    if not (netkeiba_rid.isdigit() and len(netkeiba_rid) == 12):
        raise OddsparkBetError(f"netkeiba rid 形式不正 (12桁数字でない): {netkeiba_rid!r}")
    jo = _vote_jo_code(netkeiba_rid)
    if not jo:
        raise OddsparkBetError(f"投票 joCode 不明 (場名未対応/JRA): {netkeiba_rid}")
    kaisai_bi = netkeiba_rid[:4] + netkeiba_rid[6:8] + netkeiba_rid[8:10]   # YYYYMMDD
    race_no = str(int(netkeiba_rid[10:12]))                                # 非ゼロ詰め
    return f"{kaisai_bi}_{jo}_{race_no}"


def _select_only_race(page, race_val: str) -> None:
    """対象レースのみチェック。**他レースのチェックは外す** (複数レース同時セットの誤発注防止)。

    まとめ画面は当日全レースの checkbox を持つので、レースをまたいでカート投入するとき
    前レースのチェックが残ると セット が両方に効いてしまう。対象以外を uncheck してから
    対象を check する (どちらも実 click で oddspark の内部状態を更新)。
    """
    boxes = page.locator('input[type="checkbox"]')
    try:
        n = boxes.count()
    except Exception:  # noqa: BLE001
        n = 0
    for i in range(n):
        b = boxes.nth(i)
        try:
            val = b.get_attribute("value") or ""
        except Exception:  # noqa: BLE001
            continue
        if re.fullmatch(r"\d{8}_\d+_\d+", val) and val != race_val:
            try:
                if b.is_checked():
                    b.uncheck()
            except Exception:  # noqa: BLE001
                pass
    tgt = page.locator(SELECTORS["race_checkbox"].format(race_val=race_val))
    if tgt.count() == 0:
        raise OddsparkBetError(f"レース checkbox 不在: {race_val} (締切/未発売?)")
    if not tgt.first.is_checked():
        tgt.first.check()


class BettingSession:
    """ログイン済みの持続ブラウザ。`start()` で1回ログイン (人手) → まとめ画面で待機し、
    `add_race()` を呼ぶたびに当該レースの束をカート投入する。**購入確定は決して押さない**。

    watch-auto の常駐連携 (`run_session`) と one-shot (`fill_cart`) で共用する。
    """

    def __init__(self, *, headful: bool = True, manual_login: bool = True,
                 max_total_stake: int = 10_000) -> None:
        self.headful = headful
        self.manual_login = manual_login
        self.max_total_stake = max_total_stake
        self.page = None
        self._pw = None
        self.browser = None
        self.ctx = None
        self._added: set[str] = set()   # 投入済 netkeiba_rid (二重投入防止)

    def start(self, *, clear_existing: bool = False) -> None:
        from playwright.sync_api import sync_playwright
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=not self.headful, args=["--no-sandbox", "--disable-dev-shm-usage"])
        self.ctx = self.browser.new_context(
            locale="ja-JP", viewport={"width": 1280, "height": 1800})
        self.page = self.ctx.new_page()
        # 削除/セット時の confirm() を自動承認 (購入確定 #gotobuy は押さないので安全)。
        self.page.on("dialog", lambda d: d.accept())
        # 1) ログイン
        if self.manual_login:
            self.page.goto(_BASE, wait_until="domcontentloaded")
            print("[oddspark_bet] headful ブラウザでログインを済ませて Enter ...")
            input()
        else:
            _login(self.page, _creds())
        _shot(self.page, "1_after_login")
        # 2) まとめ投票画面へ + 支払 OPコイン
        self._goto_matome()
        _shot(self.page, "3_matome")
        if clear_existing:
            try:
                da = self.page.locator(SELECTORS["delete_all"])
                if da.count() > 0:
                    da.first.click()
                    self.page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                pass

    def _goto_matome(self) -> None:
        self.page.goto(_VOTE_TOP_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        # セッション切れ検出: 投票TOPがログインフォームに化けていたら再ログインが必要。
        # (長時間 daemon で session 失効すると goto がログインへリダイレクトされる)
        try:
            if self.page.locator(SELECTORS["login_id"]).count() > 0:
                raise OddsparkBetError(
                    "セッション切れ — daemon を再起動してログインし直してください")
        except OddsparkBetError:
            raise
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.click(SELECTORS["matome_link"])
            self.page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            self.page.wait_for_timeout(800)   # 既にまとめ画面のことも
        _select_payment_opcoin(self.page)

    def add_race(self, netkeiba_rid: str, legs: list[CartLeg],
                 label: str = "") -> tuple[str, int]:
        """1 レースの束をカート投入。戻り値 (status, ok点数)。status: ok/dup。"""
        if netkeiba_rid in self._added:
            return ("dup", 0)
        total = sum(l.stake for l in legs)
        if total > self.max_total_stake:
            raise OddsparkBetError(
                f"レース合計 ¥{total:,} > 上限 ¥{self.max_total_stake:,} — 投入しない")
        race_val = _race_meta(netkeiba_rid)
        # 対象レースのみ選択 (画面状態がズレていれば まとめ を開き直して再試行)。
        try:
            _select_only_race(self.page, race_val)
        except OddsparkBetError:
            self._goto_matome()
            _select_only_race(self.page, race_val)   # 再失敗なら締切/未発売 → 上に伝播
        ok = 0
        for i, leg in enumerate(legs, 1):
            try:
                _add_leg_to_cart(self.page, leg, race_val)
                print(f"  + [{label or netkeiba_rid}] {leg.bet_type} "
                      f"{'-'.join(map(str, leg.key))} ¥{leg.stake:,}")
                ok += 1
            except Exception as ex:  # noqa: BLE001
                _shot(self.page, f"bet_{netkeiba_rid}_{i}_FAILED")
                print(f"  ! [{label or netkeiba_rid}] 脚{i} "
                      f"({leg.bet_type} {leg.key}) スキップ: {ex}")
        _shot(self.page, f"bet_{netkeiba_rid}_filled")
        self._added.add(netkeiba_rid)
        print(f"[oddspark_bet] {label or netkeiba_rid}: {ok}/{len(legs)} 点 カート投入。"
              "**購入確定は人が押す**")
        return ("ok", ok)

    def close(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def run_session(*, headful: bool = True, manual_login: bool = True,
                max_total_stake: int = 10_000, poll_sec: int = 5,
                clear_existing: bool = False) -> None:
    """常駐 betting セッション: 起動時に人がログイン → queue (`QUEUE_DIR`) を監視し、
    watch-auto が積んだ <netkeiba_rid>.req の snapshot 束を同じブラウザにカート投入し続ける。
    **購入確定は常に人が押す** (自動では絶対に押さない)。Ctrl-C で終了。
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    sess = BettingSession(headful=headful, manual_login=manual_login,
                          max_total_stake=max_total_stake)
    print("[oddspark_bet] 常駐セッション開始 (B案 半自動)。")
    try:
        sess.start(clear_existing=clear_existing)
    except Exception as ex:  # noqa: BLE001
        print(f"[oddspark_bet] セッション開始失敗: {ex}")
        sess.close()
        return
    print(f"[oddspark_bet] ログイン完了。queue 監視開始: {QUEUE_DIR}")
    print("[oddspark_bet] watch-auto を --bet-oddspark で回すと発走前レースが積まれます。")
    print("[oddspark_bet] **購入確定は常に人が目視で押します** (自動では絶対に押しません)。"
          " Ctrl-C で終了。")
    attempts: dict[str, int] = {}
    try:
        while True:
            _process_bet_queue_once(sess, attempts)
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("\n[oddspark_bet] 終了 (Ctrl-C)。ブラウザを閉じます。")
    finally:
        sess.close()


def _process_bet_queue_once(sess, attempts: dict, max_attempts: int = 3) -> None:
    """queue を1巡: 各 <rid>.req の snapshot 束をカート投入し、処理確定なら .done に rename。

    - 成功 / dup / 確定エラー (OddsparkBetError: 締切・joCode無し・束無し) → .done
    - 一過性エラー (ブラウザ/通信 glitch 等) → max_attempts まで .req 残置で再試行、超過で .done
    """
    for req in sorted(QUEUE_DIR.glob("*.req")):
        rid = req.stem
        terminal = True   # 処理確定 (.done に落とす) か。一過性失敗のみ False で .req 残置
        try:
            legs, label = _legs_from_snapshot(rid)
            status, _ok = sess.add_race(rid, legs, label=label)
            if status == "dup":
                print(f"[oddspark_bet] {rid} は投入済 (skip)")
        except OddsparkBetError as ex:
            print(f"[oddspark_bet] {rid} skip: {ex}")
        except Exception as ex:  # noqa: BLE001 — ブラウザ/通信 glitch は一過性とみなす
            attempts[rid] = attempts.get(rid, 0) + 1
            terminal = attempts[rid] >= max_attempts
            note = "上限到達→打ち切り" if terminal else f"再試行 {attempts[rid]}/{max_attempts}"
            print(f"[oddspark_bet] {rid} 失敗 ({note}): {ex}")
        if terminal:
            try:
                req.rename(req.with_suffix(".done"))   # 再投入防止
            except Exception:  # noqa: BLE001
                pass


def _to_netkeiba_rid(arg: str) -> str:
    """入力を netkeiba rid (12桁 YYYYVVMMDDRR) に正規化。
    内部 race_id (`<cup10>-<si>-<rn>`, 例 2026500527-527-9) も受ける。
    """
    a = arg.strip()
    if re.fullmatch(r"\d{12}", a):                       # netkeiba rid そのまま
        return a
    m = re.fullmatch(r"(\d{10})-(\d+)-(\d+)", a)          # 内部 race_id cup-si-rn
    if m:
        cup, _si, rn = m.group(1), m.group(2), int(m.group(3))
        return f"{cup}{rn:02d}"
    raise OddsparkBetError(
        f"race_id 形式不正: {arg!r} (netkeiba rid 12桁 か 内部 race_id <cup>-<si>-<rn>)")


def _main() -> None:
    argv = sys.argv[1:]
    # 常駐セッション (watch-auto --bet-oddspark と連携): 起動時に人がログイン → queue 監視。
    if "--session" in argv:
        poll = 5
        for a in argv:
            if a.startswith("--poll="):
                try:
                    poll = max(1, int(a.split("=", 1)[1]))
                except ValueError:
                    print(f"[oddspark_bet] --poll 値が不正 ({a}) → 既定 {poll}s")
        run_session(
            headful="--headless" not in argv,
            manual_login="--auto-login" not in argv,   # 既定は人がログイン
            poll_sec=poll,
            clear_existing="--clear" in argv,
        )
        return
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print("usage:\n"
              "  one-shot: python -m src.oddspark_bet <netkeiba_nar_race_id|race_id> "
              "[--headless] [--manual-login]\n"
              "  常駐    : python -m src.oddspark_bet --session [--auto-login] [--poll=5] [--clear]")
        raise SystemExit(2)
    try:
        rid = _to_netkeiba_rid(args[0])
        legs, label = _legs_from_snapshot(rid)
        print(f"[oddspark_bet] {label}: snapshot から {len(legs)} 点")
        fill_cart(rid, legs,
                  headful="--headless" not in sys.argv,
                  manual_login="--manual-login" in sys.argv)
    except OddsparkBetError as ex:
        print(f"[oddspark_bet] {ex}")
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
