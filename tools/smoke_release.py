#!/usr/bin/env python3
"""Installed-artifact smoke test; keep definitions in a real source file."""
from __future__ import annotations

import importlib.util

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    hotpath,
    inline_calls,
    inline_function,
    optimize_extensions,
    partial,
    specialize,
    switch,
    verify_code,
)


@enable_switch
def route(value):
    with switch(value):
        if case("read"):
            return 1
        if case("write"):
            return 2
        if case():
            return 0


@enable_switch(mode="fast", live_engine="native")
def live_route(value):
    with switch(value):
        if case(0):
            return 10
        if case(1):
            return 11
        if case(2):
            return 12
        if case():
            return -1


def configured(value, mode="safe"):
    if mode == "fast":
        return value + 10
    return value - 10


specialized_partial = partial(configured, mode="fast")


@specialize(constants={"mode": "fast"}, types={"value": int})
def guarded_specialized(value, mode="safe"):
    if type(value) is int and mode == "fast":
        return value + 1
    return -1


@hotpath(threshold=4, max_variants=1, constants=("mode",), types=False)
def adaptive(value, mode="safe"):
    if mode == "fast":
        return value * 2
    return value


@inline_function(register_only=True)
def helper(value, delta=1):
    return value + delta


@inline_calls(policy="always", binding="guarded")
def guarded(value):
    return helper(value)


@enable_goto
def loop_sum(n):
    total = 0
    label .again
    if n <= 0:
        goto .done
    total += n
    n -= 1
    goto .again
    label .done
    return total


@optimize_extensions(switch=True, inline=False, goto=False)
def composed(value):
    with switch(value):
        if case(1):
            return 11
        if case():
            return -1


def main() -> int:
    # Official release wheels are expected to ship the optional native accelerator.
    assert importlib.util.find_spec("python_extensions._livegate") is not None

    assert route("read") == 1 and route("missing") == 0
    assert live_route(0) == 10 and live_route(2) == 12 and live_route(99) == -1
    assert specialized_partial(5) == 15
    assert guarded_specialized(4, "fast") == 5
    assert guarded_specialized("4", "fast") == -1
    for _ in range(16):
        assert adaptive(3, "fast") == 6

    assert guarded(2) == 3
    helper.__defaults__ = (10,)
    assert guarded(2) == 12  # guarded path must deopt after default mutation
    assert loop_sum(5) == 15
    assert composed(1) == 11 and composed(2) == -1

    for function in (
        route,
        live_route,
        specialized_partial,
        guarded_specialized,
        adaptive,
        guarded,
        loop_sum,
        composed,
    ):
        result = verify_code(function.__code__, raise_on_error=False)
        assert result.valid, (function.__name__, result.errors)
    print("installed artifact smoke: PASS (native live + specialization + inline + goto)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
