"""オッズパーク 半自動投票 (B案: カート投入まで自動・購入確定は人) の Playwright scaffold。

**重要 / 安全方針:**
  - これは「買い目をカートに入れる」までを自動化し、**購入確定ボタンは絶対に押さない**。
    最終確定は必ず人間が headful ブラウザで目視して行う (誤発注の最終ゲート)。
  - ログイン情報は **`.env` / 環境変数のみ** から読む (load_dotenv)。コード/ログ/コミットには
    絶対残さない (`.env` は .gitignore 済):
        ODDS_PARK_ID        オッズパーク会員ID (加入者番号)  ※旧 ODDSPARK_ID も fallback
        ODDS_PARK_PASSWORD  パスワード                      ※旧 ODDSPARK_PASSWORD も fallback
        ODDS_PARK_PIN       投票暗証番号 (要る場合のみ)      ※旧 ODDSPARK_PIN も fallback
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

# 認証情報は .env から読む (analyze.py と同じ流儀)。OS env が優先 (load_dotenv は上書きしない)。
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

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
    # PIN 追加認証 (InputPinPc.jsp, 普段使ってない端末で要求, 実機 HTML 確認済 2026-05-29):
    # <form name="userConfirmForm" action="/jsp/sso_filter/LoginPc.jsp"
    #   onsubmit="return submitCheck();">
    #   <input class="pin" type="password" name="INPUT_PIN" maxlength="4">
    #   <input name="送信" type="submit" value="確　認">
    # </form>
    "pin_input": 'input[name="INPUT_PIN"]',          # 暗証番号 (半角数字4桁)
    "pin_submit": 'form[name="userConfirmForm"] input[type="submit"]',
    "logged_in_marker": "text=ログアウト",            # 確定 (マイページに存在)
    "matome_link": '#todayMultiRace',                # 確定 (レースまとめ投票へ)
    # まとめ投票画面 (確定):
    "race_checkbox": 'input[type="checkbox"][value="{race_val}"]',  # value=kaisaiBi_joCode_raceNo
    "bet_type_checkbox": 'input[name="betTypeSelect"][value="{code}"]',  # 1-9
    "amount_input": '#textfield11',                  # 金額 (100円単位 / "00円" 接尾)
    "set_button": '#multiSet',                       # セット (買い目を #buylist に積む)
    "payment_opcoin": '#paymentMethodOpCoin',        # 支払=OPコイン (口座は OPコイン残, value=1)
    "payment_buylimit": '#paymentMethodBuyLimit',    # 支払=投票資金 (value=0)
    "buylist": '#buylist',                           # 積まれた買い目一覧 (人が目視)
    "delete_selected": '#choice a',                  # 選択項目削除 (誤りの訂正)
    "delete_all": '#all a',                          # 全買い目削除 (開始時クリア用)
    "horse_cell": '#horseArea td[name="horse{pos}"] a',  # pos=1/2/3 (着順列), text=馬番
    # 馬番グリッドのリセット (各脚の前に押して累積/トグルを防ぐ)。実機 DOM 確認済:
    # <a id="reset">リセット</a> (div.control 内なので 馬番/枠番 のみクリア)。
    "umaban_reset": '#reset',
    # 投票内容確認画面への遷移ボタン (まとめ画面)。半自動モードではここで人が止まる。
    "confirm_purchase": '#gotobuy',
    # 確認画面 (VoteConfirmOpcoin.do) の **最終購入確定ボタン**。実機 HTML で確認済 (2026-05-28):
    # <a href="#" onclick="clickVoteComplete();" id="buy">投票を申込</a>
    # form name=voteCompleteOpcoinForm action=/keiba/auth/VoteCompleteOpcoin.do で確定 POST。
    # 厳格セレクタのみ: #buy が無ければ即 failed で止める (FAQ 等の「購入確定」テキストを
    # 誤クリック → 関係ない場所に navigate → 別ページの body 文言で成功誤検知の経路を防ぐ)。
    "confirm_final_candidates": [
        '#buy',                                # 確定 (id 単独で一意)
        'a[onclick*="clickVoteComplete"]',     # 確定の onclick (id 消えた時の保険、形が固有)
    ],
    # 購入成功の証跡 (VoteCompleteOpcoin.do の実 HTML 確認済 2026-05-28 園田10R 700円):
    # h2 = "投票申込完了" / body = "投票申込を受け付けました。" / 表 = "成立組数" "成立合計金額"。
    # これらのいずれかが body テキストに出れば購入成功とみなし daily_stake を加算する。
    # 必ず複数 marker で OR 判定 (1つの marker だけだと文言改訂で全件 failed になる)。
    "purchase_success_markers": ("投票申込完了", "受け付けました", "成立組数"),
    # ⚠ 不成立 (購入失敗) の証跡。完了画面 (VoteCompleteOpcoin.do) は **締切後/オッズ無効等で
    # 全脚 不成立 でも** 上記 success markers (h2 "投票申込完了" / "受け付けました" / "成立組数")
    # を全て出す (実機確認 2026-05-31 水沢2R: 全3脚 受付欄 ✕, 成立組数 0通り, 成立合計金額 0円,
    # body に「投票申込不成立商品があります」)。よって success markers だけでは偽陽性 →
    # 下記 reject markers を検出したら **failed 扱いで daily_stake を加算しない**。
    "purchase_reject_markers": ("不成立", "成立した投票はありません"),
}
# 当方 bet_type → オッズパーク betType コード (betTypeSelect value, 実機確認済)。
_BET_TYPE_CODE = {
    "win": "1", "place": "2", "quinella": "5", "exacta": "6",
    "wide": "7", "trio": "8", "trifecta": "9",
}
# 馬単/3連単 (順序付き) の自動投入可否。実機検証済 (2026-05-28 笠松1R, persistent profile):
# まとめ画面で 馬単/3連単 を選んでも 裏目/マルチ は既定 OFF (<a id="betType{N}MultiFlag"
# class="btn-urame|btn-multi"> は `disabled`→選択で enabled だが ON クラス無し) で、
# 1脚 = 単一順列 (馬単[1,2]→組数+1, 3連単[1,2,3]→組数+1, #buylist は 1→2 / 1→2→3 で正順)。
# 過剰投入 (+2/+6) は `_assert_combo_delta` が捕捉して当該脚を中止する二段の安全。
_ORDERED_BETS_VERIFIED = True

# 自動購入 (#gotobuy → 確認画面 → 確定 まで完全自動) の安全フラグ。実機検証済
# (2026-05-28 笠松10R 馬単8→9 ¥2,100 の VoteConfirmOpcoin.do HTML 全文を確認):
# 確定ボタンは <a id="buy" onclick="clickVoteComplete()">投票を申込</a> で一意特定可。
# form は voteCompleteOpcoinForm → /keiba/auth/VoteCompleteOpcoin.do に POST、確定後の
# 完了画面で「受付完了」テキストが出る想定 (CLAUDE.md の検索ルールと整合)。
AUTO_PURCHASE_VERIFIED = True

# デイリー上限の既定値 (円)。1 日の累計賭金がここを超えると _confirm_purchase が "skipped"
# を返し、カートはそのまま (人が判断)。日跨ぎ (JST 00:00) で自動的に counter が 0 に戻る。
DAILY_CAP_DEFAULT = 50_000
DAILY_STAKE_FILE = ROOT / "data" / "cache" / "oddspark_daily_stake.json"
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


def _env_any(*names: str) -> str:
    """複数の env 名を順に試し最初に見つかった非空値を返す (.env の別名表記を吸収)。"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


def _creds() -> dict:
    """環境変数 (.env 含む) から認証情報を取得 (無ければエラー)。コードには残さない。

    .env は `ODDS_PARK_ID` / `ODDS_PARK_PASSWORD` 表記、旧 `ODDSPARK_*` も fallback で受ける。
    """
    cid = _env_any("ODDS_PARK_ID", "ODDSPARK_ID")
    pw = _env_any("ODDS_PARK_PASSWORD", "ODDSPARK_PASSWORD")
    if not cid or not pw:
        raise OddsparkBetError(
            "ODDS_PARK_ID / ODDS_PARK_PASSWORD を .env か環境変数で渡してください (コミット禁止)")
    return {"id": cid, "password": pw,
            "pin": _env_any("ODDS_PARK_PIN", "ODDSPARK_PIN")}


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


def _submit_login_form(page, creds: dict) -> None:
    """ID/PW を入れて #btn-login (JS formSubmit() → LoginPc.jsp) を押す。"""
    page.fill(SELECTORS["login_id"], creds["id"])
    page.fill(SELECTORS["login_password"], creds["password"])
    page.click(SELECTORS["login_submit"])
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(3000)


def _submit_pin_form(page, pin: str) -> None:
    """暗証番号 (追加認証) を name=INPUT_PIN にフィルして 確認 (name=送信) を押す。"""
    page.fill(SELECTORS["pin_input"], pin)
    page.click(SELECTORS["pin_submit"])
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(3000)


def _is_logged_in(page) -> bool:
    """ログイン成功判定: ログインフォームも PIN フォームも見えない (=マイページ等に居る)。"""
    return (page.locator(SELECTORS["login_id"]).count() == 0
            and page.locator(SELECTORS["pin_input"]).count() == 0)


def _login(page, creds: dict) -> None:
    """ログイン (login セレクタは実機確認済)。送信は JS formSubmit() なので #btn-login click。

    追加認証 (PIN) の重要な実機挙動: 普段使ってない端末では InputPinPc.jsp で暗証番号を要求され、
    **PIN 突破後に端末登録が走って一旦ログイン画面へ戻される** (= 「PIN 入力後にまたログイン画面」)。
    この 2 回目のログインでは PIN は聞かれない (端末が信頼済になる) ので、login フォームが再出現
    したら **ID/PW を入れ直して再ログイン**する。最大 3 パスまで試し、PIN 誤り/ロックや永続的な
    login 残存は明示エラーにする (黙って「セッション切れ」に化けさせない)。
    """
    page.goto(_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    pin_attempted = False
    # パス: ①ID/PW → ②PIN (要求時) → ③端末登録で login に戻れば再 ID/PW … を収束まで。
    for _pass in range(3):
        if _is_logged_in(page):
            return
        # login フォームが見えるなら ID/PW を投入
        if page.locator(SELECTORS["login_id"]).count() > 0:
            try:
                _submit_login_form(page, creds)
            except Exception as ex:  # noqa: BLE001
                _shot(page, "login_failed")
                raise OddsparkBetError(f"ログイン送信に失敗 (login セレクタ確認): {ex}") from ex
        # 追加認証 (PIN) ページなら突破
        if page.locator(SELECTORS["pin_input"]).count() > 0:
            pin = (creds.get("pin") or "").strip()
            if not pin:
                _shot(page, "login_pin_required_no_env")
                raise OddsparkBetError(
                    "追加認証 (PIN) を要求されたが PIN が未設定 — "
                    ".env の ODDS_PARK_PIN を設定するか --manual-login で人が入力してください")
            if pin_attempted:
                # 既に PIN を入れたのに再度 PIN ページ = PIN 誤り or ロック
                _shot(page, "login_pin_failed")
                raise OddsparkBetError(
                    "PIN 再要求 — ODDS_PARK_PIN 誤り or ロック警告 (一定回数誤入力でロック)")
            pin_attempted = True
            try:
                _submit_pin_form(page, pin)
            except Exception as ex:  # noqa: BLE001
                _shot(page, "login_pin_failed")
                raise OddsparkBetError(f"PIN 送信に失敗: {ex}") from ex
            # PIN 後は端末登録で login へ戻される場合がある → 次パスで再ログイン
            _shot(page, "login_after_pin")
            continue
        # ここに来て logged_in なら成功、そうでなければ次パスで再試行
        if _is_logged_in(page):
            return
    # 3 パスやっても login/PIN が残る = 失敗
    if not _is_logged_in(page):
        _shot(page, "login_failed")
        raise OddsparkBetError(
            "ログイン未完了 (PIN 後の再ログインにも失敗) — ID/PW・PIN 誤り / ロック / 要セレクタ確認")


PAYMENT_OPCOIN = "opcoin"        # 既定: OPコイン残から引落 → VoteConfirm/CompleteOpcoin.do フロー
PAYMENT_BUYLIMIT = "buylimit"    # 投票資金: 投票資金残から引落 (会員入金額)
_VALID_PAYMENT_METHODS = {PAYMENT_OPCOIN, PAYMENT_BUYLIMIT}


def _select_payment_method(page, method: str = PAYMENT_OPCOIN) -> None:
    """支払方法 radio を選択する。method=opcoin (既定) | buylimit。

    OPコイン  : `#paymentMethodOpCoin`  (value=1) — OPコイン残 (チャージ済) から引落。
    投票資金  : `#paymentMethodBuyLimit` (value=0) — 投票資金残 (会員入金) から引落。
    どちらも radio name=paymentMethod なので排他選択。

    **検証付き**: check 後に is_checked() で確認し、要求した radio が選択されていない
    時は OddsparkBetError を raise (誤って別の支払元から引落される silent failure を防ぐ)。
    radio が画面に存在しない場合 (まだ matome に遷移してない等) は黙って続行 (DOM 不安定)。
    """
    sel_key = "payment_opcoin" if method == PAYMENT_OPCOIN else "payment_buylimit"
    try:
        page.check(SELECTORS[sel_key])
    except Exception:  # noqa: BLE001
        pass   # 既選択/transient 失敗 → 下の is_checked で最終判定
    # 検証: radio が存在するなら必ず checked であることを確かめる
    try:
        loc = page.locator(SELECTORS[sel_key])
        if loc.count() == 0:
            return                              # radio 不在 → DOM 未到達、黙って続行
        if not loc.first.is_checked():
            raise OddsparkBetError(
                f"支払方法 {method} の radio が選択されていない — 別の支払元から誤って "
                f"引落される危険があるため中止 (selector={SELECTORS[sel_key]})")
    except OddsparkBetError:
        raise
    except Exception:  # noqa: BLE001
        pass   # is_checked 自体の失敗 (DOM unstable) → 黙って続行


# 後方互換 (旧名 import を壊さない)。新規呼び出しは _select_payment_method を使う。
def _select_payment_opcoin(page) -> None:
    """[deprecated] 後方互換: OPコイン選択。新規は _select_payment_method を使うこと。"""
    _select_payment_method(page, PAYMENT_OPCOIN)


def safe_dialog_accept(d) -> None:
    """削除/セット/購入確定時の confirm() を自動承認。

    Playwright の dialog イベント発火時には存在していた dialog が、accept() コールが
    完了する前に閉じる timing race があり、`Dialog.accept: No dialog is showing` が
    handler に上がってログを汚す (機能には影響なし — カート投入は成功する)。実害は
    無いので握りつぶす。半自動モードでは #gotobuy を一切クリックしない設計だが、
    `auto_purchase=True` モードでは購入確定の dialog も自動承認の対象になる
    (アプリ層の AUTO_PURCHASE_VERIFIED / daily_cap が買うか買わないかを決める)。
    """
    try:
        d.accept()
    except Exception:  # noqa: BLE001 — Playwright timing race / 既に閉じた dialog
        pass


# -------------------------- デイリー上限 / 自動購入 ヘルパ ---------------------

def _today_jst() -> str:
    """JST の "YYYY-MM-DD"。日次累計のキー。"""
    import datetime as _dt
    jst = _dt.timezone(_dt.timedelta(hours=9))
    return _dt.datetime.now(jst).strftime("%Y-%m-%d")


def _load_daily_stake_map() -> dict[str, int]:
    if not DAILY_STAKE_FILE.exists():
        return {}
    try:
        d = json.loads(DAILY_STAKE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: int(v) for k, v in d.items() if isinstance(v, (int, float))}


def _save_daily_stake_map(d: dict[str, int]) -> None:
    DAILY_STAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_STAKE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(DAILY_STAKE_FILE)   # atomic


def get_today_stake() -> int:
    """本日(JST)の累計購入額(円)。日跨ぎで自動的に 0 に戻る (キーが変わるため)。"""
    return _load_daily_stake_map().get(_today_jst(), 0)


def _detect_purchase_reject(body_txt: str) -> str | None:
    """完了画面 (VoteCompleteOpcoin.do) の body テキストから **不成立 (購入失敗)** を検出。

    戻り値: 不成立なら理由文字列、成立 (正常購入) なら None。

    完了画面は締切後/オッズ無効等で全脚不成立でも success markers
    (h2 "投票申込完了" / "受け付けました" / "成立組数") を出すため、これだけでは
    偽陽性になる。実機 (2026-05-31 水沢2R) の不成立画面は:
      - body に「投票申込不成立商品があります」
      - 成立組数 **0通り** / 成立合計金額 **0円**
      - 各脚の受付欄に ✕
    → ① reject marker (不成立 等) を含む ② "成立合計金額 …0円" / "成立組数 …0通り"
       のいずれかが取れたら不成立とみなす (二段で取りこぼし防止)。
    """
    if not body_txt:
        return None
    for m in SELECTORS["purchase_reject_markers"]:
        if m in body_txt:
            return f"reject marker '{m}' を検出"
    # 成立組数 0通り / 成立合計金額 0円 を数値で確認 (marker 文言が変わっても効く保険)。
    import re
    m_cnt = re.search(r"成立組数[^0-9]*([0-9,]+)\s*通り", body_txt)
    if m_cnt and int(m_cnt.group(1).replace(",", "")) == 0:
        return "成立組数 0通り"
    m_sum = re.search(r"成立合計金額[^0-9]*([0-9,]+)\s*円", body_txt)
    if m_sum and int(m_sum.group(1).replace(",", "")) == 0:
        return "成立合計金額 0円"
    return None


def record_daily_stake(amount: int) -> int:
    """today's stake に amount を加算して新規累計を返す。**購入 success 検出時のみ呼ぶ**。"""
    d = _load_daily_stake_map()
    today = _today_jst()
    d[today] = d.get(today, 0) + max(0, amount)
    _save_daily_stake_map(d)
    return d[today]


def _apply_stake_multiplier(legs: list["CartLeg"], multiplier: float) -> list["CartLeg"]:
    """各 leg.stake を multiplier 倍 (100円単位に丸め)、新しい CartLeg リストを返す。

    multiplier=1.0 は no-op (引数をそのまま返す)。min 100円 を保証 (1円賭けは不可)。
    """
    if multiplier == 1.0:
        return legs
    out: list[CartLeg] = []
    for l in legs:
        new_stake = max(100, int(round(l.stake * multiplier / 100.0)) * 100)
        out.append(CartLeg(bet_type=l.bet_type, key=list(l.key), stake=new_stake))
    return out


def check_daily_cap(prospective_stake: int, daily_cap: int) -> tuple[bool, str]:
    """この race の stake (円) を加えても daily_cap 以内か判定。

    戻り値 (allowed, message)。daily_cap<=0 は無効化扱い (常に allowed)。
    """
    if daily_cap <= 0:
        return (True, "daily_cap 無効 (≤0)")
    today = get_today_stake()
    projected = today + max(0, prospective_stake)
    if projected > daily_cap:
        return (False,
                f"日次上限超過: 本日累計¥{today:,} + 本race¥{prospective_stake:,} = "
                f"¥{projected:,} > 上限¥{daily_cap:,}")
    return (True,
            f"日次累計¥{today:,} + 本race¥{prospective_stake:,} "
            f"→ ¥{projected:,} / 上限¥{daily_cap:,}")


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


def _combo_count(page) -> int | None:
    """カートの現在の合計組数 ("組数：N通り") を読む。読めなければ None。

    要素 id 非依存で body テキストから正規表現抽出する (まとめ画面の summary 欄)。
    セット の前後で読んで差分を取り、1 脚が想定どおり 1 組だけ増えたかを検証する
    (裏目/マルチ の全順列化や馬番累積で組数が膨れるのを捕捉する最終ゲート)。
    """
    try:
        v = page.evaluate(
            "() => { const m = (document.body.innerText || '')"
            ".match(/\\u7d44\\u6570[^0-9]*([0-9,]+)\\s*\\u901a\\u308a/);"
            " return m ? m[1] : null; }")
    except Exception:  # noqa: BLE001
        return None
    if not v:
        return None
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return None


def _uncheck_ura_multi(page) -> None:
    """馬単[裏目]/3連単[マルチ] (着順入替えの全順列化) を OFF にする。

    まとめ画面では betType checkbox の隣の **トグルリンク** `<a id="betType{N}MultiFlag"
    class="btn-urame|btn-multi ...">裏目/マルチ</a>` で制御 (チェックボックスではない)。
    `disabled` クラスは無効(=OFF)。`on`/`active`/`selected` 等の有効クラスが付いていたら
    全順列化されるので click して OFF にする。要素 id (betType4/6/9MultiFlag) で確実に
    特定でき、checkbox には一切触れない (賭式選択を誤って外さない)。取りこぼしは
    `_assert_combo_delta` の 組数=1 検証で最終捕捉する (二段の安全)。
    """
    try:
        page.evaluate(
            "() => { for (const a of document.querySelectorAll("
            "'a[id^=\"betType\"][id$=\"MultiFlag\"]')) {"
            " const c = ' ' + (a.className || '') + ' ';"
            " if (c.indexOf(' disabled ') < 0 &&"
            " /( on | active | selected | btn-urame-on | btn-multi-on )/.test(c)) a.click();"
            " } }")
    except Exception:  # noqa: BLE001
        pass


def _assert_combo_delta(page, before: int | None, leg: CartLeg) -> None:
    """セット後、組数が想定どおり 1 組だけ増えたか検証 (過剰/欠落投入の捕捉)。

    1 脚 = 必ず 1 組 (単複=1, 馬連/ワイド/3連複=1 ペア/トリオ, 馬単/3連単=1 順列)。
    裏目/マルチ オンや馬番累積だと +2/+6 等になるので raise して当該脚を中止する。
    組数を読めない時: 順序付き (馬単/3連単) は全順列化を検知できないので安全側で中止、
    順不同は a.on 検証で別途担保済なので続行 (既存の検証済フローを壊さない)。
    """
    after = _combo_count(page)
    # 組数表示の更新が非同期で遅延することがある: まだ変化が出ていなければ一度だけ
    # 待って再読する (セット成功脚を timing で誤って中止しないため)。過剰投入 (+2/+6) は
    # セット完了後に確定値で出るので、この再読は after==before の時しか発火しない。
    if before is not None and after == before:
        page.wait_for_timeout(1500)
        after = _combo_count(page)
    if before is None or after is None:
        if leg.bet_type in ("exacta", "trifecta"):
            raise OddsparkBetError(
                "組数を読めず 裏目/マルチ の全順列化を検知できない — 安全側で中止 "
                f"({leg.bet_type} {leg.key})")
        return
    added = after - before
    if added != 1:
        raise OddsparkBetError(
            f"組数が想定外: +{added}通り (期待 +1) — 裏目/マルチ/馬番累積を疑い中止 "
            f"({leg.bet_type} {leg.key})")


def _add_leg_to_cart(page, leg: CartLeg, race_val: str) -> None:
    """1 買い目を「まとめ投票」画面でセット (#buylist へ)。**確定は押さない**。

    順序 (実機準拠): 金額入力 → レースチェック → 賭式選択 → (順序付きは裏目/マルチ OFF)
    → 馬番選択 → セット → 組数=+1 検証。
    """
    # 馬単/3連単 (順序付き) は実機検証済 (_ORDERED_BETS_VERIFIED=True)。万一フラグを False に
    # 戻した場合は fail-safe で見送る (裏目/マルチ の全順列化制御が未検証扱いになるため)。
    if leg.bet_type in ("exacta", "trifecta") and not _ORDERED_BETS_VERIFIED:
        raise OddsparkBetError(
            f"{leg.bet_type} は裏目/マルチ(全順列)制御が未検証のため自動投入を見送り — "
            "手動で投入してください")
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
        # 3.5) 順序付き (馬単/3連単) は 裏目/マルチ を OFF にして全順列化を防ぐ
        if leg.bet_type in ("exacta", "trifecta"):
            _uncheck_ura_multi(page)
        # 4) 馬番選択 — **前脚の選択が残るので必ずリセットしてから** (累積/トグル防止)
        _reset_umaban(page)
        _select_umaban(page, leg)
        # 5) セット (#buylist へ積む) → 組数が +1 だけ増えたか検証
        before = _combo_count(page)
        page.click(SELECTORS["set_button"])
        page.wait_for_timeout(1000)
        _assert_combo_delta(page, before, leg)
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
                 max_total_stake: int = 10_000, login_wait_sec: int = 600,
                 auto_purchase: bool = False, daily_cap: int = DAILY_CAP_DEFAULT,
                 stake_multiplier: float = 1.0,
                 payment_method: str = PAYMENT_OPCOIN) -> None:
        self.headful = headful
        self.manual_login = manual_login
        self.max_total_stake = max_total_stake
        self.login_wait_sec = login_wait_sec
        # 自動購入: True で add_race の最後に #gotobuy → 確認画面 → 確定 まで自動。
        # 安全策: ① AUTO_PURCHASE_VERIFIED フラグ (実機検証前は実弾を撃たない)、
        # ② per-race max_total_stake (既存)、③ daily_cap で日次累計を制限、
        # ④ success marker 検出時のみ daily 加算 (誤検知/失敗で二重購入しない)。
        self.auto_purchase = auto_purchase
        self.daily_cap = daily_cap
        # **このセッション中のみ** 全 leg の stake を multiplier 倍する (100円単位丸め)。
        # 例: 1.0=既定 / 2.0=倍掛け / 0.5=半額。per-race 上限/日次上限は維持されるので、
        # 倍率により合計が max_total_stake を超える race は通常通り reject される。
        self.stake_multiplier = float(stake_multiplier) if stake_multiplier else 1.0
        # 支払方法: opcoin (OPコイン残) または buylimit (投票資金残)。既定は OPコイン。
        if payment_method not in _VALID_PAYMENT_METHODS:
            payment_method = PAYMENT_OPCOIN
        self.payment_method = payment_method
        self.page = None
        self._pw = None
        self.browser = None
        self.ctx = None
        self._added: set[str] = set()   # 投入済 netkeiba_rid (二重投入防止)
        self._session_staked = 0        # 本セッションで #buylist に投入した累計 (未確定含む)

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
        # timing race で「No dialog is showing」が上がるので named helper で握りつぶす。
        self.page.on("dialog", safe_dialog_accept)
        # 1) ログイン
        if self.manual_login:
            self._wait_manual_login()   # 人がブラウザでログイン → poll で検出 (stdin 非依存)
        else:
            # 自動ログイン: 認証情報が無い / ログイン失敗のときは **例外で即終了せず手動に
            # フォールバック** (ブラウザを開いたまま人がログインできる)。これがないと
            # `make api` の env に ODDSPARK_* が無い場合に daemon が 0.5s で code 0 終了し、
            # 「ブラウザが一瞬開いて閉じる」になる (UI には理由が出ない)。
            try:
                _login(self.page, _creds())
            except Exception as ex:  # noqa: BLE001
                print(f"[oddspark_bet] 自動ログイン失敗 ({ex}) → 手動ログインにフォールバック。"
                      " ブラウザでログインしてください "
                      "(env の ODDSPARK_ID/ODDSPARK_PASSWORD/ODDSPARK_PIN を確認)。", flush=True)
                self._wait_manual_login()
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

    def _wait_manual_login(self) -> None:
        """人が headful ブラウザでログインするのを **poll で待つ** (stdin 非依存)。

        `input()` だと background プロセス (統合起動 `make watch-auto-bet` で daemon を
        `&` 起動した時) で stdin 読取がサスペンドするため、ログアウトリンク (logged_in_marker)
        の出現を polling で検出する。login_wait_sec 以内に検出できなければ中止。
        """
        self.page.goto(_BASE, wait_until="domcontentloaded")
        print(f"[oddspark_bet] headful ブラウザでオッズパークにログインしてください "
              f"(検出まで最大 {self.login_wait_sec}s 待機)...", flush=True)
        print("[oddspark_bet] ※ 暗証番号(PIN)による追加認証が出た端末では、PIN 入力後に"
              "ログイン画面へ戻されることがあります。その場合は **もう一度 ID/PW でログイン**"
              "してください (2 回目は PIN 不要)。", flush=True)
        for i in range(self.login_wait_sec):
            try:
                if self.page.locator(SELECTORS["logged_in_marker"]).count() > 0:
                    print("[oddspark_bet] ログイン検出。", flush=True)
                    return
            except Exception:  # noqa: BLE001
                pass
            if i and i % 30 == 0:
                print(f"[oddspark_bet] ログイン待機中... ({i}s)", flush=True)
            self.page.wait_for_timeout(1000)
        raise OddsparkBetError(
            f"{self.login_wait_sec}s 以内にログインを検出できず — ブラウザでログインして再起動してください")

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
        _select_payment_method(self.page, self.payment_method)

    def add_race(self, netkeiba_rid: str, legs: list[CartLeg],
                 label: str = "") -> tuple[str, int]:
        """1 レースの束をカート投入。戻り値 (status, ok点数)。status: ok/dup。

        `stake_multiplier != 1.0` のときは leg.stake を倍率倍して 100 円単位に丸める。
        合計が `max_total_stake` を超えると race ごと reject (倍率込みで安全網が効く)。
        """
        if netkeiba_rid in self._added:
            return ("dup", 0)
        if self.stake_multiplier != 1.0:
            legs = _apply_stake_multiplier(legs, self.stake_multiplier)
        total = sum(l.stake for l in legs)
        if total > self.max_total_stake:
            raise OddsparkBetError(
                f"レース合計 ¥{total:,} > 上限 ¥{self.max_total_stake:,} "
                f"(stake_multiplier={self.stake_multiplier}x 適用後) — 投入しない")
        race_val = _race_meta(netkeiba_rid)
        # 対象レースのみ選択 (画面状態がズレていれば まとめ を開き直して再試行)。
        try:
            _select_only_race(self.page, race_val)
        except OddsparkBetError:
            self._goto_matome()
            _select_only_race(self.page, race_val)   # 再失敗なら締切/未発売 → 上に伝播
        ok = 0
        staked = 0   # 実際に セット まで通った脚の合計 (累計露出の正確な加算用)
        for i, leg in enumerate(legs, 1):
            try:
                _add_leg_to_cart(self.page, leg, race_val)
                print(f"  + [{label or netkeiba_rid}] {leg.bet_type} "
                      f"{'-'.join(map(str, leg.key))} ¥{leg.stake:,}")
                ok += 1
                staked += leg.stake
            except Exception as ex:  # noqa: BLE001
                _shot(self.page, f"bet_{netkeiba_rid}_{i}_FAILED")
                print(f"  ! [{label or netkeiba_rid}] 脚{i} "
                      f"({leg.bet_type} {leg.key}) スキップ: {ex}")
        _shot(self.page, f"bet_{netkeiba_rid}_filled")
        self._added.add(netkeiba_rid)
        self._session_staked += staked
        # auto_purchase=True: ここで実際に購入確定 (実弾)。 false なら従来通り人が確定。
        if self.auto_purchase and staked > 0:
            p_status, p_msg = self._confirm_purchase(staked)
            tag = "[magenta]" if p_status == "ok" else "[yellow]" if p_status == "skipped" else "[red]"
            print(f"  {tag}→ 自動購入 {p_status}:[/] {p_msg}")
            mode_note = f"自動購入 ({p_status})"
            if p_status == "ok":
                # 完了画面 → 続けて投票 → まとめ画面 で次レース受入準備に戻す
                self._continue_to_matome_after_purchase()
            else:
                # ok 以外 (failed / skipped): cart に当該 race の脚が残っている可能性。
                # 残ったまま次レースが add_race に来て #gotobuy を押すと、leftover legs +
                # 次レース legs が一括購入されるが daily_stake には次レース分しか加算されず
                # **cap 突破リスク**。まずまとめ画面に戻し (確認画面に居る場合は離脱で
                # 未確定 bet 破棄)、その後 #all a で cart 全削除して次レースへの漏れを防ぐ。
                self._continue_to_matome_after_purchase()
                try:
                    da = self.page.locator(SELECTORS["delete_all"])
                    if da.count() > 0:
                        da.first.click()
                        self.page.wait_for_timeout(500)
                        print(f"  [yellow]→ {p_status} のためカート全削除 (#all a) "
                              "で次レースへの漏れ防止[/]")
                except Exception as ex:  # noqa: BLE001
                    print(f"  [red]⚠ カート削除に失敗 — 次レースで leftover が一括購入される"
                          f"危険 (手動で全買い目削除推奨): {ex}[/]")
        else:
            mode_note = "**購入確定は人が押す**"
        print(f"[oddspark_bet] {label or netkeiba_rid}: {ok}/{len(legs)} 点 カート投入。"
              f"{mode_note} (本セッション投入累計 ¥{self._session_staked:,} 未確定含む)")
        if not self.auto_purchase and self._session_staked > self.max_total_stake:
            print(f"[oddspark_bet] ⚠ 累計 ¥{self._session_staked:,} が per-race 上限 "
                  f"¥{self.max_total_stake:,} 超。**購入確定は #buylist 全体を一括購入する**ので、"
                  "レースごとに確定/クリアして溜め過ぎに注意。")
        return ("ok", ok)

    def _confirm_purchase(self, race_stake: int) -> tuple[str, str]:
        """カートを #gotobuy → 確認画面 → 確定 で **実際に購入** する。**実弾**。

        戻り値 (status, message)。
        - "ok": 購入成功 (受付完了 marker 検出 → daily_stake を加算)
        - "skipped": 条件未充足で実行せず (フラグ未検証 / daily_cap 超過 / auto_purchase=False)
        - "failed": クリック失敗 / DOM 不一致 / success marker 不検出 (daily_stake は加算しない)

        4 段の安全:
        ① AUTO_PURCHASE_VERIFIED (実機検証フラグ) が False ならスキップ
        ② daily_cap で本日累計を制限 → 超過なら NOOP (カート残し人が判断)
        ③ 確認画面で確定ボタンを **複数候補から検出** + click → success marker を必ず確認
        ④ success marker 検出時のみ daily_stake を加算 (失敗で二重購入しない)
        """
        if not self.auto_purchase:
            return ("skipped", "auto_purchase=False")
        if not AUTO_PURCHASE_VERIFIED:
            return ("skipped",
                    "AUTO_PURCHASE_VERIFIED=False (確認画面の最終ボタン DOM 未検証なので fail-safe)")
        allowed, msg = check_daily_cap(race_stake, self.daily_cap)
        if not allowed:
            return ("skipped", msg)
        # 1) 投票内容確認画面へ (#gotobuy = 「投票内容確認」ボタン)
        try:
            self.page.click(SELECTORS["confirm_purchase"])
            self.page.wait_for_timeout(2500)
        except Exception as ex:  # noqa: BLE001
            _shot(self.page, "purchase_gotobuy_failed")
            return ("failed", f"#gotobuy クリック失敗: {ex}")
        _shot(self.page, "purchase_review")
        # 2) 確認画面で最終 「確定/購入」 ボタンを click (候補を順に試す)
        clicked_sel = None
        for sel in SELECTORS["confirm_final_candidates"]:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    loc.first.click()
                    clicked_sel = sel
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked_sel:
            _shot(self.page, "purchase_review_no_button")
            return ("failed",
                    "確認画面で確定ボタンが見つからない (DOM 未検証 — purchase_review screenshot 参照)")
        self.page.wait_for_timeout(3000)
        _shot(self.page, "purchase_after_click")
        # 3) 成功検知 — 「偽陰性」(成功なのに failed → daily_stake 未加算 → cap 緩む) と
        #    「偽陽性」(未遷移なのに success → 未購入で daily_stake 加算) を両側で防ぐ:
        #    (a) URL が VoteComplete... (= server が確定 POST を受理し completion page を返した、
        #        最強の signal、navigation 起きてなければ #buy 失敗 = 未購入)
        #    (b) h2 「投票申込完了」 OR body 「受け付けました/成立組数/投票申込完了」
        #        (= ユーザ可視のレイアウト/文言、render が遅れて片方欠ける可能性があるので OR)
        # URL 遷移が起きていない時は server 確定が無いとみなし failed (safe)。URL 遷移済なら
        # h2/body のどちらか一方でも検知できれば success として daily_stake 加算 (under-count 防止)。
        # render 完了を許容するため check の前に 1 秒追加待機 + 不足分は最大 6 秒まで polling する。
        # ⚠ 「完了画面に遷移 + success marker 検出」だけでは不十分: 締切後/オッズ無効等で
        #    **全脚 不成立** でも完了画面は同じ markers (h2 "投票申込完了"/"受け付けました"/
        #    "成立組数") を出す (実機 2026-05-31 水沢2R: 成立組数 0通り/成立合計金額 0円/全脚✕)。
        #    → reject marker (不成立) または 成立合計金額==0 を検出したら failed 扱いで
        #      daily_stake を加算しない (偽陽性 = 未購入なのに cap を消費するのを防ぐ)。
        success = False
        reject_reason = None
        import time as _t
        deadline = _t.time() + 6.0
        while _t.time() < deadline:
            try:
                url = self.page.url or ""
                is_complete_url = "VoteComplete" in url
                if not is_complete_url:
                    # URL すら遷移してない: 待っても無駄 (#buy 失敗 / form submit ブロック)
                    break
                has_h2 = self.page.locator('h2:has-text("投票申込完了")').count() > 0
                body_txt = self.page.evaluate(
                    "() => document.body ? document.body.innerText || '' : ''")
                has_marker = any(m in body_txt for m in SELECTORS["purchase_success_markers"])
                if has_h2 or has_marker:
                    # 完了画面には来た。ここで 不成立 を判定 (成立合計金額==0 / "不成立")。
                    reject_reason = _detect_purchase_reject(body_txt)
                    success = reject_reason is None
                    break
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(500)
        if reject_reason is not None:
            return ("failed",
                    f"投票不成立 — 購入されていません ({reject_reason})。締切後/オッズ無効/資金不足等。"
                    " daily_stake は加算しない (purchase_after_click screenshot 参照)")
        if not success:
            return ("failed",
                    "成功検知失敗 (URL=VoteComplete... に遷移 + h2/body marker のいずれか必須) "
                    "— ブラウザで購入状況を目視確認推奨")
        new_total = record_daily_stake(race_stake)
        return ("ok",
                f"購入完了 ¥{race_stake:,} (clicked={clicked_sel}, 本日累計 ¥{new_total:,} / 上限¥{self.daily_cap:,})")

    def _continue_to_matome_after_purchase(self) -> None:
        """購入完了画面 (VoteCompleteOpcoin.do) → 「続けて投票」(#buythru) → まとめ投票
        (#todayMultiRace) で次レース受入準備に戻す。失敗時は _goto_matome に fallback。
        """
        try:
            self.page.click('#buythru')          # 続けて投票 → VoteKeibaTop.do
            self.page.wait_for_timeout(1500)
            self.page.click(SELECTORS["matome_link"])   # レースまとめ → まとめ画面
            self.page.wait_for_timeout(1500)
            _select_payment_method(self.page, self.payment_method)
            _shot(self.page, "post_purchase_matome")
            return
        except Exception:  # noqa: BLE001
            pass
        # フォールバック: 公式ナビが失敗しても URL 直 goto + matome クリックで確実に戻す
        try:
            self._goto_matome()
        except Exception as ex:  # noqa: BLE001
            print(f"[oddspark_bet] 完了画面→まとめ画面の戻りに失敗: {ex}")

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
                clear_existing: bool = False,
                auto_purchase: bool = False, daily_cap: int = DAILY_CAP_DEFAULT,
                stake_multiplier: float = 1.0,
                payment_method: str = PAYMENT_OPCOIN) -> None:
    """常駐 betting セッション: 起動時に人がログイン → queue (`QUEUE_DIR`) を監視し、
    watch-auto が積んだ <netkeiba_rid>.req の snapshot 束を同じブラウザにカート投入し続ける。

    モード:
    - auto_purchase=False (既定・半自動): カート投入のみ。**購入確定は常に人**。
    - auto_purchase=True  (実弾・全自動): #gotobuy → 確認画面 → 確定まで自動。
      per-race ¥{max_total_stake} + 日次 ¥{daily_cap} で二重ガード。AUTO_PURCHASE_VERIFIED が
      False のうちは fail-safe で実弾を撃たない (実機 DOM 検証後にフラグを True に)。
    Ctrl-C で終了。
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    sess = BettingSession(headful=headful, manual_login=manual_login,
                          max_total_stake=max_total_stake,
                          auto_purchase=auto_purchase, daily_cap=daily_cap,
                          stake_multiplier=stake_multiplier,
                          payment_method=payment_method)
    mode = "**自動購入 (実弾)**" if auto_purchase else "半自動 (人が確定)"
    pay_label = "OPコイン残" if payment_method == PAYMENT_OPCOIN else "投票資金残 (会員入金)"
    print(f"[oddspark_bet] 常駐セッション開始 ({mode}, 支払={pay_label})。")
    if stake_multiplier != 1.0:
        print(f"[oddspark_bet] ⚠ stake_multiplier={stake_multiplier}x 適用 — 全 leg の stake を "
              f"{stake_multiplier} 倍 (100円単位丸め)。per-race ¥{max_total_stake:,} 超過の race は reject。")
    if auto_purchase:
        today = get_today_stake()
        print(f"[oddspark_bet] daily_cap: 本日累計 ¥{today:,} / 上限 ¥{daily_cap:,}")
        if not AUTO_PURCHASE_VERIFIED:
            print("[oddspark_bet] ⚠ AUTO_PURCHASE_VERIFIED=False — 確認画面 DOM 未検証なので "
                  "実弾は撃たれません (fail-safe)。1 度実機で confirm 画面を検証してフラグを True に。")
    try:
        sess.start(clear_existing=clear_existing)
    except Exception as ex:  # noqa: BLE001
        print(f"[oddspark_bet] セッション開始失敗: {ex}", flush=True)
        s = str(ex)
        if any(m in s for m in ("XServer", "X server", "$DISPLAY", "has been closed")):
            print("[oddspark_bet] ⚠ headful ブラウザを開く X server / DISPLAY がありません "
                  f"(現在 DISPLAY={os.environ.get('DISPLAY') or '未設定'})。Web UI から起動する場合は "
                  "**`make api` を DISPLAY のある端末 (WSLg 等) で**起動してください。", flush=True)
        sess.close()
        return
    print(f"[oddspark_bet] ログイン完了。queue 監視開始: {QUEUE_DIR}")
    print("[oddspark_bet] watch-auto を --bet-oddspark で回すと発走前レースが積まれます。")
    if auto_purchase:
        print(f"[oddspark_bet] ⚠ **自動購入モード (実弾)**: カート投入後に #buy まで自動で押します。"
              f" per-race ¥{max_total_stake:,} / 日次 ¥{daily_cap:,} の二重ガード付き。Ctrl-C で終了。")
    else:
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
        daily_cap = DAILY_CAP_DEFAULT
        stake_multiplier = 1.0
        payment_method = PAYMENT_OPCOIN
        for a in argv:
            if a.startswith("--poll="):
                try:
                    poll = max(1, int(a.split("=", 1)[1]))
                except ValueError:
                    print(f"[oddspark_bet] --poll 値が不正 ({a}) → 既定 {poll}s")
            elif a.startswith("--daily-cap="):
                try:
                    daily_cap = max(0, int(a.split("=", 1)[1]))
                except ValueError:
                    print(f"[oddspark_bet] --daily-cap 値が不正 ({a}) → 既定 ¥{daily_cap:,}")
            elif a.startswith("--stake-multiplier="):
                try:
                    v = float(a.split("=", 1)[1])
                except ValueError:
                    v = 0
                # 0 以下や 100 超は誤入力扱いで既定に戻す (Pydantic と同じ gt=0/le=100 制約)。
                # 0 だと _apply_stake_multiplier の min ¥100 floor が走り「全 leg ¥100」の
                # 予期しない挙動になるため拒否。
                if v <= 0 or v > 100:
                    print(f"[oddspark_bet] --stake-multiplier 値が範囲外 ({a}) → 既定 1.0x "
                          "(有効範囲: 0 < N <= 100)")
                    stake_multiplier = 1.0
                else:
                    stake_multiplier = v
            elif a.startswith("--payment="):
                v = a.split("=", 1)[1].strip().lower()
                if v in _VALID_PAYMENT_METHODS:
                    payment_method = v
                else:
                    print(f"[oddspark_bet] --payment 値が不正 ({a}) → 既定 {payment_method} "
                          f"(opcoin | buylimit)")
        run_session(
            headful="--headless" not in argv,
            auto_purchase="--auto-purchase" in argv,
            daily_cap=daily_cap,
            stake_multiplier=stake_multiplier,
            payment_method=payment_method,
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
