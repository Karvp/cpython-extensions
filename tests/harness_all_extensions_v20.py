"""Focused stress harness for the coordinated post-0.19 refinement line."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    fallthrough,
    inline_calls,
    inline_function,
    switch,
    verify_code,
)


@enable_switch(mode="portable", compact_routes=True)
def _shared(value, out):
    with switch(value):
        if case(1):
            out.append("one")
            fallthrough()
        if case(2):
            out.append("two")
            fallthrough()
        if case():
            try:
                out.append("tail")
            finally:
                out.append("done")
    return len(out)


@inline_function(register_only=True)
def _nested(a=3, b=4, c=5):
    return -(a + b + c), ~(a + b), not ((a + c) == b)


@inline_calls(policy="speed")
def _folded():
    return _nested()


@enable_goto
def _goto_loop(value):
    total = 0
    label .again
    if value <= 0:
        goto .done
    total += value
    value -= 1
    goto .again
    label .done
    return total


@enable_goto
async def _async_goto(value):
    total = 0
    try:
        label .again
        if value <= 0:
            goto .done
        await asyncio.sleep(0)
        total += value
        value -= 1
        goto .again
        label .done
    finally:
        total += 0
    return total


def _thread_worker(rounds: int, seed: int) -> int:
    checksum = 0
    for i in range(rounds):
        value = (i + seed) % 5
        out = []
        got = _shared(value, out)
        expected = 4 if value == 1 else 3 if value == 2 else 2
        assert got == expected
        assert _folded() == (-12, -8, True)
        n = (i + seed) % 8
        assert _goto_loop(n) == n * (n + 1) // 2
        checksum += got + n
    return checksum


def run(profile: str) -> int:
    if profile == "full":
        switch_calls = 1_400_000
        inline_calls_n = 1_400_000
        goto_calls = 900_000
        async_calls = 4_000
        thread_rounds = 80_000
    else:
        switch_calls = 50_000
        inline_calls_n = 50_000
        goto_calls = 30_000
        async_calls = 100
        thread_rounds = 2_000

    calls = 0
    assert _shared.__pyswitch_shared_continuation_plan_count__ == 1
    assert _shared.__pyswitch_shared_continuation_statement_count__ >= 1
    assert verify_code(_shared.__code__).valid
    for i in range(switch_calls):
        value = i % 5
        out = []
        got = _shared(value, out)
        expected_out = (
            ["one", "two", "tail", "done"] if value == 1 else
            ["two", "tail", "done"] if value == 2 else
            ["tail", "done"]
        )
        assert out == expected_out
        assert got == len(expected_out)
    calls += switch_calls

    stats = _folded.__inline_stats__
    assert stats.constant_binary_ops_folded >= 4
    assert stats.constant_unary_ops_folded >= 3
    assert stats.constant_comparisons_folded >= 1
    assert verify_code(_folded.__code__).valid
    for _ in range(inline_calls_n):
        assert _folded() == (-12, -8, True)
    calls += inline_calls_n

    goto_report = _goto_loop.__python_extensions_report__.as_dict()
    assert goto_report["marker_units_elided"] > 0
    assert verify_code(_goto_loop.__code__).valid
    for i in range(goto_calls):
        n = i % 11
        assert _goto_loop(n) == n * (n + 1) // 2
    calls += goto_calls

    async def async_rounds():
        nonlocal calls
        for i in range(async_calls):
            n = i % 7
            assert await _async_goto(n) == n * (n + 1) // 2
        calls += async_calls
    asyncio.run(async_rounds())

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda seed: _thread_worker(thread_rounds, seed), range(8)))
    assert all(value > 0 for value in results)
    calls += thread_rounds * 8 * 3

    return calls


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    total = run(args.profile)
    print(f"PASS profile={args.profile} calls={total}")
