"""per-race 上限の解決 (_resolve_max_stake): 明示円 > 上限専用倍率 > 掛金倍率連動。"""
from __future__ import annotations

from src.ipat_bet import _resolve_max_stake as resolve_ipat
from src.oddspark_bet import _resolve_max_stake as resolve_odsp


def test_explicit_wins_over_multipliers():
    for fn in (resolve_odsp, resolve_ipat):
        v, _ = fn(12_345, 2.0, 5.0)
        assert v == 12_345          # 明示円が最優先


def test_cap_multiplier_overrides_stake_linkage():
    for fn in (resolve_odsp, resolve_ipat):
        v, src = fn(None, 2.0, 5.0)
        assert v == 50_000          # 基準¥10,000 × 上限倍率5 (掛金倍率2は無視)
        assert "上限倍率" in src


def test_cap_multiplier_below_one_is_respected():
    # 上限専用倍率は明示値なので 1 未満でもそのまま絞れる (掛金倍率連動は max(1,·) で底上げ)
    for fn in (resolve_odsp, resolve_ipat):
        v, _ = fn(None, 1.0, 0.5)
        assert v == 5_000


def test_default_links_to_stake_multiplier():
    for fn in (resolve_odsp, resolve_ipat):
        v, _ = fn(None, 3.0, None)
        assert v == 30_000          # 従来挙動: 基準 × 掛金倍率
        v1, _ = fn(None, 0.5, None)
        assert v1 == 10_000         # 掛金倍率 <1 は底上げ (max(1.0, m))
