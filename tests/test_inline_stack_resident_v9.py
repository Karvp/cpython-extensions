from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def stack_binary(x):
    temp = x + 1
    return temp * 2 + temp


@inline_function(register_only=True)
def stack_deep(x):
    temp = x + 1
    return (temp + 2) * (temp + 3) + temp


@inline_function(register_only=True)
def stack_compare(x):
    temp = x + 1
    return temp * 2 < temp


@inline_function(register_only=True)
def stack_subscript(x):
    temp = x + 1
    return {temp: x}[temp]


@inline_function(register_only=True)
def stack_branch(x, flag):
    temp = x + 1
    if flag:
        return temp * 2 + temp
    return temp - 1


class TraceNumber:
    def __init__(self, value, trace):
        self.value = value
        self.trace = trace

    def __add__(self, other):
        other_value = other.value if isinstance(other, TraceNumber) else other
        self.trace.append(("add", self.value, other_value))
        return TraceNumber(self.value + other_value, self.trace)

    def __mul__(self, other):
        other_value = other.value if isinstance(other, TraceNumber) else other
        self.trace.append(("mul", self.value, other_value))
        return TraceNumber(self.value * other_value, self.trace)


@inline_function(register_only=True)
def overloaded_order(x):
    temp = x + 1
    return temp * 2 + temp


def test_repeated_synthetic_value_stays_on_stack():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return stack_binary(x)

    assert caller(10) == 33
    assert caller.__code__.co_varnames == ("x",)
    names = [i.opname for i in dis.get_instructions(caller, adaptive=False)]
    assert "COPY" in names
    assert "SWAP" in names
    assert caller.__inline_stats__.stack_resident_values >= 1
    assert verify_code(caller.__code__).valid


def test_stack_scheduler_uses_deeper_copy_depths():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return stack_deep(x)

    for x in range(-20, 21):
        assert caller(x) == stack_deep(x)
    copies = [i.arg for i in dis.get_instructions(caller, adaptive=False) if i.opname == "COPY"]
    assert 2 in copies
    assert caller.__code__.co_varnames == ("x",)
    assert verify_code(caller.__code__).valid


def test_stack_scheduler_preserves_comparison_order():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return stack_compare(x)

    for x in range(-10, 11):
        assert caller(x) == stack_compare(x)
    # Comparison dispatch did not benchmark as a reliable speed win on 3.13, so
    # the speed-oriented scheduler deliberately leaves it on a fast local.
    assert caller.__inline_stats__.stack_resident_values == 0


def test_stack_scheduler_supports_final_subscript_consumer():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return stack_subscript(x)

    for x in range(-10, 11):
        assert caller(x) == x
    # BINARY_SUBSCR was intentionally excluded after cross-run benchmarks showed
    # no stable speed benefit over CPython 3.13's specialized fast-local path.
    assert caller.__inline_stats__.stack_resident_values == 0
    assert verify_code(caller.__code__).valid


def test_stack_scheduler_preserves_overloaded_operator_order():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return overloaded_order(x)

    expected_trace = []
    actual_trace = []
    expected = overloaded_order(TraceNumber(3, expected_trace))
    actual = caller(TraceNumber(3, actual_trace))
    assert actual.value == expected.value
    assert actual_trace == expected_trace
    assert actual_trace == [("add", 3, 1), ("mul", 4, 2), ("add", 8, 4)]


def test_stack_scheduler_stops_at_control_flow():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x, flag):
        return stack_branch(x, flag)

    for x in range(-5, 6):
        assert caller(x, True) == stack_branch(x, True)
        assert caller(x, False) == stack_branch(x, False)
    assert caller.__inline_stats__.stack_resident_values == 0
    assert verify_code(caller.__code__).valid

@inline_function(register_only=True)
def stack_across_call(x):
    temp = x + 1
    return abs(temp) + temp


def test_stack_scheduler_keeps_value_across_intervening_call():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return stack_across_call(x)

    for x in range(-20, 21):
        assert caller(x) == stack_across_call(x)
    copies = [i.arg for i in dis.get_instructions(caller, adaptive=False) if i.opname == "COPY"]
    assert 3 in copies  # callable + NULL sit above the retained value
    assert caller.__code__.co_varnames == ("x",)
    assert caller.__inline_stats__.stack_resident_values >= 1
    assert verify_code(caller.__code__).valid



def test_stack_scheduler_rejects_extended_copy_depth_for_speed():
    # Exercise the private lowering proof directly: 256 values above the retained
    # temporary would make COPY require EXTENDED_ARG, so the speed pass declines.
    from bytecode import BinaryOp, Instr
    import python_extensions.inline as inline_mod

    name = "__inl_depth_guard"
    items = [Instr("STORE_FAST", name)]
    items.extend(Instr("LOAD_CONST", 0) for _ in range(256))
    items.extend([
        Instr("LOAD_FAST", name),
        Instr("BUILD_TUPLE", 257),
        Instr("LOAD_FAST", name),
        Instr("BINARY_OP", BinaryOp.ADD),
    ])
    rewritten, count = inline_mod._schedule_stack_resident_synthetic_values(
        items, {name}
    )
    assert count == 0
    assert rewritten == items
