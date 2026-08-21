from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def deep_expression_candidate(x):
    a = x + 1
    out = (x, x + 2, a * 2 + 3 * a)
    return out


@inline_function(register_only=True)
def middle_segment_candidate(x):
    a = x + 1
    b = x + 2
    u = a * 2 + a * 3 + 4 * a
    m1 = b * 5
    d = x * 11
    d + 1
    abs(d)
    m2 = b * 6
    c = x + 3
    n = b * 7 + c * 8 + 1 * b
    z = c * 9 + c * 10 + 1 * c
    return u + m1 + m2 + n + z


class TraceNumber:
    def __init__(self, value, trace):
        self.value = value
        self.trace = trace

    @staticmethod
    def _value(other):
        return other.value if isinstance(other, TraceNumber) else other

    def __add__(self, other):
        other = self._value(other)
        self.trace.append(("add", self.value, other))
        return TraceNumber(self.value + other, self.trace)

    def __radd__(self, other):
        other = self._value(other)
        self.trace.append(("radd", other, self.value))
        return TraceNumber(other + self.value, self.trace)

    def __mul__(self, other):
        other = self._value(other)
        self.trace.append(("mul", self.value, other))
        return TraceNumber(self.value * other, self.trace)

    def __rmul__(self, other):
        other = self._value(other)
        self.trace.append(("rmul", other, self.value))
        return TraceNumber(other * self.value, self.trace)

    def __abs__(self):
        self.trace.append(("abs", self.value))
        return TraceNumber(abs(self.value), self.trace)


def test_deep_final_use_uses_deferred_cleanup_in_density_mode():
    @inline_calls(policy="always", stack_strategy="density", shared_regions=False)
    def caller(x):
        return deep_expression_candidate(x)

    for value in range(-80, 81):
        assert caller(value) == deep_expression_candidate(value)
    stats = caller.__inline_stats__
    assert stats.stack_scheduler_candidates == 1
    assert stats.stack_resident_values == 1
    assert caller.__code__.co_nlocals == 1
    assert verify_code(caller.__code__).valid

    instructions = list(dis.get_instructions(caller, adaptive=False))
    assert any(ins.opname == "COPY" and ins.arg >= 4 for ins in instructions)
    # The retained original is discarded after the final expression result exists.
    assert any(ins.opname == "POP_TOP" for ins in instructions)


def test_speed_policy_rejects_deep_zero_or_negative_savings_residency():
    @inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
    def caller(x):
        return deep_expression_candidate(x)

    for value in range(-40, 41):
        assert caller(value) == deep_expression_candidate(value)
    assert caller.__inline_stats__.stack_resident_values == 0
    assert verify_code(caller.__code__).valid


def test_density_middle_segment_completes_crossing_split_model():
    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def caller(x):
        return middle_segment_candidate(x)

    for value in range(-120, 121):
        assert caller(value) == middle_segment_candidate(value)

    stats = caller.__inline_stats__
    assert stats.stack_scheduler_candidates >= 3
    assert stats.stack_resident_values >= 2
    assert stats.stack_middle_splits == 1
    assert stats.stack_split_values >= 1
    assert stats.stack_split_reads >= 2
    assert stats.segmented_local_lifetimes >= 1
    assert stats.coalesced_local_slots >= 1
    assert verify_code(caller.__code__).valid


def test_middle_segment_preserves_overloaded_operand_order():
    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def caller(x):
        return middle_segment_candidate(x)

    expected_trace = []
    actual_trace = []
    expected = middle_segment_candidate(TraceNumber(3, expected_trace))
    actual = caller(TraceNumber(3, actual_trace))
    assert actual.value == expected.value
    assert actual_trace == expected_trace
    assert caller.__inline_stats__.stack_middle_splits == 1


def test_middle_segment_is_density_only():
    @inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
    def speed(x):
        return middle_segment_candidate(x)

    @inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
    def density(x):
        return middle_segment_candidate(x)

    for value in range(-30, 31):
        expected = middle_segment_candidate(value)
        assert speed(value) == expected
        assert density(value) == expected
    assert speed.__inline_stats__.stack_middle_splits == 0
    assert density.__inline_stats__.stack_middle_splits == 1
