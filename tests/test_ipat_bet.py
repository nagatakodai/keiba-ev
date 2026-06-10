"""IPAT 投票 (src/ipat_bet.py) の純粋ロジック回帰テスト (ブラウザ不要)。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import ipat_bet as ib


def test_bet_type_definitions_exist_for_all_types():
    """券種定義 3 点セットの存在と整合 (2026-06-11 bughunt 第3R の critical 回帰防止)。

    d389b55 の編集が _IPAT_BET_LABEL/_SINGLE_COLUMN_BETS/_ORDERED_BETS の定義ブロックを
    誤って巻き込み削除し、全券種が _add_leg_to_buylist の 1 行目で NameError →
    **IPAT 投票が全停止** + req が .done 消費される regression があった。
    """
    for bt in ("win", "place", "quinella", "wide", "exacta", "trio", "trifecta"):
        assert bt in ib._IPAT_BET_LABEL, bt
        # 単一列か順序付きのどちらか一方に必ず属する
        assert (bt in ib._SINGLE_COLUMN_BETS) != (bt in ib._ORDERED_BETS), bt
    # 全角３ (IPAT の select option ラベルは全角)
    assert ib._IPAT_BET_LABEL["trio"] == "３連複"
    assert ib._IPAT_BET_LABEL["trifecta"] == "３連単"


def test_add_leg_does_not_raise_nameerror():
    """_add_leg_to_buylist が**定義欠落 (NameError) で死なない**ことの smoke。

    MagicMock の page では馬番検証 (誤組番防止) で IpatBetError になるのは正常 —
    NameError (定義欠落) でないことだけを検証する。
    """
    page = MagicMock()
    page.locator.return_value.count.return_value = 0
    page.evaluate.side_effect = RuntimeError("no js")   # _checked_horse_boxes → []
    for bt, key in (("win", [1]), ("place", [2]), ("quinella", [1, 2]),
                    ("wide", [1, 2]), ("trio", [1, 2, 3]),
                    ("exacta", [1, 2]), ("trifecta", [1, 2, 3])):
        leg = ib.CartLeg(bet_type=bt, key=key, stake=300)
        try:
            ib._add_leg_to_buylist(page, leg)
        except NameError as ex:  # 定義欠落 = regression
            pytest.fail(f"{bt}: NameError (券種定義の欠落): {ex}")
        except Exception:  # noqa: BLE001 — mock page では他の失敗は想定内
            pass


def test_session_dead_classifier():
    """セッション/ブラウザ喪失エラーの分類 (req を .done に落とさず daemon fail-fast)。"""
    assert ib._is_session_dead_error(RuntimeError("Page.click: Target closed"))
    assert ib._is_session_dead_error(ib.IpatBetError("セッション切れの可能性"))
    assert not ib._is_session_dead_error(ib.IpatBetError("レース合計 ¥9,999 > 上限"))
