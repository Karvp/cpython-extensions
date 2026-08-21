from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def two_vs_one_conflict(x):
    # a crosses both b and c, while b contains c. A lexical/oldest-first allocator
    # keeps only a; the weighted conflict solver can retain b+c and spill a.
    a = x + 1; b = x + 2; c = x + 3
    return a * 2 + b * 3 + c * 4 + a + c + b


@inline_function(register_only=True)
def all_nested(x):
    a = x + 1; b = x + 2; c = x + 3
    return a * 2 + b * 3 + c * 4 + c + b + a


class TraceNumber:
    def __init__(self, value, trace):
        self.value = value
        self.trace = trace

    def _v(self, other):
        return other.value if isinstance(other, TraceNumber) else other

    def __add__(self, other):
        other = self._v(other)
        self.trace.append(("add", self.value, other))
        return TraceNumber(self.value + other, self.trace)

    def __mul__(self, other):
        other = self._v(other)
        self.trace.append(("mul", self.value, other))
        return TraceNumber(self.value * other, self.trace)


@inline_function(register_only=True)
def overloaded_conflict(x):
    a = x + 1; b = x + 2; c = x + 3
    return a * 2 + b * 3 + c * 4 + a + c + b


def test_selective_spill_keeps_two_compatible_values_instead_of_one_oldest():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return two_vs_one_conflict(x)

    for value in range(-100, 101):
        assert caller(value) == two_vs_one_conflict(value)
    stats = caller.__inline_stats__
    assert stats.stack_scheduler_candidates == 3
    assert stats.stack_resident_values == 2
    assert stats.stack_spilled_values == 1
    assert stats.stack_crossing_conflicts == 2
    assert stats.stack_max_copy_depth >= 2
    # The long crossing value a is the deliberate spill; b and c stay on stack.
    assert caller.__code__.co_nlocals == 2
    assert any(name.endswith("_a") for name in caller.__code__.co_varnames)
    assert not any(name.endswith("_b") for name in caller.__code__.co_varnames)
    assert not any(name.endswith("_c") for name in caller.__code__.co_varnames)
    assert verify_code(caller.__code__).valid


def test_nested_dependency_dag_has_no_spills():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return all_nested(x)

    for value in range(-50, 51):
        assert caller(value) == all_nested(value)
    stats = caller.__inline_stats__
    assert stats.stack_scheduler_candidates == 3
    assert stats.stack_resident_values == 3
    assert stats.stack_spilled_values == 0
    assert stats.stack_crossing_conflicts == 0
    assert caller.__code__.co_varnames == ("x",)
    assert verify_code(caller.__code__).valid


def test_selective_spill_preserves_overloaded_operator_order():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return overloaded_conflict(x)

    expected_trace = []
    actual_trace = []
    expected = overloaded_conflict(TraceNumber(4, expected_trace))
    actual = caller(TraceNumber(4, actual_trace))
    assert actual.value == expected.value
    assert actual_trace == expected_trace
    assert caller.__inline_stats__.stack_resident_values == 2
    assert caller.__inline_stats__.stack_spilled_values == 1


def test_stack_scheduler_statistics_are_consistent():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return two_vs_one_conflict(x)

    stats = caller.__inline_stats__
    assert stats.stack_scheduler_candidates == (
        stats.stack_resident_values + stats.stack_spilled_values
    )
    assert stats.stack_instruction_savings >= 0
    copies = [
        ins.arg for ins in dis.get_instructions(caller, adaptive=False)
        if ins.opname == "COPY"
    ]
    assert copies
    assert max(copies) == stats.stack_max_copy_depth


def test_repeated_decorations_choose_deterministically():
    def build():
        @inline_calls(policy="always", shared_regions=False)
        def caller(x):
            return two_vs_one_conflict(x)
        return caller

    first = build()
    second = build()
    assert first.__inline_stats__.stack_resident_values == 2
    assert second.__inline_stats__.stack_resident_values == 2
    assert tuple(name.rsplit("_", 1)[-1] for name in first.__code__.co_varnames[1:]) == ("a",)
    assert tuple(name.rsplit("_", 1)[-1] for name in second.__code__.co_varnames[1:]) == ("a",)


def test_speed_policy_prefers_shallow_copy_while_always_maximizes_density():
    @inline_calls(policy="speed", shared_regions=False)
    def speed_caller(x):
        return two_vs_one_conflict(x)

    @inline_calls(policy="always", shared_regions=False)
    def dense_caller(x):
        return two_vs_one_conflict(x)

    for value in range(-50, 51):
        expected = two_vs_one_conflict(value)
        assert speed_caller(value) == expected
        assert dense_caller(value) == expected

    assert speed_caller.__inline_stats__.stack_resident_values == 1
    assert dense_caller.__inline_stats__.stack_resident_values == 2
    assert speed_caller.__inline_stats__.stack_max_copy_depth <= dense_caller.__inline_stats__.stack_max_copy_depth
    assert speed_caller.__code__.co_nlocals == 3
    assert dense_caller.__code__.co_nlocals == 2


def test_stack_strategy_can_override_inline_policy():
    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def dense_even_in_speed_policy(x):
        return two_vs_one_conflict(x)

    @inline_calls(policy="always", stack_strategy="speed", shared_regions=False)
    def shallow_even_in_always_policy(x):
        return two_vs_one_conflict(x)

    @inline_calls(policy="always", stack_strategy="off", shared_regions=False)
    def spilled(x):
        return two_vs_one_conflict(x)

    assert dense_even_in_speed_policy.__inline_stats__.stack_resident_values == 2
    assert shallow_even_in_always_policy.__inline_stats__.stack_resident_values == 1
    assert spilled.__inline_stats__.stack_resident_values == 0
    assert spilled.__inline_stats__.stack_scheduler_candidates == 0
    assert spilled.__inline_stats__.stack_spilled_values == 0
    assert spilled.__code__.co_nlocals == 4
    for value in range(-20, 21):
        expected = two_vs_one_conflict(value)
        assert dense_even_in_speed_policy(value) == expected
        assert shallow_even_in_always_policy(value) == expected
        assert spilled(value) == expected


def test_invalid_stack_strategy_is_rejected():
    try:
        @inline_calls(stack_strategy="magic")
        def caller(x):
            return two_vs_one_conflict(x)
    except ValueError as exc:
        assert "stack_strategy" in str(exc)
    else:
        raise AssertionError("invalid stack_strategy was accepted")
