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
