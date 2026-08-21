from __future__ import annotations

import dis

import pytest

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def ephemeral_chain(x):
    a = x
    b = a + 1
    c = b * 2
    return c


@inline_function(register_only=True)
def duplicate_temp(x):
    value = x + 3
    return value * value


@inline_function(register_only=True)
def copy_temp(x):
    alias = x
    return alias * alias + alias


@inline_function(register_only=True)
def constant_temp(x):
    enabled = True
    if enabled:
        return x + 1
    return x - 1


@inline_function(register_only=True)
def slot_a(x):
    a = x + 1
    return a * a + a


@inline_function(register_only=True)
def slot_b(x):
    b = x * 2
    return b * b + b


@inline_function(register_only=True)
def source_mutation(x):
    alias = x
    x = x + 1
    return alias + x


@inline_function(register_only=True)
def checked_temp(flag):
    if flag:
        temp = 7
    return temp


def test_ephemeral_roundtrips_disappear_entirely():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return ephemeral_chain(x)

    assert caller(4) == 10
    assert caller.__code__.co_varnames == ("x",)
    assert (caller.__inline_stats__.synthetic_roundtrips_elided + caller.__inline_stats__.synthetic_copies_propagated) >= 3
    assert len(caller.__code__.co_code) <= 18
    assert verify_code(caller.__code__).valid


def test_duplicate_temp_uses_stack_copy_not_a_fast_local():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return duplicate_temp(x)

    assert caller(5) == 64
    assert caller.__code__.co_varnames == ("x",)
    names = [item.opname for item in dis.get_instructions(caller, adaptive=False)]
    assert "COPY" in names
    assert caller.__inline_stats__.synthetic_roundtrips_elided >= 1


def test_single_assignment_copy_propagates_to_caller_local():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return copy_temp(x)

    assert caller(4) == 20
    assert caller.__code__.co_varnames == ("x",)
    assert caller.__inline_stats__.synthetic_copies_propagated >= 1
    assert verify_code(caller.__code__).valid


def test_synthetic_constant_enables_branch_pruning():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return constant_temp(x)

    assert caller(10) == 11
    stats = caller.__inline_stats__
    assert stats.synthetic_constants_propagated >= 1
    assert stats.constant_branches_folded >= 1
    assert stats.dead_instructions_pruned >= 1
    assert caller.__code__.co_varnames == ("x",)


def test_nonoverlapping_different_callee_locals_share_one_slot():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return slot_b(slot_a(x))

    assert caller(3) == slot_b(slot_a(3))
    synthetic = [name for name in caller.__code__.co_varnames if name.startswith("__inl_")]
    # v0.4 colored these two non-overlapping lifetimes onto one slot.  The
    # stack-resident scheduler can now eliminate both lifetimes entirely, which is
    # strictly stronger; retain the old one-slot outcome as an acceptable fallback.
    assert len(synthetic) <= 1
    assert (
        caller.__inline_stats__.stack_resident_values >= 1
        or caller.__inline_stats__.coalesced_local_slots >= 1
    )
    assert verify_code(caller.__code__).valid


def test_copy_propagation_rejects_source_that_is_written_later():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return source_mutation(x)

    for value in (-3, 0, 5):
        assert caller(value) == source_mutation(value)
    # alias must remain distinct from the subsequently-written x value.
    assert caller.__inline_stats__.synthetic_copies_propagated == 0


def test_unbound_local_semantics_still_win_over_slot_or_copy_optimization():
    @inline_calls(policy="always", shared_regions=False)
    def caller(flag):
        return checked_temp(flag)

    assert caller(True) == 7
    with pytest.raises(UnboundLocalError):
        caller(False)
    assert verify_code(caller.__code__).valid

@inline_function(register_only=True)
def high_index_copy(x):
    alias = x
    return alias + alias


def test_copy_propagation_handles_high_caller_local_indexes():
    # Put the actual source above the packed superinstruction fast-local range.
    @inline_calls(policy="always", shared_regions=False)
    def caller(a0, a1, a2, a3, a4, a5, a6, a7, a8, a9,
               a10, a11, a12, a13, a14, a15, a16, a17, a18):
        return high_index_copy(a18)

    values = tuple(range(19))
    assert caller(*values) == 36
    assert caller.__inline_stats__.synthetic_copies_propagated >= 1
    assert verify_code(caller.__code__).valid


@inline_function(register_only=True)
def branch_copy(x, flag):
    alias = x
    if flag:
        return alias + 1
    return alias - 1


def test_copy_propagation_conservatively_stops_at_control_flow():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x, flag):
        return branch_copy(x, flag)

    assert caller(5, True) == 6
    assert caller(5, False) == 4
    # The straight-line propagator must not stretch one alias proof across branches.
    assert caller.__inline_stats__.synthetic_copies_propagated == 0
    assert verify_code(caller.__code__).valid


@inline_function(register_only=True)
def nested_plus_one(x):
    temp = x + 1
    return temp


@inline_function(register_only=True)
def nested_times_two(x):
    temp = x * 2
    return temp


def test_nested_inline_chain_forwards_values_without_synthetic_roundtrip_locals():
    @inline_calls(policy="always", shared_regions=False)
    def caller(x):
        return nested_times_two(nested_plus_one(x))

    assert caller(7) == 16
    assert caller.__code__.co_varnames == ("x",)
    assert caller.__inline_stats__.synthetic_roundtrips_elided >= 2
    assert not any(i.opname == "CALL" for i in dis.get_instructions(caller, adaptive=False))
    assert verify_code(caller.__code__).valid
