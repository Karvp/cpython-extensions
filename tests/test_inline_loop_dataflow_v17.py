from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _loop_true():
    return True


@inline_function(register_only=True)
def _loop_identity(value):
    return value


@inline_function(register_only=True)
def _loop_choose(flag, value):
    if flag:
        return value + 1
    return value - 1


def _conditional_jumps(function):
    return [
        item
        for item in dis.get_instructions(function, adaptive=False)
        if "JUMP" in item.opname and "IF" in item.opname
    ]


def test_constant_defined_before_while_survives_backedge():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        flag = _loop_true()
        total = 0
        while count > 0:
            total += _loop_choose(flag, value)
            count -= 1
        return total

    for count in (0, 1, 5):
        assert function(10, count) == count * 11
    stats = function.__inline_stats__
    assert stats.cfg_loop_headers >= 1
    assert stats.cfg_loop_invariant_facts >= 1
    # choose(flag, value)'s internal boolean branch should fold.  The remaining
    # conditional branch belongs to the while loop itself.
    assert len(_conditional_jumps(function)) == 2


def test_outer_dynamic_version_survives_loop_when_not_redefined():
    @inline_calls(policy="always", region_dataflow=True)
    def function(value, count):
        produced = _loop_identity(value)
        total = 0
        while count > 0:
            alias = produced
            total += alias * 2
            count -= 1
        return total

    for value in (-3, 0, 8):
        for count in (0, 1, 4):
            assert function(value, count) == count * (value * 2)
    stats = function.__inline_stats__
    assert stats.cfg_loop_headers >= 1
    assert stats.cfg_loop_invariant_facts >= 1


def test_token_defined_inside_loop_is_not_carried_to_next_iteration():
    @inline_calls(policy="always", region_dataflow=True)
    def function(value, count):
        produced = _loop_identity(value)
        total = 0
        while count > 0:
            produced = _loop_identity(produced + 1)
            alias = produced
            total += alias
            count -= 1
        return total, produced

    def baseline(value, count):
        produced = value
        total = 0
        while count > 0:
            produced = produced + 1
            alias = produced
            total += alias
            count -= 1
        return total, produced

    for value in (-2, 0, 7):
        for count in (0, 1, 2, 6):
            assert function(value, count) == baseline(value, count)
    assert function.__inline_stats__.cfg_loop_variant_kills >= 1


def test_loop_carried_same_constant_can_merge_at_header():
    @inline_calls(region_dataflow=True)
    def function(value, count, refresh):
        flag = _loop_true()
        total = 0
        while count > 0:
            if refresh:
                flag = True
            else:
                flag = True
            total += _loop_choose(flag, value)
            count -= 1
        return total

    for refresh in (False, True):
        assert function(4, 5, refresh) == 25
    stats = function.__inline_stats__
    assert stats.cfg_loop_invariant_facts >= 1
    # while condition + refresh branch remain; choose's internal flag branch folds.
    assert len(_conditional_jumps(function)) == 3


def test_for_loop_preserves_outer_constant_fact():
    @inline_calls(region_dataflow=True)
    def function(value, values):
        flag = _loop_true()
        total = 0
        for _item in values:
            total += _loop_choose(flag, value)
        return total

    assert function(3, []) == 0
    assert function(3, [1, 2, 3, 4]) == 16
    stats = function.__inline_stats__
    assert stats.cfg_loop_headers >= 1
    assert stats.cfg_loop_invariant_facts >= 1
    # FOR_ITER is not an IF jump; no choose flag branch should remain.
    assert len(_conditional_jumps(function)) == 0


def test_remaining_call_inside_loop_kills_invariant_fact():
    def opaque():
        return None

    @inline_calls(region_dataflow=True)
    def function(value, count):
        flag = _loop_true()
        total = 0
        while count > 0:
            opaque()
            total += _loop_choose(flag, value)
            count -= 1
        return total

    assert function(10, 3) == 33
    # The opaque call is a frame/local observability barrier, so choose's branch
    # must remain in addition to the while branch.
    assert len(_conditional_jumps(function)) >= 2


def test_loop_carried_constant_change_blocks_header_substitution():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        flag = _loop_true()
        total = 0
        while count > 0:
            total += _loop_choose(flag, value)
            flag = False
            count -= 1
        return total

    # First iteration uses True, later iterations False.  Folding the header load to
    # either constant would therefore be incorrect.
    assert function(10, 0) == 0
    assert function(10, 1) == 11
    assert function(10, 3) == 29
    assert len(_conditional_jumps(function)) >= 3  # two while tests + choose


def test_dynamic_token_redefined_at_tail_is_unknown_on_next_header():
    @inline_calls(policy="always", region_dataflow=True)
    def function(value, count):
        produced = _loop_identity(value)
        total = 0
        while count > 0:
            alias = produced
            total += alias
            produced = _loop_identity(produced + 1)
            count -= 1
        return total, produced

    def baseline(value, count):
        produced = value
        total = 0
        while count > 0:
            alias = produced
            total += alias
            produced = produced + 1
            count -= 1
        return total, produced

    for value in (-2, 0, 9):
        for count in (0, 1, 2, 5):
            assert function(value, count) == baseline(value, count)
    assert function.__inline_stats__.cfg_loop_variant_kills >= 1


def test_continue_latches_preserve_only_common_invariant():
    @inline_calls(region_dataflow=True)
    def function(value, count):
        flag = _loop_true()
        total = 0
        while count > 0:
            count -= 1
            if count & 1:
                total += _loop_choose(flag, value)
                continue
            total += _loop_choose(flag, value)
        return total

    for count in (0, 1, 7):
        assert function(2, count) == count * 3
    stats = function.__inline_stats__
    assert stats.cfg_loop_headers >= 1
    assert stats.cfg_loop_invariant_facts >= 1


def test_nested_loop_outer_token_is_inner_invariant_but_outer_variant():
    @inline_calls(policy="always", region_dataflow=True)
    def function(value, outer, inner):
        produced = _loop_identity(value)
        total = 0
        while outer > 0:
            produced = _loop_identity(produced + 1)
            remaining = inner
            while remaining > 0:
                alias = produced
                total += alias
                remaining -= 1
            outer -= 1
        return total, produced

    def baseline(value, outer, inner):
        produced = value
        total = 0
        while outer > 0:
            produced += 1
            remaining = inner
            while remaining > 0:
                total += produced
                remaining -= 1
            outer -= 1
        return total, produced

    for value in (0, 4):
        for outer in (0, 1, 3):
            for inner in (0, 1, 4):
                assert function(value, outer, inner) == baseline(value, outer, inner)
    stats = function.__inline_stats__
    assert stats.cfg_loop_headers >= 2
    assert stats.cfg_loop_variant_kills >= 1


def test_loop_carried_copy_of_same_outer_version_remains_stable():
    @inline_calls(policy="always", region_dataflow=True)
    def function(value, count):
        produced = _loop_identity(value)
        carried = produced
        total = 0
        while count > 0:
            alias = carried
            total += alias * 2
            carried = alias
            count -= 1
        return total, carried

    for value in (-5, 0, 12):
        for count in (0, 1, 5):
            assert function(value, count) == (count * value * 2, value)
    stats = function.__inline_stats__
    assert stats.cfg_loop_invariant_facts >= 1
