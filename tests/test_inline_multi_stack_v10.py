from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def crossing_lifetimes(x):
    a = x + 1
    b = x + 2
    return a * 3 + b * 4 + a + b


@inline_function(register_only=True)
def nested_lifetimes(x):
    a = x + 1
    b = x + 2
    return a * 3 + b * 4 + b + a


@inline_function(register_only=True)
def triple_nested(x):
    # Keep these on one physical source line so CPython 3.13 emits the
    # STORE_FAST_LOAD_FAST superinstruction between assignments.
    a = x + 1; b = x + 2; c = x + 3
    return a * 2 + b * 3 + c * 4 + c + b + a


class TraceNumber:
    def __init__(self, value, trace):
        self.value = value
        self.trace = trace

    def _value(self, other):
        return other.value if isinstance(other, TraceNumber) else other

    def __add__(self, other):
        other = self._value(other)
        self.trace.append(("add", self.value, other))
        return TraceNumber(self.value + other, self.trace)

    def __mul__(self, other):
        other = self._value(other)
        self.trace.append(("mul", self.value, other))
        return TraceNumber(self.value * other, self.trace)


@inline_function(register_only=True)
def nested_overloaded(x):
    a = x + 1
    b = x + 2
    return a * 3 + b * 4 + b + a


def test_crossing_retained_lifetimes_are_not_promoted_together():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return crossing_lifetimes(x)

    for value in range(-100, 101):
        assert caller(value) == crossing_lifetimes(value)
    # Crossing lifetimes are not representable by a pure retained-value stack:
    # the older value would have to die while the newer value is still above it.
    # Keep exactly one of the crossing values local.
    assert caller.__inline_stats__.stack_resident_values == 1
    assert caller.__code__.co_nlocals == 2
    assert verify_code(caller.__code__).valid


def test_two_nested_lifetimes_are_scheduled_together():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return nested_lifetimes(x)

    for value in range(-100, 101):
        assert caller(value) == nested_lifetimes(value)
    # Regression for 0.5.0: scheduling these two values outer-first changed the
    # first COPY depth and nested_lifetimes(10) incorrectly became 107, not 104.
    assert caller(10) == 104
    assert caller.__inline_stats__.stack_resident_values >= 2
    assert caller.__code__.co_varnames == ("x",)
    copies = [
        ins.arg for ins in dis.get_instructions(caller, adaptive=False)
        if ins.opname == "COPY"
    ]
    assert 2 in copies
    assert verify_code(caller.__code__).valid


def test_three_nested_lifetimes_support_fused_store_load_superinstructions():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return triple_nested(x)

    for value in range(-50, 51):
        assert caller(value) == triple_nested(value)
    assert caller.__inline_stats__.stack_resident_values >= 3
    assert caller.__code__.co_varnames == ("x",)
    copies = [
        ins.arg for ins in dis.get_instructions(caller, adaptive=False)
        if ins.opname == "COPY"
    ]
    assert 3 in copies
    assert verify_code(caller.__code__).valid


def test_nested_multi_value_scheduler_preserves_overloaded_order():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return nested_overloaded(x)

    expected_trace = []
    actual_trace = []
    expected = nested_overloaded(TraceNumber(3, expected_trace))
    actual = caller(TraceNumber(3, actual_trace))
    assert actual.value == expected.value
    assert actual_trace == expected_trace
    assert caller.__inline_stats__.stack_resident_values >= 2


def test_nested_stack_residency_is_lifo_not_arbitrary_overlap():
    @inline_calls(policy="always", shared_regions=False)
    def nested(x):
        return nested_lifetimes(x)

    @inline_calls(policy="always", shared_regions=False)
    def crossing(x):
        return crossing_lifetimes(x)

    assert nested.__inline_stats__.stack_resident_values > crossing.__inline_stats__.stack_resident_values
