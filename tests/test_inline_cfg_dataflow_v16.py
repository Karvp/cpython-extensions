from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _cfg_flag():
    return True


@inline_function(register_only=True)
def _cfg_identity(value):
    return value


@inline_function(register_only=True)
def _cfg_choose(flag, value):
    if flag:
        return value + 1
    return value - 1


@inline_function(register_only=True)
def _cfg_scale(value):
    return value * 3 + 1


def _conditional_jumps(function):
    return [
        item
        for item in dis.get_instructions(function, adaptive=False)
        if "JUMP" in item.opname and "IF" in item.opname
    ]


class TestCfgWideRegionDataflow:
    def test_equal_constants_survive_branch_merge(self):
        @inline_calls(region_dataflow=True)
        def function(value, choose_left):
            produced = _cfg_flag()
            if choose_left:
                alias = produced
            else:
                alias = produced
            return _cfg_choose(alias, value)

        assert function(10, True) == 11
        assert function(10, False) == 11
        # Only the caller's choose_left branch remains; _cfg_choose's flag branch
        # has folded after the phi-like merge proves alias=True on both paths.
        assert len(_conditional_jumps(function)) == 1
        stats = function.__inline_stats__
        assert stats.cfg_merge_facts >= 1
        assert stats.cfg_constant_propagations >= 1
        assert stats.cfg_branches_folded >= 1

    def test_different_constants_do_not_merge(self):
        @inline_calls(region_dataflow=True)
        def function(value, choose_left):
            produced = _cfg_flag()
            if choose_left:
                alias = produced
            else:
                alias = False
            return _cfg_choose(alias, value)

        assert function(10, True) == 11
        assert function(10, False) == 9
        # Caller branch plus downstream _cfg_choose branch remain.
        assert len(_conditional_jumps(function)) >= 2

    def test_dynamic_inlined_result_version_merges_under_always_policy(self):
        @inline_calls(policy="always", region_dataflow=True)
        def function(value, choose_left):
            produced = _cfg_identity(value)
            if choose_left:
                alias = produced
            else:
                alias = produced
            return _cfg_scale(alias)

        for value in (-5, 0, 8):
            assert function(value, True) == value * 3 + 1
            assert function(value, False) == value * 3 + 1
        assert function.__inline_stats__.cfg_copy_propagations >= 1

    def test_remaining_call_kills_cfg_fact(self):
        def opaque():
            return None

        @inline_calls(region_dataflow=True)
        def function(value, choose_left):
            produced = _cfg_flag()
            if choose_left:
                alias = produced
            else:
                alias = produced
            opaque()
            return _cfg_choose(alias, value)

        assert function(10, True) == 11
        assert function(10, False) == 11
        # The remaining CALL is an observability barrier, so choose's branch stays.
        assert len(_conditional_jumps(function)) >= 2

    def test_source_rewrite_after_merge_does_not_change_captured_alias(self):
        @inline_calls(policy="always", region_dataflow=True)
        def function(value, choose_left):
            produced = _cfg_identity(value)
            if choose_left:
                alias = produced
            else:
                alias = produced
            produced = value + 100
            return alias, produced

        assert function(7, True) == (7, 107)
        assert function(7, False) == (7, 107)

    def test_nested_forward_branch_merges_same_constant(self):
        @inline_calls(region_dataflow=True)
        def function(value, first, second):
            produced = _cfg_flag()
            if first:
                if second:
                    alias = produced
                else:
                    alias = produced
            else:
                alias = produced
            return _cfg_choose(alias, value)

        for first in (False, True):
            for second in (False, True):
                assert function(3, first, second) == 4
        stats = function.__inline_stats__
        assert stats.cfg_merge_facts >= 1
        assert stats.cfg_branches_folded >= 1


def test_cfg_fixed_point_exposes_second_order_merge():
    @inline_calls(region_dataflow=True)
    def function(value, outer):
        produced = _cfg_flag()
        if outer:
            first = produced
        else:
            first = produced
        if first:
            alias = True
        else:
            alias = False
        return _cfg_choose(alias, value)

    assert function(10, True) == 11
    assert function(10, False) == 11
    # CFG round 1 proves first=True at the first merge and removes the branch that
    # defines alias=False.  Rebuilding the CFG lets round 2 prove alias=True at the
    # next merge and fold the downstream _cfg_choose branch.
    assert len(_conditional_jumps(function)) == 1
    stats = function.__inline_stats__
    assert stats.cfg_branches_folded >= 2
    assert stats.cfg_dataflow_rounds >= 2
