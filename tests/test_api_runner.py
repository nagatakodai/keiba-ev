"""api/runner.py の Job.seq monotonicity test。

旧実装は `seq = len(self.lines)` で `deque(maxlen=4000)` を使っていたため、
4000 件溢れた後の entry が全て seq=4000 になり、stream の since= 比較が壊れていた。
本 test は seq が単調増加し続けることを保証する (deque eviction 後も)。
"""
from __future__ import annotations

import asyncio


def test_job_seq_is_monotonic_past_deque_maxlen():
    """5000 entries append しても seq が 0..4999 と単調増加する。
    deque maxlen=4000 なので物理 entry は 4000 件だが、seq だけは増え続ける。"""
    from api.runner import Job

    async def run() -> tuple[int, int, int]:
        j = Job("t", "test", ["echo", "x"])
        for _ in range(5000):
            await j._append({"stream": "stdout", "text": "x"})
        return len(j.lines), j.lines[0]["seq"], j.lines[-1]["seq"]

    n_lines, first_seq, last_seq = asyncio.run(run())
    assert n_lines == 4000, f"deque should cap at 4000, got {n_lines}"
    # deque は最初の 1000 件を evict し、残る seq 範囲は 1000..4999
    assert first_seq == 1000, f"first seq should be 1000 after eviction, got {first_seq}"
    assert last_seq == 4999, f"last seq should be 4999 (monotonic), got {last_seq}"


def test_job_seq_unique_across_all_appends():
    """全 entry の seq が unique。"""
    from api.runner import Job

    async def run() -> set[int]:
        j = Job("t", "test", ["echo", "x"])
        for i in range(500):
            await j._append({"stream": "stdout", "text": f"line {i}"})
        return {e["seq"] for e in j.lines}

    seqs = asyncio.run(run())
    assert len(seqs) == 500  # all unique
    assert seqs == set(range(500))


def test_job_stream_filter_uses_seq_correctly():
    """stream の `e["seq"] >= idx` ロジックが正しく動く。
    5000 件 append、since=4500 で stream → 500 件が返る。"""
    from api.runner import Job

    async def run() -> int:
        j = Job("t", "test", ["echo", "x"])
        for _ in range(5000):
            await j._append({"stream": "stdout", "text": "x"})
        # stream を呼ぶには status を更新する必要がある (running 中以外なら 1 周で抜ける)
        j.status = "done"
        count = 0
        async for _e in j.stream(since=4500):
            count += 1
        return count

    n = asyncio.run(run())
    # seq 4500..4999 が deque に残ってる範囲 (1000..4999 のうち 4500..4999) → 500 件
    assert n == 500, f"expected 500 entries since=4500, got {n}"
