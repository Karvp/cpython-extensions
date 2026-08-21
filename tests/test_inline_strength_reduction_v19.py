from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _sr_affine(i):
    return i * 7 + 3


@inline_function(register_only=True)
def _sr_scaled(i):
    return i * 5


@inline_function(register_only=True)
def _sr_negative(i):
    return i * -3 - 4


def _binary_reprs(function):
    return [
        item.argrepr
        for item in dis.get_instructions(function, adaptive=False)
        if item.opname == "BINARY_OP"
    ]


def test_repeated_affine_expression_becomes_secondary_induction():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 0
        total = 0
        while count > 0:
            total += _sr_affine(i)
            total += _sr_affine(i)
            i += 2
            count -= 1
        return total

    for count in range(12):
        expected = sum(2 * (i * 7 + 3) for i in range(0, count * 2, 2))
        assert function(count) == expected
    stats = function.__inline_stats__
    assert stats.cfg_strength_reduced_values == 1
    assert stats.cfg_strength_reduced_uses == 2
    assert stats.cfg_strength_reduction_updates == 1
    assert "*" not in _binary_reprs(function)
    assert any(name.startswith("__inl_sr_") for name in function.__code__.co_varnames)


def test_scaled_only_requires_enough_repeated_uses_for_speed_profit():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 1
        total = 0
        while count > 0:
            total += _sr_scaled(i)
            total += _sr_scaled(i)
            total += _sr_scaled(i)
            total += _sr_scaled(i)
            i += 3
            count -= 1
        return total

    for count in range(10):
        expected = sum(4 * (i * 5) for i in range(1, 1 + 3 * count, 3))
        assert function(count) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert "*" not in _binary_reprs(function)


def test_negative_scale_and_decreasing_recurrence():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 20
        total = 0
        while count > 0:
            total += _sr_negative(i)
            total += _sr_negative(i)
            i -= 2
            count -= 1
        return total

    for count in range(9):
        i = 20
        expected = 0
        for _ in range(count):
            expected += 2 * (i * -3 - 4)
            i -= 2
        assert function(count) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert "*" not in _binary_reprs(function)


def test_single_affine_use_is_not_strength_reduced_under_speed_policy():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count):
        i = 0
        total = 0
        while count > 0:
            total += _sr_affine(i)
            i += 2
            count -= 1
        return total

    assert function(8) == sum(i * 7 + 3 for i in range(0, 16, 2))
    assert function.__inline_stats__.cfg_strength_reduced_values == 0
    assert "*" in _binary_reprs(function)


def test_dynamic_scale_is_not_strength_reduced():
    @inline_function(register_only=True)
    def dynamic(i, scale):
        return i * scale + 3

    @inline_calls(region_dataflow=True)
    def function(count, scale):
        i = 0
        total = 0
        while count > 0:
            total += dynamic(i, scale)
            total += dynamic(i, scale)
            i += 2
            count -= 1
        return total

    for scale in (-5, 1, 7):
        expected = sum(2 * (i * scale + 3) for i in range(0, 12, 2))
        assert function(6, scale) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 0


def test_use_after_induction_update_tracks_derived_recurrence():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 0
        total = 0
        while count > 0:
            total += _sr_affine(i)
            i += 2
            total += _sr_affine(i)
            count -= 1
        return total

    for count in range(8):
        i = 0
        expected = 0
        for _ in range(count):
            expected += i * 7 + 3
            i += 2
            expected += i * 7 + 3
        assert function(count) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert "*" not in _binary_reprs(function)


def test_zero_iteration_does_not_execute_strength_body_initialization_semantics():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 2
        total = 10
        while count > 0:
            total += _sr_affine(i)
            total += _sr_affine(i)
            i += 4
            count -= 1
        return total

    assert function(0) == 10
    assert function(1) == 44
    assert function.__inline_stats__.cfg_strength_reduced_values == 1


def test_strength_reduction_preserves_paired_accumulator_load_shape():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 0
        total = 0
        while count > 0:
            total += i * 7 + 3
            total += i * 7 + 3
            i += 2
            count -= 1
        return total

    assert function(5) == 310
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert "*" not in _binary_reprs(function)


def test_constant_left_multiply_is_strength_reduced():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 2
        total = 0
        while count > 0:
            total += 11 * i + 5
            total += 11 * i + 5
            i += 3
            count -= 1
        return total

    i = 2
    expected = 0
    for _ in range(7):
        expected += 2 * (11 * i + 5)
        i += 3
    assert function(7) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert "*" not in _binary_reprs(function)


def test_multiple_derived_recurrences_share_one_induction_update():
    @inline_calls(region_dataflow=True)
    def function(count):
        i = 0
        total = 0
        while count > 0:
            total += i * 7 + 3
            total += i * 7 + 3
            total += i * 11 - 5
            total += i * 11 - 5
            i += 2
            count -= 1
        return total

    i = 0
    expected = 0
    for _ in range(8):
        expected += 2 * (i * 7 + 3) + 2 * (i * 11 - 5)
        i += 2
    assert function(8) == expected
    stats = function.__inline_stats__
    assert stats.cfg_strength_reduced_values == 2
    assert stats.cfg_strength_reduced_uses == 4
    assert stats.cfg_strength_reduction_updates == 2
    assert "*" not in _binary_reprs(function)
    assert sum(name.startswith("__inl_sr_") for name in function.__code__.co_varnames) == 2
