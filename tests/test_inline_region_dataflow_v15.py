from __future__ import annotations

import dis
import sys

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _region_flag():
    return True


@inline_function(register_only=True)
def _region_choose(flag, value):
    if flag:
        return value + 1
    return value - 1


@inline_function(register_only=True)
def _region_identity(value):
    return value


@inline_function(register_only=True)
def _region_scale(value):
    return value * 3 + 1


@inline_calls(region_dataflow=False)
def _region_baseline(value):
    produced = _region_flag()
    alias = produced
    return _region_choose(alias, value)


@inline_calls(region_dataflow=True)
def _region_optimized(value):
    produced = _region_flag()
    alias = produced
    return _region_choose(alias, value)


class TestWholeRegionDataflow:
    def test_constant_flows_through_ordinary_caller_copy(self):
        assert _region_baseline(10) == 11
        assert _region_optimized(10) == 11
        assert len(_region_optimized.__code__.co_code) < len(_region_baseline.__code__.co_code)
        stats = _region_optimized.__inline_stats__
        assert stats.region_constant_propagations + stats.region_copy_propagations >= 1
        assert stats.region_branches_folded >= 1

    def test_safe_region_keeps_caller_bindings(self):
        @inline_calls(region_dataflow=True)
        def function(value):
            produced = _region_flag()
            alias = produced
            result = _region_choose(alias, value)
            snapshot = locals().copy()
            return result, snapshot["produced"], snapshot["alias"]

        assert function(4) == (5, True, True)

    def test_region_dataflow_can_be_disabled(self):
        names = [instruction.opname for instruction in dis.get_instructions(_region_baseline)]
        assert "POP_JUMP_IF_FALSE" in names
        assert _region_baseline.__inline_stats__.region_dataflow_rounds == 0

    def test_region_dataflow_folds_downstream_arithmetic(self):
        @inline_calls(region_dataflow=True)
        def function():
            produced = _region_identity(2)
            alias = produced
            return _region_scale(alias)

        assert function() == 7
        instructions = list(dis.get_instructions(function, adaptive=False))
        # The downstream multiply/add have collapsed to one constant.
        assert not any(item.opname == "BINARY_OP" for item in instructions)
        assert any(item.opname == "LOAD_CONST" and item.argval == 7 for item in instructions)

    def test_source_reassignment_blocks_copy_substitution(self):
        @inline_calls(region_dataflow=True)
        def function(value):
            produced = _region_identity(value)
            alias = produced
            produced = value + 100
            return alias, produced

        assert function(7) == (7, 107)

    def test_multiple_caller_copy_hops_reach_later_callee(self):
        @inline_calls(region_dataflow=True)
        def function(value):
            produced = _region_flag()
            first = produced
            second = first
            return _region_choose(second, value)

        assert function(9) == 10
        stats = function.__inline_stats__
        assert stats.region_constant_propagations + stats.region_copy_propagations >= 2
        assert stats.region_branches_folded >= 1



    def test_remaining_call_is_a_region_observability_barrier(self):
        def mutate_caller_alias():
            # CPython 3.13 exposes optimized locals through a write-through f_locals
            # proxy.  A safe region must not substitute a pre-call constant after
            # this arbitrary non-inlined call.
            sys._getframe(1).f_locals["alias"] = False

        @inline_calls(region_dataflow=True)
        def function(value):
            produced = _region_flag()
            alias = produced
            mutate_caller_alias()
            return _region_choose(alias, value)

        assert function(10) == 9
        names = [item.opname for item in dis.get_instructions(function, adaptive=False)]
        assert "POP_JUMP_IF_FALSE" in names

    def test_fused_store_fast_load_result_is_a_region_root(self):
        # CPython 3.13 fuses the first store with the immediately-following load in
        # this exact shape.  The result destination must still be tracked.
        @inline_calls(region_dataflow=True)
        def function(value):
            produced = _region_flag()
            alias = produced
            return _region_choose(alias, value)

        assert function(2) == 3
        assert function.__inline_stats__.region_dataflow_rounds >= 1
