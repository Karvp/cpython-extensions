from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from python_extensions import (
    SpecializationLimitError,
    SpecializationUnsupportedError,
    hotpath,
    partial,
    specialize,
    verify_code,
)


def test_partial_removes_bound_parameter_and_folds_branch():
    def route(value, mode="safe"):
        if mode == "fast":
            return value * 4 + 1
        return value * 2 + 3

    fast = partial(route, mode="fast")
    assert str(inspect.signature(fast)) == "(value)"
    assert fast(7) == 29
    assert len(fast.__code__.co_code) < len(route.__code__.co_code)
    verify_code(fast.__code__)
    with pytest.raises(TypeError):
        fast(7, mode="safe")


def test_partial_positional_binding_removes_leading_parameter():
    def affine(scale, value, bias=1):
        return value * scale + bias

    times_eight = partial(affine, 8)
    assert str(inspect.signature(times_eight)) == "(value, bias=1)"
    assert times_eight(5) == 41
    assert times_eight(5, 3) == 43


def test_partial_keyword_only_binding_updates_real_signature():
    def render(value, *, mode="short", suffix="!"):
        return f"{mode}:{value}{suffix}"

    long = partial(render, mode="long")
    assert str(inspect.signature(long)) == "(value, *, suffix='!')"
    assert long("x") == "long:x!"
    assert long("x", suffix="?") == "long:x?"


def test_partial_preserves_bound_parameter_in_locals_and_reassignment():
    def inspect_local(value, mode="safe"):
        before = locals().copy()
        mode = "changed"
        return before, mode, value

    fast = partial(inspect_local, mode="fast")
    before, mode, value = fast(9)
    assert before == {"value": 9, "mode": "fast"}
    assert mode == "changed"
    assert value == 9


def test_partial_preserves_defaults_after_removing_middle_optional_parameter():
    def calculate(x, scale=2, bias=3):
        return x * scale + bias

    fixed_scale = partial(calculate, scale=4)
    assert str(inspect.signature(fixed_scale)) == "(x, bias=3)"
    assert fixed_scale(5) == 23
    assert fixed_scale(5, 7) == 27


def test_partial_handles_positional_only_and_keyword_only_together():
    def function(a, /, b=2, *, c=3):
        return a + b * c

    fixed = partial(function, 10, c=4)
    assert str(inspect.signature(fixed)) == "(b=2)"
    assert fixed() == 18
    assert fixed(5) == 30


def test_partial_rejects_bound_cell_parameter():
    def outer(value):
        def function(mode="fast"):
            def inner():
                return mode
            return value, inner()
        return function

    function = outer(2)
    with pytest.raises(SpecializationUnsupportedError, match="cell parameter"):
        partial(function, mode="fast")



def test_partial_rejects_varkw_when_bound_name_could_leak_into_mapping():
    def function(value, mode="safe", **options):
        return value, mode, options

    with pytest.raises(SpecializationUnsupportedError, match=r"\*\*kwargs"):
        partial(function, mode="fast")

def test_partial_works_on_coroutine_function():
    async def compute(value, mode="slow"):
        await asyncio.sleep(0)
        if mode == "fast":
            return value + 1
        return value - 1

    fast = partial(compute, mode="fast")
    assert inspect.iscoroutinefunction(fast)
    assert asyncio.run(fast(4)) == 5


def test_partial_descriptor_orders():
    class Demo:
        @partial(scale=4)
        @staticmethod
        def a(value, scale=2):
            return value * scale

        @staticmethod
        @partial(scale=5)
        def b(value, scale=2):
            return value * scale

    assert Demo.a(3) == 12
    assert Demo.b(3) == 15


def test_specialize_constant_guard_has_fast_variant_and_fallback():
    @specialize(constants={"mode": "fast"})
    def route(value, mode="safe"):
        if mode == "fast":
            return value * 4 + 1
        return value * 2 + 3

    assert route(5, "fast") == 21
    assert route(5, "safe") == 13
    stats = route.specialization_stats()
    assert stats.variants_created == 1
    assert stats.runtime_metrics is False
    assert route.__python_extensions_dispatch_mode__ == "inline"
    variants = route.specialization_variants()
    assert len(variants) == 1
    assert len(variants[0].__code__.co_code) < len(route.__wrapped__.__code__.co_code)


def test_specialize_exact_type_guard_folds_type_predicate_and_subclass_falls_back():
    class ChildInt(int):
        pass

    @specialize(types={"value": int})
    def classify(value):
        if type(value) is int:
            return value + 1
        return -1

    assert classify(3) == 4
    assert classify(ChildInt(3)) == -1
    variant = classify.specialization_variants()[0]
    assert len(variant.__code__.co_code) < len(classify.__wrapped__.__code__.co_code)


def test_specialize_folds_safe_builtin_isinstance_predicate():
    @specialize(types={"value": str})
    def classify(value):
        if isinstance(value, str):
            return len(value) + 1
        return -1

    assert classify("abc") == 4
    assert classify(12) == -1


def test_specialize_does_not_fold_custom_instancecheck():
    calls = []

    class Meta(type):
        def __instancecheck__(cls, instance):
            calls.append(instance)
            return True

    class Marker(metaclass=Meta):
        pass

    @specialize(types={"value": int}, policy="always")
    def classify(value):
        if isinstance(value, Marker):
            return 1
        return 0

    assert classify(5) == 1
    assert calls == [5]


def test_specialize_unsafe_explicit_constant_guard_is_identity_only():
    class Explosive:
        def __eq__(self, other):
            raise AssertionError("specialization guard invoked user equality")
        __hash__ = None

    marker = Explosive()

    @specialize(constants={"token": marker}, policy="always")
    def function(value, token):
        return value + (1 if token is marker else 2)

    assert function(3, marker) == 4
    assert function(3, Explosive()) == 5


def test_specialize_bare_decorator_can_register_variant_later():
    @specialize
    def function(value, mode="slow"):
        if mode == "fast":
            return value * 3
        return value

    assert function.specialization_stats().variants_created == 0
    variant = function.register_specialization(constants={"mode": "fast"})
    assert variant is not None
    assert function(5, "fast") == 15
    assert function.specialization_stats().variant_hits == 1



def test_specialize_duplicate_registration_reuses_existing_variant():
    @specialize(max_variants=1)
    def function(value, mode="slow"):
        if mode == "fast":
            return value + 1
        return value - 1

    first = function.register_specialization(constants={"mode": "fast"})
    second = function.register_specialization(constants={"mode": "fast"})
    assert first is second
    assert function.specialization_stats().variants_created == 1

def test_specialize_respects_max_variants():
    @specialize(max_variants=1, policy="always")
    def function(value, mode="a"):
        if mode == "a":
            return value + 1
        return value - 1

    function.register_specialization(constants={"mode": "a"})
    with pytest.raises(SpecializationLimitError):
        function.register_specialization(constants={"mode": "b"})



def test_specialize_does_not_pollute_function_module_globals():
    before = set(globals())

    @specialize(constants={"mode": "fast"})
    def function(value, mode="slow"):
        if mode == "fast":
            return value + 1
        return value - 1

    assert function(1, "fast") == 2
    assert set(globals()) == before
    assert not any(name.startswith("__pex_") for name in globals())


def test_explicit_specialize_is_frame_transparent_on_fast_and_fallback_paths():
    import sys

    @specialize(constants={"mode": "fast"})
    def function(mode="slow"):
        if mode == "fast":
            return sys._getframe(1).f_code.co_name
        return sys._getframe(1).f_code.co_name

    def caller(mode):
        return function(mode)

    assert caller("fast") == "caller"
    assert caller("slow") == "caller"


def test_inline_specialize_can_add_multiple_variants_without_wrapper_frames():
    @specialize(constants={"mode": "a"}, max_variants=2)
    def function(value, mode):
        if mode == "a":
            return value + 1
        if mode == "b":
            return value + 2
        return value + 3

    function.register_specialization(constants={"mode": "b"})
    assert function.__python_extensions_dispatch_mode__ == "inline"
    assert function(1, "a") == 2
    assert function(1, "b") == 3
    assert function(1, "c") == 4
    assert len(function.specialization_variants()) == 2


def test_auto_specialize_uses_canonical_inframe_float_guard():
    @specialize(constants={"ratio": 1.5})
    def function(value, ratio=1.0):
        if ratio == 1.5:
            return value + 1
        return value - 1

    assert function.__python_extensions_dispatch_mode__ == "inline"
    assert function(2, 1.5) == 3
    assert function(2, 1.0) == 1
    inline = specialize(constants={"ratio": 1.5}, dispatch="inline")(function.__wrapped__)
    assert inline(3, 1.5) == 4

def test_specialize_preserves_coroutine_function_behavior():
    @specialize(constants={"mode": "fast"})
    async def function(value, mode="slow"):
        await asyncio.sleep(0)
        if mode == "fast":
            return value + 1
        return value - 1

    assert inspect.iscoroutinefunction(function)
    assert asyncio.run(function(5, "fast")) == 6
    assert asyncio.run(function(5, "slow")) == 4


def test_specialize_rejects_generator_dispatcher_without_silent_semantic_change():
    def function(mode="a"):
        yield mode

    with pytest.raises(SpecializationUnsupportedError, match="non-generator"):
        specialize(constants={"mode": "a"})(function)



def test_partial_folds_none_and_safe_membership_branches():
    def none_case(value, mode=None):
        if mode is None:
            return value + 1
        return value - 1

    def membership(value, mode="a"):
        if mode in ("a", "b"):
            return value + 2
        return value - 2

    fixed_none = partial(none_case, mode=None)
    fixed_member = partial(membership, mode="a")
    assert fixed_none(3) == 4
    assert fixed_member(3) == 5
    assert fixed_none.__python_extensions_partial_stats__.constant_branches_folded >= 1
    assert fixed_member.__python_extensions_partial_stats__.constant_expressions_folded >= 1



def test_specialize_wrapper_exposes_variant_report_to_explain_api():
    from python_extensions import explain_extensions

    @specialize(constants={"mode": "fast"})
    def function(value, mode="slow"):
        if mode == "fast":
            return value + 1
        return value - 1

    text = explain_extensions(function)
    assert "specialize" in text
    assert "specialize-dispatch" in text
    assert "constant_branches_folded" in text


def test_hotpath_refreshes_wrapper_report_after_promotion():
    from python_extensions import explain_extensions

    @hotpath(threshold=2, types=False, constants=("mode",), policy="always")
    def function(value, mode="slow"):
        if mode == "fast":
            return value + 1
        return value - 1

    assert "no python_extensions report" in explain_extensions(function)
    function(1, "fast"); function(2, "fast"); function(3, "fast")
    assert "specialize" in explain_extensions(function)

def test_hotpath_infers_none_and_reversed_literal_comparisons():
    @hotpath(threshold=2, types=False, policy="always")
    def none_case(value, mode=None):
        if mode is None:
            return value + 1
        return value - 1

    @hotpath(threshold=2, types=False, policy="always")
    def reversed_case(value, mode="slow"):
        if "fast" == mode:
            return value + 1
        return value - 1

    assert none_case.__python_extensions_hotpath_candidates__["constants"] == ("mode",)
    assert reversed_case.__python_extensions_hotpath_candidates__["constants"] == ("mode",)
    none_case(1, None); none_case(2, None); none_case(3, None)
    reversed_case(1, "fast"); reversed_case(2, "fast"); reversed_case(3, "fast")
    assert none_case.specialization_stats().variants_created == 1
    assert reversed_case.specialization_stats().variants_created == 1

def test_hotpath_infers_literal_comparison_and_promotes_after_threshold():
    @hotpath(threshold=3, types=False, policy="always", metrics=True)
    def function(value, mode="slow"):
        if mode == "fast":
            return value * 4 + 1
        return value * 2 + 3

    assert function.__python_extensions_hotpath_candidates__ == {
        "types": (),
        "constants": ("mode",),
    }
    assert [function(i, "fast") for i in range(6)] == [1, 5, 9, 13, 17, 21]
    stats = function.specialization_stats()
    assert stats.variants_created == 1
    assert stats.variant_hits == 3
    assert stats.fallback_calls == 3


def test_hotpath_infers_exact_type_predicate():
    @hotpath(threshold=2, policy="always", metrics=True)
    def function(value):
        if type(value) is int:
            return value + 1
        return -1

    assert function.__python_extensions_hotpath_candidates__["types"] == ("value",)
    assert function(1) == 2
    assert function(2) == 3
    assert function(3) == 4
    assert function.specialization_stats().variants_created == 1
    assert function.specialization_stats().variant_hits == 1


def test_hotpath_auto_uses_monitoring_for_one_variant_ordinary_function():
    @hotpath(threshold=2, types=False, constants=("mode",), policy="always")
    def function(value, mode):
        if mode == "fast":
            return value + 1
        return value - 1

    assert function.__python_extensions_dispatch_mode__ == "monitoring"
    original_globals = function.__globals__
    assert function(1, "fast") == 2
    assert function(2, "fast") == 3
    assert function(3, "fast") == 4
    assert function.__python_extensions_dispatch_mode__ == "monitoring-inline"
    assert function.__globals__ is original_globals
    assert function.specialization_stats().variants_created == 1
    assert function.specialization_stats().profiling_active is False


def test_hotpath_monitoring_does_not_mutate_original_function_alias():
    def baseline(value, mode):
        if mode == "fast":
            return value + 1
        return value - 1

    original_code = baseline.__code__
    optimized = hotpath(
        threshold=2,
        types=False,
        constants=("mode",),
        policy="always",
        backend="monitoring",
    )(baseline)
    assert optimized is not baseline
    optimized(1, "fast")
    optimized(2, "fast")
    assert optimized.__python_extensions_dispatch_mode__ == "monitoring-inline"
    assert baseline.__code__ is original_code
    assert baseline(5, "slow") == 4


def test_hotpath_monitoring_rejection_restores_exact_original_code():
    def baseline(value):
        return value + 1

    original_code = baseline.__code__
    optimized = hotpath(
        threshold=2,
        types=("value",),
        constants=False,
        policy="speed",
        backend="monitoring",
    )(baseline)
    assert optimized(1) == 2
    assert optimized(2) == 3
    assert optimized.__python_extensions_dispatch_mode__ == "passthrough"
    assert optimized.__code__ is original_code
    assert optimized.specialization_stats().variants_created == 0
    assert optimized.specialization_stats().profiling_active is False


def test_hotpath_polymorphic_auto_falls_back_to_wrapper():
    @hotpath(
        threshold=2,
        max_variants=2,
        types=False,
        constants=("mode",),
        policy="always",
    )
    def function(value, mode):
        if mode == "a":
            return value + 1
        if mode == "b":
            return value + 2
        return value

    assert function.__python_extensions_dispatch_mode__ == "wrapper"


def test_hotpath_monitoring_rejects_detailed_metrics_and_polymorphism():
    def function(value):
        return value

    with pytest.raises(SpecializationUnsupportedError, match="max_variants=1"):
        hotpath(backend="monitoring", max_variants=2)(function)
    with pytest.raises(SpecializationUnsupportedError, match="runtime metrics"):
        hotpath(backend="monitoring", metrics=True)(function)


def test_hotpath_disables_per_call_metrics_by_default():
    @hotpath(threshold=2, types=False, constants=("mode",), policy="always")
    def function(value, mode):
        if mode == "fast":
            return value + 1
        return value - 1

    function(1, "fast")
    function(2, "fast")
    function(3, "fast")
    stats = function.specialization_stats()
    assert stats.runtime_metrics is False
    assert stats.calls == stats.variant_hits == stats.fallback_calls == 0
    assert stats.variants_created == 1
    assert stats.profile_calls == 2


def test_hotpath_speed_policy_rejects_unprofitable_type_only_shape():
    @hotpath(threshold=2, types=("value",), constants=False)
    def identity(value):
        return value

    for _ in range(4):
        assert identity(4) == 4
    stats = identity.specialization_stats()
    assert stats.variants_created == 0
    assert stats.variants_rejected >= 1


def test_hotpath_stops_profiling_after_max_variants():
    @hotpath(threshold=2, max_variants=2, types=False, constants=("mode",), policy="always")
    def function(value, mode):
        if mode == "a":
            return value + 1
        if mode == "b":
            return value + 2
        return value + 3

    for mode in ("a", "a", "b", "b", "c", "c"):
        function(1, mode)
    stats = function.specialization_stats()
    assert stats.variants_created == 2
    assert stats.profiling_active is False


def test_hotpath_mixed_shapes_remain_differentially_correct():
    def baseline(value, mode):
        if mode == "add":
            return value + 10
        if mode == "mul":
            return value * 3
        if mode == "neg":
            return -value
        return value

    optimized = hotpath(
        threshold=3,
        max_variants=3,
        types=False,
        constants=("mode",),
        policy="always",
    )(baseline)

    modes = ("add", "mul", "neg", "other")
    for i in range(200):
        mode = modes[i % len(modes)]
        assert optimized(i, mode) == baseline(i, mode)


def test_hotpath_threaded_calls_and_promotion_are_safe():
    @hotpath(threshold=20, max_variants=2, types=False, constants=("mode",), policy="always")
    def function(value, mode):
        if mode == "fast":
            return value * 2 + 1
        return value - 1

    barrier = threading.Barrier(8)

    def worker(offset):
        barrier.wait()
        total = 0
        for i in range(1000):
            total += function(i + offset, "fast")
        return total

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert all(isinstance(value, int) for value in results)
    assert function.specialization_stats().variants_created == 1


def test_partial_and_specialize_preserve_metadata():
    def function(value: int, mode: str = "slow") -> int:
        """example doc"""
        if mode == "fast":
            return value + 1
        return value - 1

    fixed = partial(function, mode="fast")
    guarded = specialize(constants={"mode": "fast"})(function)
    assert fixed.__name__ == guarded.__name__ == "function"
    assert fixed.__doc__ == guarded.__doc__ == "example doc"
    assert fixed.__annotations__ == {"value": "int", "return": "int"}
    assert guarded.__annotations__ == function.__annotations__


def test_partial_report_and_stats_are_attached():
    def function(value, mode="slow"):
        if mode == "fast":
            return value + 1
        return value - 1

    fixed = partial(function, mode="fast")
    stats = fixed.__python_extensions_partial_stats__
    assert stats.constants_bound == 1
    assert stats.constant_branches_folded >= 1
    assert fixed.__python_extensions_report__.feature == "partial"


def test_optimize_extensions_partial_pipeline():
    from python_extensions import optimize_extensions

    @optimize_extensions(partial={"mode": "fast"})
    def function(value, mode="slow"):
        if mode == "fast":
            return value * 4
        return value

    assert str(inspect.signature(function)) == "(value)"
    assert function(3) == 12
    assert function.__python_extensions_pipeline__ == ("partial",)


def test_optimize_extensions_hotpath_pipeline():
    from python_extensions import optimize_extensions

    @optimize_extensions(hotpath={"threshold": 2, "types": False, "constants": ("mode",), "policy": "always"})
    def function(value, mode="slow"):
        if mode == "fast":
            return value + 1
        return value - 1

    assert function(1, "fast") == 2
    assert function(2, "fast") == 3
    assert function(3, "fast") == 4
    assert function.specialization_stats().variants_created == 1
    assert function.__python_extensions_pipeline__ == ("hotpath",)


def test_optimize_extensions_rejects_two_dynamic_dispatch_layers():
    from python_extensions import optimize_extensions

    with pytest.raises(ValueError, match="alternative final dispatch"):
        optimize_extensions(specialize=True, hotpath=True)


def test_partial_then_inline_composition_specializes_surviving_call():
    from python_extensions import clear_inline_registry, inline_function, optimize_extensions

    clear_inline_registry()

    @inline_function(register_only=True)
    def helper(value):
        return value * 3 + 1

    @optimize_extensions(partial={"mode": "fast"}, inline={"policy": "always"})
    def function(value, mode="slow"):
        if mode == "fast":
            return helper(value)
        return value - 1

    assert str(inspect.signature(function)) == "(value)"
    assert function(5) == 16
    assert function.__python_extensions_pipeline__ == ("partial", "inline")
    assert function.__inline_stats__.calls_inlined == 1
    clear_inline_registry()


def test_goto_then_explicit_specialize_composition():
    from python_extensions import optimize_extensions

    @optimize_extensions(goto=True, specialize={"constants": {"mode": "fast"}})
    def function(value, mode="slow"):
        total = value
        if mode == "fast":
            goto .done
        total -= 10
        label .done
        return total

    assert function(7, "fast") == 7
    assert function(7, "slow") == -3
    assert function.__python_extensions_pipeline__ == ("goto", "specialize")
    assert function.__python_extensions_dispatch_mode__ == "inline"


def test_wrapper_specialize_float_guard_distinguishes_signed_zero_without_user_equality():
    import math

    @specialize(constants={"ratio": -0.0}, dispatch="wrapper", policy="always")
    def function(ratio):
        return math.copysign(1.0, ratio)

    assert function(-0.0) == -1.0
    assert function(+0.0) == 1.0
    stats = function.specialization_stats()
    assert stats.variant_hits == 1
    assert stats.fallback_calls == 1


def test_wrapper_specialize_nan_guard_uses_canonical_bit_shape():
    import struct

    nan_bits = bytes.fromhex("7ff8000000001234")
    first = struct.unpack("!d", nan_bits)[0]
    same_bits = struct.unpack("!d", nan_bits)[0]
    other_bits = struct.unpack("!d", bytes.fromhex("7ff8000000005678"))[0]

    @specialize(constants={"token": first}, dispatch="wrapper", policy="always")
    def function(token):
        return 7 if token != token else 0

    assert function(same_bits) == 7
    assert function(other_bits) == 7
    stats = function.specialization_stats()
    assert stats.variant_hits == 1
    assert stats.fallback_calls == 1


def test_specialize_type_analysis_does_not_observe_custom_metaclass_metadata():
    observed = []

    class Base:
        pass

    class Meta(type):
        def __getattribute__(cls, name):
            if name in {"__mro__", "__qualname__"}:
                observed.append(name)
            return super().__getattribute__(name)

    class Child(Base, metaclass=Meta):
        pass

    @specialize(types={"value": Child}, policy="always")
    def function(value):
        if isinstance(value, Base):
            return 1
        return 0

    assert observed == []
    assert function(Child()) == 1
    assert function(Base()) == 1
    assert observed == []


def test_hotpath_megamorphic_profile_state_is_bounded_and_budgeted():
    @hotpath(
        threshold=100,
        max_variants=2,
        types=False,
        constants=("mode",),
        policy="always",
        max_profiled_shapes=8,
        profile_budget=40,
    )
    def function(value, mode):
        if mode == -1:
            return value + 1
        return value

    for mode in range(80):
        assert function(mode, mode) == mode

    stats = function.specialization_stats()
    assert stats.variants_created == 0
    assert stats.profile_evictions > 0
    assert stats.profile_budget_exhausted is True
    assert stats.profiling_active is False
    assert stats.profile_calls == 40
    assert stats.profiled_shapes <= 8


def test_hotpath_profile_limit_validation():
    def function(value):
        return value

    with pytest.raises(ValueError, match="max_profiled_shapes"):
        hotpath(max_profiled_shapes=0)(function)
    with pytest.raises(ValueError, match="profile_budget"):
        hotpath(profile_budget=0)(function)


def test_hotpath_speed_policy_uses_executed_path_not_dead_code_size():
    namespace: dict[str, object] = {}
    lines = ["def route(value, mode):"]
    for index in range(64):
        lines.append(("    if" if index == 0 else "    elif") + f" mode == 'm{index}':")
        lines.append(f"        return value + {index}")
    lines.extend(["    else:", "        return value - 1"])
    exec("\n".join(lines), namespace)
    route = namespace["route"]

    first = hotpath(
        threshold=2,
        max_variants=1,
        types=False,
        constants=("mode",),
        policy="speed",
        backend="monitoring",
    )(route)
    first(1, "m0"); first(2, "m0")
    assert first.__python_extensions_dispatch_mode__ == "passthrough"
    assert first.specialization_stats().variants_created == 0

    last = hotpath(
        threshold=2,
        max_variants=1,
        types=False,
        constants=("mode",),
        policy="speed",
        backend="monitoring",
    )(route)
    last(1, "m63"); last(2, "m63")
    assert last.__python_extensions_dispatch_mode__ == "monitoring-inline"
    variant = last.specialization_variants()[0]
    stats = variant.__python_extensions_partial_stats__
    assert stats.estimated_executed_instructions_removed is not None
    assert stats.estimated_executed_instructions_removed >= 200


def test_partial_stats_report_known_path_estimate_when_provable():
    def route(value, mode):
        if mode == "a":
            return value + 1
        if mode == "b":
            return value + 2
        return value

    fixed = partial(route, mode="b")
    stats = fixed.__python_extensions_partial_stats__
    assert stats.estimated_original_path_instructions is not None
    assert stats.estimated_specialized_path_instructions is not None
    assert stats.estimated_executed_instructions_removed == (
        stats.estimated_original_path_instructions
        - stats.estimated_specialized_path_instructions
    )


def test_hotpath_without_candidates_is_immediate_passthrough():
    def baseline(value):
        return value * 2 + 1

    optimized = hotpath(baseline)
    assert optimized is not baseline
    assert optimized.__python_extensions_dispatch_mode__ == "passthrough"
    assert optimized.__code__ is baseline.__code__
    stats = optimized.specialization_stats()
    assert stats.profiling_active is False
    assert stats.profile_calls == 0


def test_hotpath_inference_ignores_shadowed_type_and_isinstance():
    namespace = {
        "type": lambda value: int,
        "isinstance": lambda value, expected: True,
    }
    exec(
        "def function(value):\n"
        "    if type(value) is int:\n"
        "        return 1\n"
        "    if isinstance(value, str):\n"
        "        return 2\n"
        "    return 3\n",
        namespace,
    )
    optimized = hotpath(namespace["function"])
    assert optimized.__python_extensions_hotpath_candidates__["types"] == ()
    assert optimized.__python_extensions_dispatch_mode__ == "passthrough"
    assert optimized(5) == namespace["function"](5)


def test_hotpath_monitoring_preserves_closure_cells():
    def outer(offset):
        def route(value, mode):
            if mode == "fast":
                return value + offset
            return value - offset
        return route

    baseline = outer(7)
    optimized = hotpath(
        threshold=2,
        types=False,
        constants=("mode",),
        policy="always",
        backend="monitoring",
    )(baseline)
    assert optimized(10, "fast") == 17
    assert optimized(11, "fast") == 18
    assert optimized.__python_extensions_dispatch_mode__ == "monitoring-inline"
    assert optimized(10, "slow") == 3


def test_hotpath_monitoring_descriptor_orders():
    class Demo:
        @hotpath(threshold=2, types=False, constants=("mode",), policy="always")
        @staticmethod
        def static(value, mode="slow"):
            if mode == "fast":
                return value + 1
            return value - 1

        @classmethod
        @hotpath(threshold=2, types=False, constants=("mode",), policy="always")
        def cls(cls, value, mode="slow"):
            if mode == "fast":
                return cls.__name__, value + 2
            return cls.__name__, value - 2

    assert Demo.static(1, "fast") == 2
    assert Demo.static(2, "fast") == 3
    assert Demo.cls(1, "fast") == ("Demo", 3)
    assert Demo.cls(2, "fast") == ("Demo", 4)


def test_hotpath_monitoring_churn_is_crash_isolated():
    root = Path(__file__).resolve().parents[1]
    source = """
from python_extensions import hotpath
for outer in range(250):
    def route(value, mode, bias=outer):
        if mode == 'a':
            return value + bias
        if mode == 'b':
            return value - bias
        return value
    optimized = hotpath(
        threshold=2,
        types=False,
        constants=('mode',),
        policy='always',
        backend='monitoring',
    )(route)
    assert optimized(3, 'b') == 3 - outer
    assert optimized(4, 'b') == 4 - outer
    for value in range(20):
        assert optimized(value, 'b') == value - outer
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [sys.executable, "-X", "dev", "-W", "error", "-c", source],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
