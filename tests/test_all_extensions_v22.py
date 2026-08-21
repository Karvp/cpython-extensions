from __future__ import annotations

from functools import partial

from python_extensions import (
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    switch,
    case,
    fallthrough,
)


@inline_function(register_only=True)
def _v22_medium(x, a=3, b=5):
    return x + (a * b) + (a + b)


@inline_function(register_only=True)
def _v22_add(a, b):
    return a + b


def test_guarded_closure_speed_policy_uses_body_optimization_credit():
    target = _v22_medium

    @inline_calls(policy="speed")
    def caller(x):
        return target(x)

    assert caller(10) == 33
    stats = caller.__inline_stats__
    assert stats.guarded_closure_calls == 1
    assert stats.guarded_closure_speed_accepted == 1
    assert stats.guarded_closure_body_credit >= 2
    details = dict(caller.__python_extensions_report__.details)
    assert details["guarded_closure_speed_accepted"] == 1
    assert details["guarded_closure_body_credit"] >= 2


def test_guarded_closure_speed_policy_still_rejects_trivial_body():
    @inline_function(register_only=True)
    def tiny(x):
        return x + 1

    target = tiny

    @inline_calls(policy="speed")
    def caller(x):
        return target(x)

    assert caller(8) == 9
    assert caller.__inline_stats__.guarded_closure_calls == 0
    assert caller.__inline_stats__.guarded_closure_speed_accepted == 0


def test_guarded_bound_method_preserves_rebinding_fallback():
    class Accumulator:
        def __init__(self, base):
            self.base = base

        @inline_function(register_only=True)
        def add(self, value):
            return self.base + value

    owner = Accumulator(10)
    target = owner.add

    def rebind(value):
        nonlocal target
        target = value

    @inline_calls(policy="always")
    def caller(value):
        return target(value)

    assert caller(4) == 14
    assert caller.__inline_stats__.guarded_closure_calls == 1
    rebind(lambda value: value * 10)
    assert caller(4) == 40


def test_guarded_positional_partial_preserves_rebinding_fallback():
    target = partial(_v22_add, 7)

    def rebind(value):
        nonlocal target
        target = value

    @inline_calls(policy="always")
    def caller(value):
        return target(value)

    assert caller(5) == 12
    assert caller.__inline_stats__.guarded_closure_calls == 1
    rebind(lambda value: value - 3)
    assert caller(5) == 2


def test_keyword_partial_is_not_guarded_by_identity_alone():
    target = partial(_v22_add, b=7)

    @inline_calls(policy="always")
    def caller(value):
        return target(value)

    assert caller(5) == 12
    assert caller.__inline_stats__.guarded_closure_calls == 0
    target.keywords["b"] = 11
    assert caller(5) == 16


def test_callable_instance_is_not_guarded_by_object_identity_alone():
    class Callable:
        @inline_function(register_only=True)
        def __call__(self, value):
            return value + 1

    target = Callable()

    @inline_calls(policy="always")
    def caller(value):
        return target(value)

    assert caller(5) == 6
    assert caller.__inline_stats__.guarded_closure_calls == 0
    Callable.__call__ = lambda self, value: value + 20
    assert caller(5) == 25


def test_switch_auto_compaction_reports_bytecode_savings():
    @enable_switch(mode="portable", compact_routes="auto")
    def route(value, out):
        with switch(value):
            if case(1):
                out.append("one")
                fallthrough()
            if case():
                try:
                    out.append("a")
                    out.append("b")
                    out.append("c")
                    out.append("d")
                finally:
                    out.append("done")
        return tuple(out)

    assert route(1, []) == ("one", "a", "b", "c", "d", "done")
    assert route(9, []) == ("a", "b", "c", "d", "done")
    assert route.__pyswitch_auto_compact_plan_count__ == 1
    assert route.__pyswitch_auto_compact_estimated_bytes_saved__ >= 64
    details = dict(route.__python_extensions_report__.details)
    assert details["auto_compact_plans"] == 1
    assert details["auto_compact_estimated_bytes_saved"] >= 64


def test_switch_auto_compaction_fails_closed_for_context_sensitive_tail():
    @enable_switch(mode="portable", compact_routes="auto")
    def route(value):
        out = []
        for _ in range(1):
            with switch(value):
                if case(1):
                    out.append("one")
                    fallthrough()
                if case():
                    out.append("tail")
                    continue
            out.append("unreachable-by-shared-tail")
        return tuple(out)

    assert route(1) == ("one", "tail")
    assert route(9) == ("tail",)
    assert route.__pyswitch_auto_compact_plan_count__ == 0
    assert route.__pyswitch_auto_compact_estimated_bytes_saved__ == 0


def test_goto_reports_exact_synthetic_cfg_proof_and_single_verification_pass():
    @enable_goto
    def route(value):
        if value:
            goto .done
        value = 10
        label .done
        return value

    assert route(True) is True
    assert route(False) == 10
    details = dict(route.__python_extensions_report__.details)
    assert details["synthetic_jumps_verified"] == 2
    assert details["cfg_verification_passes"] == 1


def test_marker_free_goto_reports_one_generic_cfg_verification():
    @enable_goto
    def plain(value):
        return value + 1

    assert plain(3) == 4
    details = dict(plain.__python_extensions_report__.details)
    assert details["synthetic_jumps_verified"] == 0
    assert details["cfg_verification_passes"] == 1
