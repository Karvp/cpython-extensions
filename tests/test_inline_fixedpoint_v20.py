from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _nested_constants(a=3, b=4):
    return -(a + b), not (a == b), ~(a + b)


def test_constant_expression_folding_reaches_fixed_point():
    @inline_calls(policy="speed")
    def caller():
        return _nested_constants()

    assert caller() == (-7, True, -8)
    stats = caller.__inline_stats__
    assert stats.constant_binary_ops_folded >= 2
    assert stats.constant_comparisons_folded >= 1
    # These unary folds are exposed only after binary/comparison folding.
    assert stats.constant_unary_ops_folded >= 3
    ops = [item.opname for item in dis.get_instructions(caller, adaptive=False)]
    assert "UNARY_NEGATIVE" not in ops
    assert "UNARY_INVERT" not in ops
    assert "UNARY_NOT" not in ops


def test_fixed_point_folding_does_not_touch_dynamic_unary_protocols():
    events = []

    class Loud:
        def __neg__(self):
            events.append("neg")
            return 10

    @inline_function(register_only=True)
    def helper(value):
        return -value

    @inline_calls(policy="speed")
    def caller(value):
        return helper(value)

    assert events == []
    assert caller(Loud()) == 10
    assert events == ["neg"]

