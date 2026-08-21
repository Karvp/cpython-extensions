from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def split_candidate(x):
    a = x + 1
    b = x + 2
    y = a * 2 + a * 3 + a * 4 + b * 5 + a
    z = b * 4 + b
    return y + z + b


@inline_function(register_only=True)
def no_zero_stack_suffix(x):
    a = x + 1
    b = x + 2
    return a * 2 + a * 3 + a * 4 + a * 5 + a * 6 + b * 7 + a + b + b + b


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
def traced_split_candidate(x):
    a = x + 1
    b = x + 2
    y = a * 2 + a * 3 + a * 4 + b * 5 + a
    z = b * 4 + b
    return y + z + b


def test_density_live_range_split_shortens_spill_and_unlocks_slot_reuse():
    @inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
    def speed(x):
        return split_candidate(x)

    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def density(x):
        return split_candidate(x)

    for value in range(-100, 101):
        expected = split_candidate(value)
        assert speed(value) == expected
        assert density(value) == expected

    speed_stats = speed.__inline_stats__
    dense_stats = density.__inline_stats__
    assert speed_stats.stack_split_values == 0
    assert dense_stats.stack_split_values == 1
    assert dense_stats.stack_split_reads == 3
    assert dense_stats.stack_split_instruction_cost == 1
    assert dense_stats.coalesced_local_slots >= 1
    assert density.__code__.co_nlocals < speed.__code__.co_nlocals
    assert verify_code(density.__code__).valid


def test_split_reload_is_visible_before_copy_suffix():
    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def caller(x):
        return split_candidate(x)

    instructions = list(dis.get_instructions(caller, adaptive=False))
    assert caller.__inline_stats__.stack_split_values == 1
    # A split suffix is seeded by a real LOAD_FAST and then served by COPY/SWAP.
    assert any(ins.opname == "COPY" for ins in instructions)
    assert any(ins.opname == "SWAP" for ins in instructions)
    assert verify_code(caller.__code__).valid


def test_split_is_rejected_without_exact_zero_stack_seed_boundary():
    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def caller(x):
        return no_zero_stack_suffix(x)

    for value in range(-40, 41):
        assert caller(value) == no_zero_stack_suffix(value)
    assert caller.__inline_stats__.stack_crossing_conflicts >= 1
    assert caller.__inline_stats__.stack_split_values == 0
    assert verify_code(caller.__code__).valid


def test_split_preserves_overloaded_operator_order_and_identity():
    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def caller(x):
        return traced_split_candidate(x)

    expected_trace = []
    actual_trace = []
    expected = traced_split_candidate(TraceNumber(4, expected_trace))
    actual = caller(TraceNumber(4, actual_trace))
    assert actual.value == expected.value
    assert actual_trace == expected_trace
    assert caller.__inline_stats__.stack_split_values == 1


def test_stack_strategy_off_and_speed_do_not_run_split_planner():
    @inline_calls(policy="always", stack_strategy="off", shared_regions=False)
    def off(x):
        return split_candidate(x)

    @inline_calls(policy="always", stack_strategy="speed", shared_regions=False)
    def speed(x):
        return split_candidate(x)

    assert off.__inline_stats__.stack_split_values == 0
    assert off.__inline_stats__.stack_split_reads == 0
    assert speed.__inline_stats__.stack_split_values == 0
    for value in range(-20, 21):
        expected = split_candidate(value)
        assert off(value) == expected
        assert speed(value) == expected

@inline_function(register_only=True)
def speed_prefix_candidate(x):
    a = x + 1
    p = a * 2 + a * 3
    b = x + 2
    y = a * 4 + b * 5 + a
    z = b * 6 + b * 7 + b * 8
    return p + y + z + b


@inline_function(register_only=True)
def wider_prefix_candidate(x):
    a = x + 1
    p = a * 2 + a * 3 + a * 4
    b = x + 2
    y = a * 5 + b * 6 + a
    z = b * 7 + b * 8 + b * 9 + b * 10
    return p + y + z + b


def test_speed_strategy_uses_zero_cost_two_read_prefix_split():
    @inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
    def caller(x):
        return speed_prefix_candidate(x)

    for value in range(-100, 101):
        assert caller(value) == speed_prefix_candidate(value)
    stats = caller.__inline_stats__
    assert stats.stack_split_values == 1
    assert stats.stack_split_reads == 2
    assert stats.stack_split_instruction_cost == 0
    assert stats.coalesced_local_slots >= 1
    assert verify_code(caller.__code__).valid


def test_speed_strategy_declines_wider_prefix_but_density_may_take_it():
    @inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
    def speed(x):
        return wider_prefix_candidate(x)

    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def density(x):
        return wider_prefix_candidate(x)

    for value in range(-50, 51):
        expected = wider_prefix_candidate(value)
        assert speed(value) == expected
        assert density(value) == expected
    assert speed.__inline_stats__.stack_split_values == 0
    assert density.__inline_stats__.stack_split_values >= 1
    assert verify_code(speed.__code__).valid
    assert verify_code(density.__code__).valid
