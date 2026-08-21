from __future__ import annotations

import json
import statistics
import timeit

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    fallthrough,
    inline_calls,
    inline_function,
    switch,
)


@inline_function(register_only=True)
def _medium(x, a=3, b=5):
    return x + (a * b) + (a + b)


def _medium_factory():
    target = _medium
    @inline_calls(policy="speed")
    def caller(x):
        return target(x)
    return caller


@inline_function(register_only=True)
def _tiny(x):
    return x + 1


def _tiny_factory():
    target = _tiny
    @inline_calls(policy="speed")
    def caller(x):
        return target(x)
    return caller


_medium_caller = _medium_factory()
_tiny_caller = _tiny_factory()


@enable_switch(mode="portable", compact_routes="auto")
def _auto(value, out):
    with switch(value):
        if case(1):
            out.append("one")
            fallthrough()
        if case():
            out.extend(range(8))
            out.reverse()
    return len(out)


@enable_switch(mode="portable")
def _switch_control(value):
    with switch(value):
        if case(1):
            return 10
        if case(2):
            return 20
        if case():
            return -1


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


def _raw_goto(value):
    total = 0
    label .again
    if value <= 0:
        goto .done
    total += value
    value -= 1
    goto .again
    label .done
    return total


def _median_ns(stmt: str, number: int, repeat: int = 2) -> float:
    values = timeit.repeat(stmt, globals=globals(), number=number, repeat=repeat)
    return statistics.median(values) * 1e9 / number


def main():
    results = {
        "inline_body_aware": _median_ns("_medium_caller(123)", 100_000),
        "inline_trivial_control": _median_ns("_tiny_caller(123)", 100_000),
        "switch_auto_hit": _median_ns("_auto(1, [])", 40_000),
        "switch_auto_miss": _median_ns("_auto(9, [])", 40_000),
        "switch_default_control": _median_ns("_switch_control(2)", 100_000),
        "goto_runtime_control": _median_ns("_goto_loop(8)", 30_000),
        "goto_decoration": _median_ns("enable_goto(_raw_goto)", 200, repeat=2),
    }
    meta = {
        "medium_inlined": getattr(_medium_caller.__inline_stats__, "guarded_closure_calls", 0),
        "medium_body_credit": getattr(_medium_caller.__inline_stats__, "guarded_closure_body_credit", 0),
        "tiny_inlined": getattr(_tiny_caller.__inline_stats__, "guarded_closure_calls", 0),
        "switch_code_bytes": len(_auto.__code__.co_code),
        "switch_shared_plans": getattr(_auto, "__pyswitch_shared_continuation_plan_count__", 0),
        "auto_compact_plans": getattr(_auto, "__pyswitch_auto_compact_plan_count__", None),
        "auto_estimated_bytes_saved": getattr(_auto, "__pyswitch_auto_compact_estimated_bytes_saved__", None),
        "goto_code_bytes": len(_goto_loop.__code__.co_code),
    }
    print(json.dumps({"results_ns": results, "meta": meta}, sort_keys=True))


if __name__ == "__main__":
    main()
