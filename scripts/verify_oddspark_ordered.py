#!/usr/bin/env python
"""馬単/3連単 自動投入を有効化する前の実機検証 (B案・購入確定は絶対に押さない)。

CLAUDE.md の precondition「裏目/マルチ checkbox を OFF にする処理を追加し #buylist で
組数=1 を確認」を実機 DOM で確認する。本番コード (_combo_count / _uncheck_ura_multi /
_assert_combo_delta / _add_leg_to_cart) をそのまま実行して検証する。

**stdin 非依存** (ログインはブラウザ画面で行い、スクリプトはログイン検出をポーリング)。
バックグラウンド起動できるよう input() は使わない。出力とスクショで結果を確認する。

使い方:
    .venv/bin/python -m scripts.verify_oddspark_ordered          # pass1: 検査のみ (無投入)
    .venv/bin/python -m scripts.verify_oddspark_ordered --set    # pass2: 馬単1脚 試投入(未購入)
"""
from __future__ import annotations

import json
import sys

from src import oddspark_bet as ob

LOGIN_WAIT_SEC = 240   # 手動ログインをポーリングで待つ上限


def _dump_betform(page) -> None:
    """賭式まわりの全 input + 賭式行の outerHTML を吐く (裏目/マルチ の真の markup 特定用)。"""
    res = page.evaluate(
        "() => { const inputs = Array.from(document.querySelectorAll('input')).map(cb => ({"
        "  type:cb.type, name:cb.name, id:cb.id, value:cb.value, checked:cb.checked,"
        "  disabled:cb.disabled,"
        "  pTxt:(cb.parentElement?cb.parentElement.innerText:'').replace(/\\s+/g,'').slice(0,16),"
        "  pCls:(cb.parentElement?cb.parentElement.className:'')}));"
        "  const bt = document.querySelector('input[name=betTypeSelect]');"
        "  let html=''; if (bt) { let n=bt; for(let k=0;k<6&&n.parentElement;k++) n=n.parentElement;"
        "    html = n.outerHTML; }"
        "  return {inputs, html}; }")
    print("  全 input 一覧:", flush=True)
    for c in res["inputs"]:
        # 馬番グリッド(name=horse*)等のノイズは除外して賭式まわりだけ
        if c["name"] in ("horse1", "horse2", "horse3") or c["type"] == "hidden":
            continue
        print("   ", json.dumps(c, ensure_ascii=False), flush=True)
    html = (res.get("html") or "").replace("\n", " ")
    print("  賭式行 outerHTML (先頭3500字):", flush=True)
    print(html[:3500], flush=True)
    # 裏目/マルチ の真の markup: 馬単(bt6)/3連単(bt9)/枠単(bt4) セルの outerHTML
    cells = page.evaluate(
        "() => { const out={}; for (const c of ['bt4','bt6','bt9']) {"
        " const e=document.querySelector('.'+c); out[c]=e?e.outerHTML.replace(/\\s+/g,' '):null;"
        " } return out; }")
    for k, v in cells.items():
        print(f"  .{k} outerHTML:", flush=True)
        print("   ", (v or "(なし)")[:900], flush=True)


def main() -> None:
    do_set = "--set" in sys.argv
    from playwright.sync_api import sync_playwright
    ob._SHOT_DIR.mkdir(parents=True, exist_ok=True)
    profile = ob.ROOT / "data" / "cache" / "oddspark_profile"   # 永続プロファイル(ログイン保持)
    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(profile), headless=False, locale="ja-JP",
        viewport={"width": 1280, "height": 1800},
        args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("dialog", ob.safe_dialog_accept)   # 削除/セット confirm を自動承認 (#gotobuy は押さない)

    print("[verify] ブラウザ起動。表示された Chrome でオッズパークにログインしてください ...",
          flush=True)
    page.goto(ob._BASE, wait_until="domcontentloaded")
    logged_in = False
    for _ in range(LOGIN_WAIT_SEC):
        try:
            if page.locator(ob.SELECTORS["logged_in_marker"]).count() > 0:
                logged_in = True
                break
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1000)
    if not logged_in:
        print(f"[verify] {LOGIN_WAIT_SEC}s 以内にログインを検出できず。中止。", flush=True)
        ctx.close()
        pw.stop()
        return

    print("[verify] ログイン検出。まとめ投票画面へ ...", flush=True)
    page.goto(ob._VOTE_TOP_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    try:
        page.click(ob.SELECTORS["matome_link"])
        page.wait_for_timeout(2000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(800)
    try:
        ob._select_payment_opcoin(page)
    except Exception:  # noqa: BLE001
        pass
    ob._shot(page, "verify_matome")

    if "--clear" in sys.argv:
        print("\n===== カートを全削除 (#all a) =====", flush=True)
        before = ob._combo_count(page)
        try:
            da = page.locator(ob.SELECTORS["delete_all"])
            if da.count() > 0:
                da.first.click()
                page.wait_for_timeout(1000)
        except Exception as ex:  # noqa: BLE001
            print(f"  削除失敗: {ex}", flush=True)
        ob._shot(page, "verify_cleared")
        print(f"  組数 {before} -> {ob._combo_count(page)} (0 になれば全削除 OK)", flush=True)
        ctx.close()
        pw.stop()
        print("[verify] 終了。", flush=True)
        return

    print("\n===== pass1: 検査 (無投入) =====", flush=True)
    cc = ob._combo_count(page)
    print(f"  _combo_count(空カート想定) = {cc!r}  "
          f"(数値が取れれば '組数：N通り' 読取 OK。None なら正規表現要修正)", flush=True)
    _dump_betform(page)
    print("  _uncheck_ura_multi(page) 試行後の再ダンプ ↓ (裏目/マルチ checked が false になるか):",
          flush=True)
    ob._uncheck_ura_multi(page)
    _dump_betform(page)

    if do_set:
        print("\n===== pass2: 試投入 (購入はしない) =====", flush=True)
        race_val = page.evaluate(
            "() => { for (const cb of document.querySelectorAll('input[type=checkbox]')) {"
            " if (/^\\d{8}_\\d+_\\d+$/.test(cb.value||'')) return cb.value; } return null; }")

        def _mf():
            return page.evaluate(
                "() => Array.from(document.querySelectorAll('a[id$=\"MultiFlag\"]'))"
                ".map(a=>a.id+':'+a.className)")

        if not race_val:
            print("  発売中レースの checkbox が見つからない (締切後?)。pass2 skip。", flush=True)
        else:
            print(f"  対象レース checkbox value = {race_val}", flush=True)
            ob._ORDERED_BETS_VERIFIED = True   # 検証用に一時許可 (本体は False のまま)
            try:
                ob._select_only_race(page, race_val)
                # --- 観察: 馬単/3連単 を選んだ時の 裏目/マルチ リンクの class ---
                print("  既定 MultiFlag class:", _mf(), flush=True)
                page.check('input#rentan')          # 馬単
                page.wait_for_timeout(300)
                print("  馬単 選択後 MultiFlag class:", _mf(), flush=True)
                page.check('input#sanrentan')       # 3連単
                page.wait_for_timeout(300)
                print("  3連単 選択後 MultiFlag class:", _mf(), flush=True)
                ob._uncheck_ura_multi(page)
                print("  _uncheck_ura_multi 後 MultiFlag class:", _mf(), flush=True)
                # 賭式の試し選択をクリアして本番ループへ
                page.evaluate("document.querySelectorAll('input[name=betTypeSelect]:checked')"
                              ".forEach(c=>{c.checked=false;});")
                for leg in (ob.CartLeg("win", [1], 100),
                            ob.CartLeg("exacta", [1, 2], 100),
                            ob.CartLeg("trifecta", [1, 2, 3], 100)):
                    b = ob._combo_count(page)
                    try:
                        ob._add_leg_to_cart(page, leg, race_val)
                        print(f"  {leg.bet_type} {leg.key}: 組数 {b} -> {ob._combo_count(page)} "
                              f"(期待 +1。馬単+2/3連単+6 なら裏目/マルチが外れていない)", flush=True)
                    except ob.OddsparkBetError as ex:
                        print(f"  {leg.bet_type} {leg.key}: 中止 — {ex}", flush=True)
            except Exception as ex:  # noqa: BLE001
                print(f"  pass2 失敗: {ex}", flush=True)
        ob._shot(page, "verify_buylist")

    hold = 120 if do_set else 30
    print(f"\n[verify] #buylist を目視できるよう {hold}s 開いたままにします。"
          f"**購入確定 (#gotobuy) は押していません**。", flush=True)
    page.wait_for_timeout(hold * 1000)
    ctx.close()
    pw.stop()
    print("[verify] 終了。", flush=True)


if __name__ == "__main__":
    main()
