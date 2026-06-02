"""オッズパーク半自動投票 scaffold の純粋ロジックテスト (ネット/ブラウザ不要)。

認証情報の env 取得と、誤入力暴走を防ぐ合計賭金ハードリミットを検証する。
"""
from __future__ import annotations

import pytest

from src import oddspark_bet as ob


def test_creds_requires_env(monkeypatch):
    # .env を load_dotenv で読むため新名 (ODDS_PARK_*) + 旧名 (ODDSPARK_*) を両方クリア。
    for k in ("ODDS_PARK_ID", "ODDS_PARK_PASSWORD", "ODDS_PARK_PIN",
              "ODDSPARK_ID", "ODDSPARK_PASSWORD", "ODDSPARK_PIN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ob.OddsparkBetError):
        ob._creds()
    monkeypatch.setenv("ODDS_PARK_ID", "u")
    monkeypatch.setenv("ODDS_PARK_PASSWORD", "p")
    monkeypatch.setenv("ODDS_PARK_PIN", "1234")
    c = ob._creds()
    assert c == {"id": "u", "password": "p", "pin": "1234"}


def test_fill_cart_rejects_over_limit():
    """合計賭金が上限超過なら、ネット/ブラウザに触れる前に中止 (誤入力の暴走防止)。"""
    legs = [ob.CartLeg("wide", [2, 11], 6000), ob.CartLeg("win", [7], 6000)]  # 計 12000
    with pytest.raises(ob.OddsparkBetError, match="上限"):
        ob.fill_cart("202650052705", legs, max_total_stake=10_000)


def test_shikibetsu_covers_all_bet_types():
    for bt in ("win", "place", "quinella", "wide", "exacta", "trio", "trifecta"):
        assert bt in ob._SHIKIBETSU


def test_vote_jo_code_maps_venue():
    # netkeiba rid → 場名 → 投票 joCode (オッズ側 opTrackCd とは別 namespace)
    assert ob._vote_jo_code("202650052705") == "51"   # 園田(netkeiba50) → vote 51
    assert ob._vote_jo_code("202635052601") == "11"   # 盛岡(netkeiba35) → vote 11
    assert ob._vote_jo_code("202699052601") is None    # 未対応コード → None


def test_race_meta_builds_checkbox_value():
    # netkeiba rid → まとめ画面のレース選択 checkbox value (YYYYMMDD_joCode_raceNo非0詰め)
    assert ob._race_meta("202650052709") == "20260527_51_9"   # 園田9R
    assert ob._race_meta("202647052712") == "20260527_42_12"  # 笠松12R


def test_race_meta_rejects_non_12digit():
    # 内部 race_id を誤って渡しても int('-5') でクラッシュせず明示エラー
    for bad in ("2026500527-527-9", "20265005270", "abcd"):
        with pytest.raises(ob.OddsparkBetError, match="形式不正"):
            ob._race_meta(bad)


def test_to_netkeiba_rid_accepts_both_forms():
    assert ob._to_netkeiba_rid("202650052709") == "202650052709"
    assert ob._to_netkeiba_rid("2026500527-527-9") == "202650052709"
    with pytest.raises(ob.OddsparkBetError, match="形式不正"):
        ob._to_netkeiba_rid("nope")


# --- 常駐 daemon の queue 処理 状態遷移 (_process_bet_queue_once) ---

class _FakeSession:
    """BettingSession の add_race だけ差し替えるフェイク (ブラウザ不要)。"""
    def __init__(self, behavior):
        self.behavior = behavior   # rid -> "ok" / "dup" / OddsparkBetError / Exception
        self.calls = []

    def add_race(self, rid, legs, label=""):
        self.calls.append(rid)
        b = self.behavior.get(rid, "ok")
        if isinstance(b, type) and issubclass(b, Exception):
            raise b("boom")
        if b == "dup":
            return ("dup", 0)
        return ("ok", len(legs))


def _put_req(qdir, rid):
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / f"{rid}.req").write_text("{}", encoding="utf-8")


def test_queue_success_and_terminal_error_mark_done(tmp_path, monkeypatch):
    qdir = tmp_path / "q"
    monkeypatch.setattr(ob, "QUEUE_DIR", qdir)
    monkeypatch.setattr(ob, "_legs_from_snapshot",
                        lambda rid, source_override=None: ([ob.CartLeg("win", [1], 100)], "X"))
    for rid in ("202650052701", "202650052702", "202650052703"):
        _put_req(qdir, rid)
    sess = _FakeSession({
        "202650052701": "ok",
        "202650052702": "dup",
        "202650052703": ob.OddsparkBetError,   # 締切等 = 確定エラー
    })
    ob._process_bet_queue_once(sess, {})
    # 成功/dup/確定エラー すべて .done (再投入されない)
    for rid in ("202650052701", "202650052702", "202650052703"):
        assert (qdir / f"{rid}.done").exists(), rid
        assert not (qdir / f"{rid}.req").exists(), rid


def test_queue_transient_error_retries_then_gives_up(tmp_path, monkeypatch):
    qdir = tmp_path / "q"
    monkeypatch.setattr(ob, "QUEUE_DIR", qdir)
    monkeypatch.setattr(ob, "_legs_from_snapshot",
                        lambda rid, source_override=None: ([ob.CartLeg("win", [1], 100)], "X"))
    rid = "202650052705"
    _put_req(qdir, rid)
    sess = _FakeSession({rid: RuntimeError})   # 一過性 glitch を模す
    attempts: dict = {}
    # 1,2 回目: .req 残置で再試行継続
    ob._process_bet_queue_once(sess, attempts, max_attempts=3)
    assert (qdir / f"{rid}.req").exists() and attempts[rid] == 1
    ob._process_bet_queue_once(sess, attempts, max_attempts=3)
    assert (qdir / f"{rid}.req").exists() and attempts[rid] == 2
    # 3 回目: 上限到達 → .done
    ob._process_bet_queue_once(sess, attempts, max_attempts=3)
    assert (qdir / f"{rid}.done").exists() and not (qdir / f"{rid}.req").exists()
    assert sess.calls.count(rid) == 3


# --- BettingSession.add_race の累計露出トラッキング (helper を no-op 化, ブラウザ不要) ---

def test_add_race_tracks_session_staked(monkeypatch):
    monkeypatch.setattr(ob, "_race_meta", lambda r: "20260527_51_9")
    monkeypatch.setattr(ob, "_select_only_race", lambda p, rv: None)
    monkeypatch.setattr(ob, "_add_leg_to_cart", lambda p, leg, rv: None)   # 全脚成功
    monkeypatch.setattr(ob, "_shot", lambda p, n: None)
    sess = ob.BettingSession(headful=False, manual_login=True)
    sess.page = object()
    st, ok = sess.add_race("202650052709",
                           [ob.CartLeg("wide", [4, 10], 600), ob.CartLeg("win", [7], 400)])
    assert (st, ok) == ("ok", 2) and sess._session_staked == 1000
    sess.add_race("202650052710", [ob.CartLeg("win", [1], 500)])
    assert sess._session_staked == 1500
    # dup は再加算しない
    assert sess.add_race("202650052709", [ob.CartLeg("win", [7], 400)]) == ("dup", 0)
    assert sess._session_staked == 1500


def test_add_race_failed_leg_not_counted(monkeypatch):
    monkeypatch.setattr(ob, "_race_meta", lambda r: "20260527_51_9")
    monkeypatch.setattr(ob, "_select_only_race", lambda p, rv: None)
    monkeypatch.setattr(ob, "_shot", lambda p, n: None)

    def fake_add(p, leg, rv):
        if list(leg.key) == [3, 10]:
            raise ob.OddsparkBetError("boom")   # 1脚だけ失敗

    monkeypatch.setattr(ob, "_add_leg_to_cart", fake_add)
    sess = ob.BettingSession(headful=False, manual_login=True)
    sess.page = object()
    st, ok = sess.add_race("202650052709",
                           [ob.CartLeg("wide", [4, 10], 600), ob.CartLeg("wide", [3, 10], 600)])
    assert ok == 1 and sess._session_staked == 600   # 失敗脚 600 は露出に含めない


def test_ordered_bets_enabled_after_verification():
    """馬単/3連単 は実機検証済 (2026-05-28 笠松1R: 組数+1) なので投入可。

    過剰投入 (裏目/マルチ の +2/+6) は _add_leg_to_cart 内の _assert_combo_delta が
    捕捉して当該脚を中止する (別テスト test_assert_combo_delta_* で検証済)。
    """
    assert ob._ORDERED_BETS_VERIFIED is True
    # フラグ True なら page に触れる前の "見送り" 早期 raise はしない (page=None なら
    # 賭式選択時に AttributeError 等。"見送り" メッセージでないことだけ確認)。
    for bt in ("exacta", "trifecta"):
        with pytest.raises(Exception) as ei:
            ob._add_leg_to_cart(None, ob.CartLeg(bt, [1, 2, 3], 100), "20260527_51_9")
        assert "見送り" not in str(ei.value)


# --- 組数 (combination count) ガード: 裏目/マルチ・馬番累積の過剰投入を捕捉 ---

class _FakeEvalPage:
    """evaluate(js) が固定値を返すだけのフェイク page (組数読取テスト用)。"""
    def __init__(self, ret):
        self._ret = ret

    def evaluate(self, *args):
        return self._ret


def test_combo_count_parses_or_none():
    # "組数：N通り" を数値抽出 (要素 id 非依存)
    assert ob._combo_count(_FakeEvalPage("8")) == 8
    assert ob._combo_count(_FakeEvalPage("1,200")) == 1200
    assert ob._combo_count(_FakeEvalPage(None)) is None   # マッチ無し
    # evaluate が例外でも None (ブラウザ未対応/未到達)
    class _Boom:
        def evaluate(self, *a):
            raise RuntimeError("no js")
    assert ob._combo_count(_Boom()) is None


def test_assert_combo_delta_rejects_over_injection(monkeypatch):
    # 1 脚 = 必ず +1 組。+2 (裏目) や +6 (マルチ) は raise して中止
    monkeypatch.setattr(ob, "_combo_count", lambda p: 6)   # after=6
    with pytest.raises(ob.OddsparkBetError, match="組数が想定外"):
        ob._assert_combo_delta(object(), 0, ob.CartLeg("trifecta", [1, 2, 3], 100))
    # ちょうど +1 なら通過
    monkeypatch.setattr(ob, "_combo_count", lambda p: 1)
    ob._assert_combo_delta(object(), 0, ob.CartLeg("wide", [1, 2], 100))


def test_assert_combo_delta_reread_on_async_delay(monkeypatch):
    """組数更新が遅延 (after==before) なら一度待って再読し、+1 になれば通過 (誤中止しない)。"""
    seq = iter([0, 1])   # 1回目=未更新(0), 再読=1
    monkeypatch.setattr(ob, "_combo_count", lambda p: next(seq))

    class _P:
        def wait_for_timeout(self, ms):  # 再読前の待機 (no-op)
            pass
    ob._assert_combo_delta(_P(), 0, ob.CartLeg("win", [1], 100))   # raise しない


def test_assert_combo_delta_unreadable_ordered_aborts_unordered_continues(monkeypatch):
    # 組数を読めない時: 順序付きは安全側で中止、順不同は続行 (既存フロー維持)
    monkeypatch.setattr(ob, "_combo_count", lambda p: None)   # after 読めず
    with pytest.raises(ob.OddsparkBetError, match="安全側で中止"):
        ob._assert_combo_delta(object(), None, ob.CartLeg("exacta", [1, 2], 100))
    ob._assert_combo_delta(object(), None, ob.CartLeg("win", [1], 100))   # 順不同は OK


def test_uncheck_ura_multi_best_effort_no_throw():
    # evaluate が失敗してもスローしない (best-effort、組数検証が最終ゲート)
    class _Boom:
        def evaluate(self, *a):
            raise RuntimeError("x")
    ob._uncheck_ura_multi(_Boom())   # 例外を投げないこと


def test_today_jst_format():
    """JST 日付キー が "YYYY-MM-DD" 形式 (日跨ぎで日次累計リセットのため)。"""
    s = ob._today_jst()
    assert len(s) == 10 and s[4] == "-" and s[7] == "-"
    int(s[:4]); int(s[5:7]); int(s[8:10])   # parse OK


def test_daily_stake_record_and_read(monkeypatch, tmp_path):
    """record_daily_stake が累計、get_today_stake で読み戻し、日付キー別に独立。"""
    monkeypatch.setattr(ob, "DAILY_STAKE_FILE", tmp_path / "daily.json")
    assert ob.get_today_stake() == 0           # 初期は 0
    ob.record_daily_stake(300)
    ob.record_daily_stake(700)
    assert ob.get_today_stake() == 1000
    # 別日付のキーは独立 (= 日跨ぎで自動 reset)
    monkeypatch.setattr(ob, "_today_jst", lambda: "2099-12-31")
    assert ob.get_today_stake() == 0


def test_check_daily_cap_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(ob, "DAILY_STAKE_FILE", tmp_path / "daily.json")
    # 上限無効化 (0以下) は常に allowed
    allowed, _ = ob.check_daily_cap(1_000_000, 0)
    assert allowed
    # 範囲内
    ob.record_daily_stake(10_000)
    allowed, msg = ob.check_daily_cap(5_000, 50_000)
    assert allowed and "15,000" in msg
    # 超過
    allowed, msg = ob.check_daily_cap(45_000, 50_000)
    assert not allowed and "超過" in msg


def test_confirm_purchase_skipped_when_flag_disabled(monkeypatch, tmp_path):
    """AUTO_PURCHASE_VERIFIED=False の間は実弾を撃たず "skipped" を返す (fail-safe)。

    実機検証後は True が既定だが、緊急時に False に戻したら fail-safe で実弾を止める
    挙動を確認 (flag 復元の保険テスト)。
    """
    monkeypatch.setattr(ob, "DAILY_STAKE_FILE", tmp_path / "daily.json")
    monkeypatch.setattr(ob, "AUTO_PURCHASE_VERIFIED", False)
    sess = ob.BettingSession(headful=False, auto_purchase=True, daily_cap=50_000)
    sess.page = object()    # page には触れない (skipped で抜けるため)
    status, msg = sess._confirm_purchase(race_stake=300)
    assert status == "skipped" and "AUTO_PURCHASE_VERIFIED" in msg
    # daily_stake は加算されていない
    assert ob.get_today_stake() == 0


def test_auto_purchase_verified_default_true():
    """実機 DOM 検証後の既定値は True (確定ボタン id=#buy が確認済のため)。"""
    assert ob.AUTO_PURCHASE_VERIFIED is True
    # 確定ボタンの主要セレクタが #buy であること (フォールバックは別途維持)
    assert ob.SELECTORS["confirm_final_candidates"][0] == "#buy"


def test_confirm_purchase_skipped_when_daily_cap_exceeded(monkeypatch, tmp_path):
    """daily_cap 超過時は実弾を撃たず "skipped" を返し、daily_stake も加算しない。"""
    monkeypatch.setattr(ob, "DAILY_STAKE_FILE", tmp_path / "daily.json")
    monkeypatch.setattr(ob, "AUTO_PURCHASE_VERIFIED", True)   # フラグ ON でも cap でガード
    ob.record_daily_stake(49_500)
    sess = ob.BettingSession(headful=False, auto_purchase=True, daily_cap=50_000)
    sess.page = object()
    status, msg = sess._confirm_purchase(race_stake=1_000)
    assert status == "skipped" and "超過" in msg
    assert ob.get_today_stake() == 49_500   # 加算されていない


def test_payment_method_validation():
    """payment_method=opcoin | buylimit | (不正値 → opcoin に fallback)。"""
    assert ob.BettingSession(headful=False, payment_method="opcoin").payment_method == "opcoin"
    assert ob.BettingSession(headful=False, payment_method="buylimit").payment_method == "buylimit"
    # 不正値は既定 (opcoin) に倒れる
    assert ob.BettingSession(headful=False, payment_method="invalid").payment_method == "opcoin"


def test_select_payment_method_dispatches_to_right_selector():
    """_select_payment_method が method に応じて opcoin/buylimit の radio を check する。"""
    seen = []

    class _P:
        def check(self, sel):
            seen.append(sel)
    ob._select_payment_method(_P(), "opcoin")
    assert seen[-1] == ob.SELECTORS["payment_opcoin"]   # #paymentMethodOpCoin
    ob._select_payment_method(_P(), "buylimit")
    assert seen[-1] == ob.SELECTORS["payment_buylimit"]  # #paymentMethodBuyLimit


def test_select_payment_method_raises_when_radio_unchecked():
    """is_checked() が False を返したら OddsparkBetError を raise (silent fallback 防止)。"""
    class _Loc:
        def __init__(self, checked): self._checked = checked
        def count(self): return 1
        @property
        def first(self): return self
        def is_checked(self): return self._checked

    class _PageUnchecked:
        def check(self, sel): pass     # check() 自体は通る
        def locator(self, sel): return _Loc(False)   # でも実際は未選択

    with pytest.raises(ob.OddsparkBetError, match="radio が選択されていない"):
        ob._select_payment_method(_PageUnchecked(), "buylimit")


def test_select_payment_method_silent_when_radio_absent():
    """radio が画面に無い (count=0、まだ matome に遷移してない 等) なら黙って続行。"""
    class _Loc:
        def count(self): return 0
        @property
        def first(self): return self
        def is_checked(self): return False

    class _PageAbsent:
        def check(self, sel): pass
        def locator(self, sel): return _Loc()
    # 例外を投げず素通りすること
    ob._select_payment_method(_PageAbsent(), "opcoin")


def test_apply_stake_multiplier_basic():
    """stake_multiplier=N で各 leg の stake が N 倍 (100円単位丸め)、key/bet_type は不変。"""
    legs = [
        ob.CartLeg("wide", [2, 11], 600),
        ob.CartLeg("win", [7], 300),
        ob.CartLeg("exacta", [1, 2], 100),
    ]
    out = ob._apply_stake_multiplier(legs, 2.0)
    assert [l.stake for l in out] == [1200, 600, 200]
    # 元の leg リストは変更しない (新リストを返す)
    assert [l.stake for l in legs] == [600, 300, 100]
    # key/bet_type は保存
    assert out[0].bet_type == "wide" and out[0].key == [2, 11]


def test_apply_stake_multiplier_identity_when_one():
    """multiplier=1.0 は no-op (引数をそのまま返す)。"""
    legs = [ob.CartLeg("win", [1], 500)]
    assert ob._apply_stake_multiplier(legs, 1.0) is legs


def test_apply_stake_multiplier_min_100():
    """微小な multiplier でも最低 100 円 (1 円賭けは不可)。"""
    out = ob._apply_stake_multiplier([ob.CartLeg("win", [1], 100)], 0.1)
    assert out[0].stake == 100   # 10 にはしない


def test_apply_stake_multiplier_rounds_to_100():
    """100 円単位に四捨五入丸め (150 → 200、149 → 100)。"""
    # 100 * 1.5 = 150 → round(1.5)*100 = 200
    assert ob._apply_stake_multiplier([ob.CartLeg("win", [1], 100)], 1.5)[0].stake == 200
    # 100 * 1.49 = 149 → round(1.49)*100 = 100
    assert ob._apply_stake_multiplier([ob.CartLeg("win", [1], 100)], 1.49)[0].stake == 100


def test_add_race_respects_stake_multiplier(monkeypatch):
    """BettingSession.stake_multiplier が add_race の cap 判定に反映される。"""
    monkeypatch.setattr(ob, "_race_meta", lambda r: "20260527_51_9")
    monkeypatch.setattr(ob, "_select_only_race", lambda p, rv: None)
    monkeypatch.setattr(ob, "_add_leg_to_cart", lambda p, leg, rv: None)
    monkeypatch.setattr(ob, "_shot", lambda p, n: None)
    sess = ob.BettingSession(headful=False, manual_login=True,
                             max_total_stake=10_000, stake_multiplier=2.0)
    sess.page = object()
    # 合計 4000 だが 2倍で 8000 → 範囲内、投入成功
    legs = [ob.CartLeg("wide", [2, 11], 1500), ob.CartLeg("win", [7], 2500)]
    st, ok = sess.add_race("202650052709", legs)
    assert (st, ok) == ("ok", 2)
    assert sess._session_staked == 8000   # 2 倍適用後 (3000 + 5000)
    # 合計 6000 だが 2倍で 12000 → 上限超過で reject
    sess2 = ob.BettingSession(headful=False, manual_login=True,
                              max_total_stake=10_000, stake_multiplier=2.0)
    sess2.page = object()
    with pytest.raises(ob.OddsparkBetError, match="上限"):
        sess2.add_race("202650052710", [ob.CartLeg("wide", [1, 2], 6000)])


def test_confirm_purchase_skipped_when_auto_purchase_off():
    """auto_purchase=False は実弾を撃たない (半自動)。"""
    sess = ob.BettingSession(headful=False, auto_purchase=False)
    sess.page = object()
    status, _ = sess._confirm_purchase(race_stake=500)
    assert status == "skipped"


def test_safe_dialog_accept_swallows_timing_race():
    """Playwright dialog の timing race ("No dialog is showing") を握りつぶす。"""
    class _Closed:
        def accept(self):
            raise RuntimeError("Dialog.accept: No dialog is showing")
    ob.safe_dialog_accept(_Closed())   # 上に伝播してログを汚さないこと

    # 正常系は accept() が呼ばれて完了
    called = []
    class _Open:
        def accept(self):
            called.append(True)
    ob.safe_dialog_accept(_Open())
    assert called == [True]
