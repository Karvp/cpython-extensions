from __future__ import annotations

import dis
import pytest

from python_extensions import inline_calls, inline_function, verify_code
from python_extensions._core import analyze_fast_locals


@inline_function(register_only=True)
def reusable_local_helper(x):
    y = x + 1
    z = y * 2
    return z - 3


@inline_function(register_only=True)
def possibly_unbound(x):
    if x:
        value = 7
    return value


@inline_function(register_only=True)
def frozen_branch(x, positive=True):
    if positive:
        return x + 1
    return x - 1


def test_repeated_inline_sites_reuse_safe_local_slots():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        a = reusable_local_helper(x)
        b = reusable_local_helper(a)
        c = reusable_local_helper(b)
        return reusable_local_helper(c)

    assert caller(3) == reusable_local_helper(
        reusable_local_helper(reusable_local_helper(reusable_local_helper(3)))
    )
    synthetic = [name for name in caller.__code__.co_varnames if name.startswith("__inl_reuse_")]
    # v0.4 may eliminate both reusable temporaries entirely by keeping their
    # single-use values on the operand stack.  Older safe slot reuse remains a
    # valid fallback when a local has a longer lifetime.
    assert len(synthetic) <= 2
    assert (
        caller.__inline_stats__.reused_local_groups >= 1
        or caller.__inline_stats__.synthetic_roundtrips_elided >= 1
    )
    assert verify_code(caller.__code__).valid


def test_local_reuse_is_rejected_when_unbound_semantics_matter():
    analysis = analyze_fast_locals(possibly_unbound.__code__)
    assert not analysis.reuse_safe

    @inline_calls(policy="always", shared_regions=False)
    def caller(first, second):
        a = possibly_unbound(first)
        b = possibly_unbound(second)
        return a, b

    assert caller(True, True) == (7, 7)
    with pytest.raises(UnboundLocalError):
        caller(True, False)
    assert caller.__inline_stats__.reused_local_groups == 0


def test_default_boolean_branch_is_folded_and_dead_arm_pruned():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return frozen_branch(x)

    assert caller(10) == 11
    stats = caller.__inline_stats__
    assert stats.constant_branches_folded >= 1
    assert stats.dead_instructions_pruned >= 1
    assert stats.late_stack_forwards + stats.caller_parameter_aliases >= 1
    assert stats.redundant_jumps_removed >= 1

    instructions = list(dis.get_instructions(caller, adaptive=False))
    assert not any(i.opname == "CALL" for i in instructions)
    assert not any(i.opname == "TO_BOOL" for i in instructions)
    assert not any(i.opname.startswith("POP_JUMP") for i in instructions)
    assert caller.__code__.co_nlocals == 1
    assert len(caller.__code__.co_code) <= 12


def test_explicit_runtime_boolean_is_not_frozen():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x, flag):
        return frozen_branch(x, flag)

    assert caller(10, True) == 11
    assert caller(10, False) == 9
    # The explicit flag is a runtime value, so branch folding must not specialize it.
    assert any(i.opname.startswith("POP_JUMP") for i in dis.get_instructions(caller, adaptive=False))

@inline_function(register_only=True)
def repeated_read(a, b):
    return a * a + b * b


def test_readonly_callee_parameters_alias_always_bound_caller_parameters():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x, y):
        return repeated_read(x, y)

    assert caller(3, 4) == 25
    assert caller.__inline_stats__.caller_parameter_aliases == 2
    synthetic_parameters = {
        name for name in caller.__code__.co_varnames if "repeated_read" in name
    }
    assert not synthetic_parameters
    assert not any(i.opname == "CALL" for i in dis.get_instructions(caller, adaptive=False))
    assert verify_code(caller.__code__).valid


def test_aliasing_does_not_apply_to_potentially_unbound_caller_local():
    @inline_calls(policy="always", shared_regions=False)
    def caller(flag, y):
        if flag:
            x = 3
        return repeated_read(x, y)

    assert caller(True, 4) == 25
    with pytest.raises(UnboundLocalError):
        caller(False, 4)
    assert caller.__inline_stats__.caller_parameter_aliases == 0

@inline_function(register_only=True)
def folded_math(x, scale=3, offset=4):
    return x + scale * 2 + offset - 1


def test_constant_binary_expression_folds_after_default_propagation():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return folded_math(x)

    assert caller(10) == folded_math(10)
    assert caller.__inline_stats__.constant_binary_ops_folded >= 1
    assert caller.__inline_stats__.caller_parameter_aliases >= 1
    assert not any(i.opname == "CALL" for i in dis.get_instructions(caller, adaptive=False))
    assert verify_code(caller.__code__).valid


@inline_function(register_only=True)
def runtime_error_default(x, divisor=0):
    return x + (1 // divisor)


def test_constant_folding_does_not_move_runtime_exceptions_to_decoration():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return runtime_error_default(x)

    with pytest.raises(ZeroDivisionError):
        caller(3)


def test_proven_bound_caller_locals_can_alias_without_synthetic_parameter_slots():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x, y):
        local = x + 1
        return repeated_read(local, y)

    assert caller(2, 4) == 25
    assert caller.__inline_stats__.caller_local_aliases >= 1
    assert caller.__inline_stats__.caller_parameter_aliases >= 1
    assert verify_code(caller.__code__).valid
