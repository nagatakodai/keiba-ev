"""オッズパーク半自動投票 scaffold の純粋ロジックテスト (ネット/ブラウザ不要)。

認証情報の env 取得と、誤入力暴走を防ぐ合計賭金ハードリミットを検証する。
"""
from __future__ import annotations

import pytest

from src import oddspark_bet as ob


def test_creds_requires_env(monkeypatch):
    monkeypatch.delenv("ODDSPARK_ID", raising=False)
    monkeypatch.delenv("ODDSPARK_PASSWORD", raising=False)
    with pytest.raises(ob.OddsparkBetError):
        ob._creds()
    monkeypatch.setenv("ODDSPARK_ID", "u")
    monkeypatch.setenv("ODDSPARK_PASSWORD", "p")
    monkeypatch.setenv("ODDSPARK_PIN", "1234")
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
                        lambda rid: ([ob.CartLeg("win", [1], 100)], "X"))
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
                        lambda rid: ([ob.CartLeg("win", [1], 100)], "X"))
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
