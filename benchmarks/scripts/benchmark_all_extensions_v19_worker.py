"""Worker used by benchmark_all_extensions_v19.py.

This file intentionally contains real source-backed decorated functions so
pyswitch can inspect/recompile them in both the baseline and candidate package.
"""
from __future__ import annotations

import argparse
import json
import timeit

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    switch,
)


@enable_switch(mode="portable")
def switch_binary(value, guard=True):
    with switch(value):
        if case(1, 2, 3, 4, 5, 6, 7, 8, when=guard):
            # Keep this control-heavy enough to avoid expression templates.
            try:
                result = value + 10
            finally:
                pass
            return result
        if case():
            try:
                result = value - 10
            finally:
                pass
            return result


@enable_switch(mode="portable")
def switch_direct(value):
    with switch(value):
        if case(0):
            return 11
        if case(1):
            return 12
        if case(2):
            return 13
        if case(3):
            return 14
        if case(4):
            return 15
        if case(5):
            return 16
        if case(6):
            return 17
        if case(7):
            return 18
        if case():
            return -1


@inline_function(register_only=True)
def _neg(value=7):
    return -value


@inline_function(register_only=True)
def _inv(value=7):
    return ~value


@inline_function(register_only=True)
def _logical(value=()):
    return not value


@inline_function(register_only=True)
def _positive(value=-7):
    return +value


@inline_function(register_only=True)
def _dynamic_neg(value):
    return -value


@inline_calls(policy="speed")
def inline_neg():
    return _neg()


@inline_calls(policy="speed")
def inline_inv():
    return _inv()


@inline_calls(policy="speed")
def inline_not():
    return _logical()


@inline_calls(policy="speed")
def inline_pos():
    return _positive()


@inline_calls(policy="speed")
def inline_dynamic(value):
    return _dynamic_neg(value)


@enable_goto
def goto_backward(value):
    total = 0
    label .again
    total += value
    value -= 1
    if value > 0:
        goto .again
    return total


@enable_goto
def goto_forward(flag):
    value = 1
    if flag:
        goto .done
    value += 10
    label .done
    return value


@enable_goto
def goto_control(value):
    return value + 1


def _ns_per_call(stmt: str, loops: int) -> float:
    return timeit.timeit(stmt, globals=globals(), number=loops) / loops * 1e9


def run(loops: int) -> dict[str, object]:
    warm = max(10_000, loops // 20)
    for _ in range(warm):
        switch_binary(4, True)
        switch_binary(99, True)
        switch_direct(4)
        inline_neg(); inline_inv(); inline_not(); inline_pos(); inline_dynamic(7)
        goto_backward(8); goto_forward(True); goto_forward(False); goto_control(7)

    metrics = {
        "switch_binary_hit_ns": _ns_per_call("switch_binary(4, True)", loops),
        "switch_binary_miss_ns": _ns_per_call("switch_binary(99, True)", loops),
        "switch_binary_guard_false_ns": _ns_per_call("switch_binary(4, False)", loops),
        "switch_direct_control_ns": _ns_per_call("switch_direct(4)", loops),
        "inline_neg_ns": _ns_per_call("inline_neg()", loops),
        "inline_inv_ns": _ns_per_call("inline_inv()", loops),
        "inline_not_ns": _ns_per_call("inline_not()", loops),
        "inline_pos_ns": _ns_per_call("inline_pos()", loops),
        "inline_dynamic_control_ns": _ns_per_call("inline_dynamic(7)", loops),
        "goto_backward_ns": _ns_per_call("goto_backward(8)", loops),
        "goto_forward_taken_ns": _ns_per_call("goto_forward(True)", loops),
        "goto_forward_fallthrough_ns": _ns_per_call("goto_forward(False)", loops),
        "goto_marker_free_control_ns": _ns_per_call("goto_control(7)", loops),
    }
    return {
        "metrics": metrics,
        "code_bytes": {
            "switch_binary": len(switch_binary.__code__.co_code),
            "switch_direct": len(switch_direct.__code__.co_code),
            "inline_neg": len(inline_neg.__code__.co_code),
            "inline_inv": len(inline_inv.__code__.co_code),
            "inline_not": len(inline_not.__code__.co_code),
            "inline_pos": len(inline_pos.__code__.co_code),
            "inline_dynamic": len(inline_dynamic.__code__.co_code),
            "goto_backward": len(goto_backward.__code__.co_code),
            "goto_forward": len(goto_forward.__code__.co_code),
            "goto_control": len(goto_control.__code__.co_code),
        },
        "telemetry": {
            "switch_backend": getattr(switch_binary, "__pyswitch_backend__", None),
            "switch_binary_route_plans": getattr(
                switch_binary, "__pyswitch_binary_route_plan_count__", 0
            ),
            "inline_unary_folds": getattr(
                getattr(inline_neg, "__inline_stats__", None),
                "constant_unary_ops_folded",
                0,
            ),
            "goto_early_jump": goto_backward.__python_extensions_report__.as_dict().get(
                "early_jump_lowering", False
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops", type=int, default=300_000)
    args = parser.parse_args()
    print(json.dumps(run(args.loops), sort_keys=True))


if __name__ == "__main__":
    main()
