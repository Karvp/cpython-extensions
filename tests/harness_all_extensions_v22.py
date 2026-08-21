"""Coordinated 0.22 stress harness for all three extensions."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial

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


@enable_switch(mode="portable", compact_routes="auto")
def _auto_compact(value, seed):
    out = seed
    with switch(value):
        if case(0):
            out += 1
            fallthrough()
        if case():
            try:
                out = (out * 3) + 5
                out ^= 0x55
                out += 11
                out = (out << 1) - 7
            finally:
                out += 2
    return out


@enable_switch(mode="portable", compact_routes="auto")
def _context_decline(value):
    out = 0
    for _ in range(1):
        with switch(value):
            if case(1):
                out += 1
                fallthrough()
            if case():
                out += 10
                continue
        out += 1000
    return out


@inline_function(register_only=True)
def _medium(x, a=3, b=5):
    return x + (a * b) + (a + b)


@inline_function(register_only=True)
def _add(a, b):
    return a + b


def _make_guarded(target, *, policy="always"):
    current = target

    def rebind(value):
        nonlocal current
        current = value

    @inline_calls(policy=policy)
    def caller(value):
        return current(value)

    return caller, rebind


_speed, _set_speed = _make_guarded(_medium, policy="speed")
_rebind, _set_rebind = _make_guarded(_medium)


class _Accumulator:
    def __init__(self, base):
        self.base = base

    @inline_function(register_only=True)
    def add(self, value):
        return self.base + value


_owner = _Accumulator(7)
_method, _set_method = _make_guarded(_owner.add)
_partial, _set_partial = _make_guarded(partial(_add, 9))


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


def _expected_auto(value, seed):
    out = seed
    if value == 0:
        out += 1
    try:
        out = (out * 3) + 5
        out ^= 0x55
        out += 11
        out = (out << 1) - 7
    finally:
        out += 2
    return out


def run(*, full: bool) -> int:
    if full:
        counts = dict(
            switch=1_200_000,
            inline=1_400_000,
            rebind=800_000,
            method=700_000,
            partial=700_000,
            goto=1_200_000,
            decline=600_000,
            threaded=600_000,
            safety=660_000,
        )
    else:
        counts = {key: max(2000, value // 50) for key, value in dict(
            switch=1_200_000,
            inline=1_400_000,
            rebind=800_000,
            method=700_000,
            partial=700_000,
            goto=1_200_000,
            decline=600_000,
            threaded=600_000,
            safety=660_000,
        ).items()}

    assert verify_code(_auto_compact.__code__).valid
    assert _auto_compact.__pyswitch_auto_compact_plan_count__ == 1
    assert _auto_compact.__pyswitch_auto_compact_estimated_bytes_saved__ >= 64
    assert _context_decline.__pyswitch_auto_compact_plan_count__ == 0
    assert _speed.__inline_stats__.guarded_closure_speed_accepted == 1
    assert _speed.__inline_stats__.guarded_closure_body_credit >= 2
    assert _method.__inline_stats__.guarded_closure_calls == 1
    assert _partial.__inline_stats__.guarded_closure_calls == 1
    goto_details = dict(_goto_loop.__python_extensions_report__.details)
    assert goto_details["synthetic_jumps_verified"] == 4
    assert goto_details["cfg_verification_passes"] == 1

    calls = 0
    for i in range(counts["switch"]):
        value = i & 3
        seed = (i % 101) - 50
        assert _auto_compact(value, seed) == _expected_auto(value, seed)
    calls += counts["switch"]

    for i in range(counts["inline"]):
        x = (i % 1001) - 500
        assert _speed(x) == x + 23
    calls += counts["inline"]

    for i in range(counts["rebind"]):
        if (i & 255) == 0:
            _set_rebind(lambda x: x - 17)
        elif (i & 255) == 128:
            _set_rebind(_medium)
        expected = (i - 17) if (i & 255) < 128 else (i + 23)
        assert _rebind(i) == expected
    _set_rebind(_medium)
    calls += counts["rebind"]

    for i in range(counts["method"]):
        assert _method(i) == i + 7
    calls += counts["method"]

    for i in range(counts["partial"]):
        assert _partial(i) == i + 9
    calls += counts["partial"]

    for i in range(counts["goto"]):
        n = i % 12
        assert _goto_loop(n) == n * (n + 1) // 2
    calls += counts["goto"]

    for i in range(counts["decline"]):
        assert _context_decline(i & 1) == (11 if (i & 1) else 10)
    calls += counts["decline"]

    per_thread = counts["threaded"] // 8
    def worker(base):
        subtotal = 0
        for j in range(per_thread):
            x = base + j
            subtotal += _speed(x)
            if _auto_compact(x & 1, x) != _expected_auto(x & 1, x):
                raise AssertionError(x)
        return subtotal
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, [k * per_thread for k in range(8)]))
    calls += per_thread * 8

    # Identity-only guard exclusions: mutable keyword partials and callable
    # instances must stay ordinary while their behavior changes in place.
    target = partial(_add, b=3)
    @inline_calls(policy="always")
    def keyword_partial(x):
        return target(x)
    assert keyword_partial.__inline_stats__.guarded_closure_calls == 0
    for i in range(counts["safety"]):
        if (i & 1023) == 0:
            target.keywords["b"] = 3 if target.keywords["b"] == 4 else 4
        assert keyword_partial(i) == i + target.keywords["b"]
    calls += counts["safety"]

    return calls


if __name__ == "__main__":
    full = "--full" in sys.argv
    calls = run(full=full)
    print(f"0.22 coordinated harness: {calls:,}/{calls:,} calls passed")
