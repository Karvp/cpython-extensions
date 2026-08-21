from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _dom_affine(i):
    return i * 7 + 3


def _has_multiply(function) -> bool:
    return any(
        instruction.opname == "BINARY_OP" and instruction.argrepr == "*"
        for instruction in dis.get_instructions(function, adaptive=False)
    )


def test_strength_reduction_spans_both_branch_blocks():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, left):
        i = 0
        total = 0
        while count > 0:
            if left:
                total += _dom_affine(i)
            else:
                total += _dom_affine(i)
            i += 2
            count -= 1
        return total

    expected = sum(i * 7 + 3 for i in range(0, 16, 2))
    assert function(8, True) == expected
    assert function(8, False) == expected
    stats = function.__inline_stats__
    assert stats.cfg_strength_reduced_values == 1
    assert stats.cfg_strength_reduced_uses == 2
    assert not _has_multiply(function)


def test_speed_policy_rejects_branch_where_update_can_pay_without_affine_use():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 0
        total = 0
        while count > 0:
            if use_affine:
                total += _dom_affine(i)
            else:
                total += 1
            i += 2
            count -= 1
        return total

    for use_affine in (False, True):
        i = 0
        expected = 0
        for _ in range(7):
            expected += i * 7 + 3 if use_affine else 1
            i += 2
        assert function(7, use_affine) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 0
    assert _has_multiply(function)


def test_conditional_induction_update_is_mirrored_across_blocks():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, advance):
        i = 0
        total = 0
        while count > 0:
            total += _dom_affine(i)
            total += _dom_affine(i)
            if advance:
                i += 2
            count -= 1
        return total

    for advance in (False, True):
        i = 0
        expected = 0
        for _ in range(8):
            expected += 2 * (i * 7 + 3)
            if advance:
                i += 2
        assert function(8, advance) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert not _has_multiply(function)


def test_update_in_one_branch_keeps_derived_value_synchronized():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, advance):
        i = 0
        total = 0
        while count > 0:
            if advance:
                total += _dom_affine(i)
                i += 2
            else:
                total += _dom_affine(i)
            count -= 1
        return total

    for advance in (False, True):
        i = 0
        expected = 0
        for _ in range(7):
            expected += i * 7 + 3
            if advance:
                i += 2
        assert function(7, advance) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert not _has_multiply(function)


def test_use_before_and_after_induction_update_share_one_derived_value():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count):
        i = 0
        total = 0
        while count > 0:
            total += _dom_affine(i)
            i += 2
            total += _dom_affine(i)
            count -= 1
        return total

    i = 0
    expected = 0
    for _ in range(9):
        expected += i * 7 + 3
        i += 2
        expected += i * 7 + 3
    assert function(9) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert not _has_multiply(function)


def test_density_can_reduce_branch_group_even_when_speed_cost_proof_rejects():
    @inline_calls(region_dataflow=True, policy="always")
    def function(count, use_affine):
        i = 0
        total = 0
        while count > 0:
            if use_affine:
                total += _dom_affine(i)
                total += _dom_affine(i)
            else:
                total += 1
            i += 2
            count -= 1
        return total

    for use_affine in (False, True):
        i = 0
        expected = 0
        for _ in range(5):
            expected += 2 * (i * 7 + 3) if use_affine else 1
            i += 2
        assert function(5, use_affine) == expected
    # Coverage/density policy may trade update work for static code reduction.
    assert function.__inline_stats__.cfg_strength_reduced_values == 1


def test_strength_reduction_credits_guaranteed_branch_work_after_update():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, left):
        i = 0
        total = 0
        while count > 0:
            i += 2
            if left:
                total += _dom_affine(i)
            else:
                total += _dom_affine(i)
            count -= 1
        return total

    expected = sum(i * 7 + 3 for i in range(2, 18, 2))
    assert function(8, True) == expected
    assert function(8, False) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 1
    assert not _has_multiply(function)


def test_speed_rejects_post_update_branch_with_early_exit_without_affine_work():
    @inline_calls(region_dataflow=True, policy="speed")
    def function(count, use_affine):
        i = 0
        total = 0
        while count > 0:
            i += 2
            if use_affine:
                total += _dom_affine(i)
            else:
                total += 1
            count -= 1
        return total

    for use_affine in (False, True):
        i = 0
        expected = 0
        for _ in range(6):
            i += 2
            expected += i * 7 + 3 if use_affine else 1
        assert function(6, use_affine) == expected
    assert function.__inline_stats__.cfg_strength_reduced_values == 0
