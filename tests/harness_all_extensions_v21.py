"""Coordinated stress harness for post-0.20 guarded/auto/CFG refinements."""
from __future__ import annotations

import argparse
import concurrent.futures

from python_extensions import (
    case, enable_goto, enable_switch, fallthrough, inline_calls, inline_function,
    switch, verify_code,
)


@enable_switch(mode="portable", compact_routes="auto")
def _auto_compact(value, out):
    with switch(value):
        if case(1):
            out.append("one")
            fallthrough()
        if case(2):
            out.append("two")
            fallthrough()
        if case():
            try:
                out.append("a")
                out.append("b")
                out.append("c")
            finally:
                out.append("done")
    return len(out)


def _closure_factory():
    @inline_function(register_only=True)
    def add1(value):
        return value + 1

    target = add1

    def set_target(value):
        nonlocal target
        target = value

    @inline_calls(policy="always")
    def caller(value):
        return target(value)

    return caller, set_target, add1


_guarded, _set_guarded, _guarded_original = _closure_factory()


@enable_goto
def _goto(value):
    total = 0
    label .again
    if value <= 0:
        goto .done
    total += value
    value -= 1
    goto .again
    label .done
    return total


def _worker(rounds: int, seed: int) -> int:
    checksum = 0
    for i in range(rounds):
        value = (i + seed) % 5
        out = []
        got = _auto_compact(value, out)
        expected = 6 if value == 1 else 5 if value == 2 else 4
        assert got == expected
        assert _guarded(i) == i + 1
        n = (i + seed) % 8
        assert _goto(n) == n * (n + 1) // 2
        checksum += got + n
    return checksum


def run(profile: str) -> int:
    if profile == "full":
        switch_calls = 1_500_000
        guarded_calls = 1_500_000
        goto_calls = 900_000
        rebound_rounds = 120_000
        thread_rounds = 70_000
    else:
        switch_calls = 40_000
        guarded_calls = 40_000
        goto_calls = 25_000
        rebound_rounds = 4_000
        thread_rounds = 2_000

    calls = 0
    assert _auto_compact.__pyswitch_shared_continuation_plan_count__ == 1
    assert verify_code(_auto_compact.__code__).valid
    for i in range(switch_calls):
        value = i % 5
        out = []
        got = _auto_compact(value, out)
        expected = 6 if value == 1 else 5 if value == 2 else 4
        assert got == expected
    calls += switch_calls

    assert _guarded.__inline_stats__.guarded_closure_calls == 1
    assert verify_code(_guarded.__code__).valid
    for i in range(guarded_calls):
        assert _guarded(i) == i + 1
    calls += guarded_calls

    def alt(value):
        return value + 1000

    for i in range(rebound_rounds):
        if i & 1:
            _set_guarded(_guarded_original)
            expected = i + 1
        else:
            _set_guarded(alt)
            expected = i + 1000
        assert _guarded(i) == expected
    _set_guarded(_guarded_original)
    calls += rebound_rounds

    report = _goto.__python_extensions_report__.as_dict()
    assert report["synthetic_jumps_verified"] == 4
    assert verify_code(_goto.__code__).valid
    for i in range(goto_calls):
        n = i % 11
        assert _goto(n) == n * (n + 1) // 2
    calls += goto_calls

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda seed: _worker(thread_rounds, seed), range(8)))
    assert all(result > 0 for result in results)
    calls += thread_rounds * 8 * 3
    return calls


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    total = run(args.profile)
    print(f"PASS profile={args.profile} calls={total}")
