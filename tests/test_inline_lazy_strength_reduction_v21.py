from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _lazy_affine(i):
    return i * 7 + 3


@inline_function(register_only=True)
def _lazy_mul(i):
    return i * 11


def _multiply_count(function) -> int:
    return sum(
        instruction.opname == "BINARY_OP" and instruction.argrepr == "*"
        for instruction in dis.get_instructions(function, adaptive=False)
    )


def test_speed_lazily_materializes_repeated_affine_work_on_rare_branch():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 0
        total = 0
        while count > 0:
            if use_affine:
                total += _lazy_affine(i)
                total += _lazy_affine(i)
            else:
                total += 1
            i += 2
            count -= 1
        return total

    for use_affine in (False, True):
        i = 0
        expected = 0
        for _ in range(9):
            expected += 2 * (i * 7 + 3) if use_affine else 1
            i += 2
        assert function(9, use_affine) == expected

    stats = function.__inline_stats__
    assert stats.cfg_strength_reduced_values == 1
    assert stats.cfg_strength_reduced_uses == 1
    assert stats.cfg_strength_reduction_updates == 0
    assert stats.cfg_strength_lazy_values == 1
    assert stats.cfg_strength_lazy_uses == 1
    assert stats.cfg_strength_lazy_materializations == 1
    # One affine expression remains as the path-entry synchronization point.
    assert _multiply_count(function) == 1


def test_lazy_value_resynchronizes_when_branch_membership_changes_each_iteration():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count):
        i = 0
        total = 0
        while count > 0:
            if i & 4:
                total += _lazy_affine(i)
                total += _lazy_affine(i)
            else:
                total -= 1
            i += 1
            count -= 1
        return total

    for count in range(0, 40):
        i = 0
        expected = 0
        for _ in range(count):
            if i & 4:
                expected += 2 * (i * 7 + 3)
            else:
                expected -= 1
            i += 1
        assert function(count) == expected

    stats = function.__inline_stats__
    assert stats.cfg_strength_lazy_materializations == 1
    assert stats.cfg_strength_reduction_updates == 0


def test_lazy_materialization_handles_post_update_affine_path():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = -3
        total = 0
        while count > 0:
            i += 4
            if use_affine:
                total += _lazy_affine(i)
                total += _lazy_affine(i)
            else:
                total += 2
            count -= 1
        return total

    for use_affine in (False, True):
        i = -3
        expected = 0
        for _ in range(11):
            i += 4
            expected += 2 * (i * 7 + 3) if use_affine else 2
        assert function(11, use_affine) == expected

    stats = function.__inline_stats__
    assert stats.cfg_strength_lazy_materializations == 1
    assert stats.cfg_strength_reduction_updates == 0


def test_lazy_materialization_splits_pre_and_post_update_affine_paths():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 1
        total = 0
        while count > 0:
            if use_affine:
                total += _lazy_affine(i)
                total += _lazy_affine(i)
            i += 3
            if use_affine:
                total += _lazy_affine(i)
                total += _lazy_affine(i)
            else:
                total += 1
            count -= 1
        return total

    for use_affine in (False, True):
        i = 1
        expected = 0
        for _ in range(8):
            if use_affine:
                expected += 2 * (i * 7 + 3)
            i += 3
            if use_affine:
                expected += 2 * (i * 7 + 3)
            else:
                expected += 1
        assert function(8, use_affine) == expected

    stats = function.__inline_stats__
    assert stats.cfg_strength_lazy_materializations == 2
    assert stats.cfg_strength_lazy_values == 2
    assert stats.cfg_strength_lazy_uses == 2
    assert stats.cfg_strength_reduction_updates == 0


def test_single_rare_affine_use_stays_unmodified_under_speed_policy():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 0
        total = 0
        while count > 0:
            if use_affine:
                total += _lazy_affine(i)
            else:
                total += 1
            i += 2
            count -= 1
        return total

    assert function(6, False) == 6
    assert function.__inline_stats__.cfg_strength_reduced_values == 0
    assert function.__inline_stats__.cfg_strength_lazy_materializations == 0
    assert _multiply_count(function) == 1


def test_multiply_only_pair_is_not_cached_when_copy_store_would_break_even():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 1
        total = 0
        while count > 0:
            if use_affine:
                total += _lazy_mul(i)
                total += _lazy_mul(i)
            else:
                total += 1
            i += 2
            count -= 1
        return total

    assert function(5, True) == 2 * sum(i * 11 for i in range(1, 11, 2))
    stats = function.__inline_stats__
    assert stats.cfg_strength_lazy_materializations == 0
    assert _multiply_count(function) == 2


def test_three_multiply_only_uses_make_lazy_cache_profitable():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 1
        total = 0
        while count > 0:
            if use_affine:
                total += _lazy_mul(i)
                total += _lazy_mul(i)
                total += _lazy_mul(i)
            else:
                total += 1
            i += 2
            count -= 1
        return total

    assert function(5, False) == 5
    assert function(5, True) == 3 * sum(i * 11 for i in range(1, 11, 2))
    stats = function.__inline_stats__
    assert stats.cfg_strength_lazy_materializations == 1
    assert stats.cfg_strength_lazy_uses == 2
    assert stats.cfg_strength_reduction_updates == 0
    assert _multiply_count(function) == 1
