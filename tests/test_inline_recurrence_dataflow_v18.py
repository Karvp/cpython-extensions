from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _rec_even(i, value):
    if i % 2 == 0:
        return value + 7
    return value - 11


@inline_function(register_only=True)
def _rec_low_bits(i, value):
    if (i & 3) == 3:
        return value + 5
    return value - 9


@inline_function(register_only=True)
def _rec_nonnegative(i, value):
    if i >= 0:
        return value + 1
    return value - 1


@inline_function(register_only=True)
def _rec_at_one(i, value):
    if i == 1:
        return value + 1000
    return value + 2


@inline_function(register_only=True)
def _rec_at_most_ten(i, value):
    if i <= 10:
        return value + 3
    return value - 3


def _binary_reprs(function):
    return [
        item.argrepr
        for item in dis.get_instructions(function, adaptive=False)
        if item.opname == "BINARY_OP"
    ]


def test_even_affine_recurrence_folds_modulo_branch():
    @inline_calls(region_dataflow=True)
    def function(value, limit):
        i = 0
        total = 0
        while i < limit:
            total += _rec_even(i, value)
            i += 2
        return total

    for limit in (0, 1, 2, 3, 9):
        iterations = (limit + 1) // 2 if limit > 0 else 0
        assert function(10, limit) == iterations * 17
    stats = function.__inline_stats__
    assert stats.cfg_affine_recurrences >= 1
    assert stats.cfg_recurrence_folds >= 1
    assert "%" not in _binary_reprs(function)


def test_odd_affine_recurrence_folds_to_false_branch():
    @inline_calls(region_dataflow=True)
    def function(value, limit):
        i = 1
        total = 0
        while i < limit:
            total += _rec_even(i, value)
            i += 2
        return total

    for limit in (0, 1, 2, 7):
        expected = sum((10 - 11) for i in range(1, limit, 2))
        assert function(10, limit) == expected
    assert function.__inline_stats__.cfg_recurrence_folds >= 1
    assert "%" not in _binary_reprs(function)


def test_power_of_two_low_bits_are_recurrence_invariant():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        i = 3
        total = 0
        while count > 0:
            total += _rec_low_bits(i, value)
            i += 4
            count -= 1
        return total

    assert function(2, 0) == 0
    assert function(2, 5) == 35
    assert "&" not in _binary_reprs(function)
    assert function.__inline_stats__.cfg_recurrence_folds >= 1


def test_positive_recurrence_proves_monotonic_lower_bound():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        i = 0
        total = 0
        while count > 0:
            total += _rec_nonnegative(i, value)
            i += 3
            count -= 1
        return total

    assert function(8, 5) == 45
    stats = function.__inline_stats__
    assert stats.cfg_affine_recurrences >= 1
    assert stats.cfg_recurrence_folds >= 1


def test_congruence_proves_unreachable_equality():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        i = 0
        total = 0
        while count > 0:
            total += _rec_at_one(i, value)
            i += 2
            count -= 1
        return total

    assert function(4, 6) == 36
    assert function.__inline_stats__.cfg_recurrence_folds >= 1


def test_decreasing_recurrence_proves_upper_bound():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        i = 10
        total = 0
        while count > 0:
            total += _rec_at_most_ten(i, value)
            i -= 2
            count -= 1
        return total

    assert function(5, 4) == 32
    assert function.__inline_stats__.cfg_recurrence_folds >= 1


def test_conditional_update_preserves_congruence_properties():
    @inline_calls(region_dataflow=True)
    def function(value, count, advance):
        i = 0
        total = 0
        while count > 0:
            total += _rec_even(i, value)
            if advance:
                i += 2
            count -= 1
        return total

    for advance in (False, True):
        assert function(3, 5, advance) == 50
    assert function.__inline_stats__.cfg_recurrence_folds >= 1


def test_recurrence_fact_propagates_through_caller_copy():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        i = 0
        total = 0
        while count > 0:
            alias = i
            total += _rec_even(alias, value)
            i += 2
            count -= 1
        return total

    assert function(1, 6) == 48
    assert function.__inline_stats__.cfg_recurrence_folds >= 1


def test_dynamic_step_is_not_promoted_to_affine_recurrence():
    @inline_calls(region_dataflow=True)
    def function(value, count, step):
        i = 0
        total = 0
        while count > 0:
            total += _rec_even(i, value)
            i += step
            count -= 1
        return total

    def baseline(value, count, step):
        i = 0
        total = 0
        while count > 0:
            total += value + 7 if i % 2 == 0 else value - 11
            i += step
            count -= 1
        return total

    for step in (1, 2, 3):
        assert function(10, 7, step) == baseline(10, 7, step)
    assert function.__inline_stats__.cfg_affine_recurrences == 0


def test_remaining_call_blocks_recurrence_proof():
    def opaque():
        return None

    @inline_calls(region_dataflow=True)
    def function(value, count):
        i = 0
        total = 0
        while count > 0:
            opaque()
            total += _rec_even(i, value)
            i += 2
            count -= 1
        return total

    assert function(10, 5) == 85
    assert function.__inline_stats__.cfg_affine_recurrences == 0
    assert "%" in _binary_reprs(function)


def test_multiple_induction_writes_reject_recurrence():
    @inline_calls(region_dataflow=True)
    def function(value, count, extra):
        i = 0
        total = 0
        while count > 0:
            total += _rec_even(i, value)
            i += 2
            if extra:
                i += 2
            count -= 1
        return total

    def baseline(value, count, extra):
        i = 0
        total = 0
        while count > 0:
            total += value + 7 if i % 2 == 0 else value - 11
            i += 2
            if extra:
                i += 2
            count -= 1
        return total

    for extra in (False, True):
        assert function(4, 7, extra) == baseline(4, 7, extra)
    assert function.__inline_stats__.cfg_affine_recurrences == 0
