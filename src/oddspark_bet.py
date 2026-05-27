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
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SHOT_DIR = ROOT / "data" / "cache"

_BASE = "https://www.oddspark.com"
# ログイン: 実機HTML確認済 (2026-05)。フォーム name=loginForm、隠し _csrf/SSO_* は
# フォーム送信で自動付与。送信は <a id="btn-login" href="javascript:formSubmit()"> をclick。
_LOGIN_URL = f"{_BASE}/user/my/Index.do"
# 投票エントリ (マイページの「投票する」リンク先, 実機確認済)。通常は別窓で開くが
# Playwright は直接 goto で良い。ここから 開催→レース→券種→買い目入力→カート と進む
# (その先の画面 HTML は未確認 = _vote_url 以降は要実機調整)。
_VOTE_TOP_URL = f"{_BASE}/keiba/auth/VoteKeibaTop.do?gamenId=P901&gamenKoumokuId=topVote"
# ↓↓↓ vote_* / cart 系は **要実機調整** (投票ページHTML未確認)。login は確定済。 ↓↓↓
SELECTORS = {
    "login_id": 'input[name="SSO_ACCOUNTID"]',       # 確定
    "login_password": 'input[name="SSO_PASSWORD"]',  # 確定
    "login_submit": '#btn-login',                    # 確定 (JSリンク formSubmit())
    "logged_in_marker": "text=ログアウト",            # ログイン成功判定 (要確認)
    "vote_pin": 'input[name="ansyoNo"]',             # 投票暗証 (要る場合・要確認)
    # 投票フォーム (1 買い目ぶん) — 要確認
    "bet_type_select": 'select[name="shikibetsu"]',
    "umaban_input": 'input[name="umaban"]',          # 組番入力 (式別で形式が変わる)
    "amount_input": 'input[name="kingaku"]',         # 金額 (100円単位)
    "add_to_cart": 'button.add-cart',                # カート投入
    # 購入確定は **使わない** (人が押す)。参考までに置くがコードからは押さない。
    "confirm_purchase": 'button.kakutei',
}
# 当方 bet_type → オッズパーク式別の表示値/コード (要確認)。
_SHIKIBETSU = {
    "win": "単勝", "place": "複勝", "quinella": "馬連", "wide": "ワイド",
    "exacta": "馬単", "trio": "3連複", "trifecta": "3連単",
}
# ↑↑↑ ここまで要実機調整 ↑↑↑


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
    from playwright.sync_api import sync_playwright

    from .scrape_oddspark import find_oddspark_race

    total = sum(l.stake for l in legs)
    if total > max_total_stake:
        raise OddsparkBetError(
            f"合計賭金 ¥{total:,} が上限 ¥{max_total_stake:,} を超過 — 中止 (誤入力防止)")

    loc = find_oddspark_race(netkeiba_rid)
    if loc is None:
        raise OddsparkBetError(f"オッズパークで {netkeiba_rid} の開催が見つからない")
    creds = None if manual_login else _creds()

    _SHOT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[oddspark_bet] {loc.venue} {loc.race_nb}R / {len(legs)}点 合計¥{total:,} "
          f"→ カート投入手前まで (確定は人)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(locale="ja-JP", viewport={"width": 1280, "height": 1800})
        page = ctx.new_page()
        try:
            # 1) ログイン (manual_login なら人が手で)
            if manual_login:
                page.goto(_BASE, wait_until="domcontentloaded")
                print("[oddspark_bet] headful ブラウザでログインを済ませて Enter ...")
                input()
            else:
                _login(page, creds)
            _shot(page, "1_after_login")

            # 2) 対象 race の投票ページへ (要確認: 投票 URL の組み立て)
            vote_url = _vote_url(loc)
            page.goto(vote_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            _shot(page, "2_vote_page")

            # 3) 各買い目をカート投入
            for i, leg in enumerate(legs, 1):
                _add_leg_to_cart(page, leg)
                _shot(page, f"3_cart_{i}_{leg.bet_type}")
                print(f"  + カート投入: {_SHIKIBETSU.get(leg.bet_type, leg.bet_type)} "
                      f"{'-'.join(map(str, leg.key))} ¥{leg.stake:,}")

            _shot(page, "4_cart_filled")
            print("[oddspark_bet] カート投入完了。**購入確定は押していません** — "
                  "ブラウザで内容を確認し、人が確定してください。Enter で終了 (ブラウザを閉じる)。")
            input()
        finally:
            browser.close()


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


def _vote_url(loc) -> str:
    """投票エントリ (VoteKeibaTop, 確定値)。**ここから 開催→レース→券種 への遷移と
    買い目入力フォームは未確認** = `_add_leg_to_cart` と合わせて要実機調整。
    一旦この TOP へ遷移し、続きのナビゲーション/フォーム HTML を貰って詰める。"""
    return _VOTE_TOP_URL


def _add_leg_to_cart(page, leg: CartLeg) -> None:
    """要実機調整: 1 買い目をフォームに入力してカート投入。式別ごとに組番入力形式が違う。"""
    try:
        page.select_option(SELECTORS["bet_type_select"], label=_SHIKIBETSU[leg.bet_type])
        # 組番入力 (式別で形式が変わる — 要実機調整)。ここでは "-" 連結を一旦入れる。
        page.fill(SELECTORS["umaban_input"], "-".join(map(str, leg.key)))
        page.fill(SELECTORS["amount_input"], str(leg.stake // 100))  # 100円単位
        page.click(SELECTORS["add_to_cart"])
        page.wait_for_timeout(800)
    except Exception as ex:  # noqa: BLE001
        _shot(page, f"addleg_failed_{leg.bet_type}")
        raise OddsparkBetError(
            f"買い目投入失敗 ({leg.bet_type} {leg.key}) — SELECTORS/入力形式を実機調整: {ex}"
        ) from ex


def _main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: ODDSPARK_ID=.. ODDSPARK_PASSWORD=.. "
              "python -m src.oddspark_bet <netkeiba_nar_race_id> [--headless] [--manual-login]")
        raise SystemExit(2)
    rid = args[0]
    try:
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
