#!/usr/bin/env python3
"""Small installed-artifact smoke test; keep definitions in a real source file."""
from __future__ import annotations

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    optimize_extensions,
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
    assert route("read") == 1 and route("missing") == 0
    assert guarded(2) == 3
    helper.__defaults__ = (10,)
    assert guarded(2) == 12  # guarded path must deopt after default mutation
    assert loop_sum(5) == 15
    assert composed(1) == 11 and composed(2) == -1
    for function in (route, guarded, loop_sum, composed):
        result = verify_code(function.__code__, raise_on_error=False)
        assert result.valid, (function.__name__, result.errors)
    print("installed artifact smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
