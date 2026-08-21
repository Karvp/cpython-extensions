from __future__ import annotations

import dis

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _unary_defaults(value=-7, mask=5, truth=()):
    return -value, ~mask, not truth


@inline_function(register_only=True)
def _kw_helper(*, value=3):
    return value + 1


@inline_function(register_only=True)
def _positive_default(value=-9):
    return +value


def test_inline_folds_safe_constant_unary_ops_from_defaults():
    @inline_calls(policy="speed")
    def caller():
        return _unary_defaults()

    assert caller() == (7, -6, True)
    stats = caller.__inline_stats__
    assert stats.constant_unary_ops_folded >= 3
    report = caller.__python_extensions_report__.as_dict()
    assert report["constant_unary_ops_folded"] >= 3
    ops = [i.opname for i in dis.get_instructions(caller, adaptive=False)]
    assert "UNARY_NEGATIVE" not in ops
    assert "UNARY_INVERT" not in ops
    assert "UNARY_NOT" not in ops


def test_inline_unary_folding_does_not_execute_user_protocols_at_decoration():
    events = []

    class Loud:
        def __neg__(self):
            events.append("neg")
            return 99
        def __bool__(self):
            events.append("bool")
            return True

    loud = Loud()

    @inline_function(register_only=True)
    def helper(value=loud):
        return -value, not value

    @inline_calls(policy="speed")
    def caller():
        return helper()

    assert events == []
    assert caller() == (99, False)
    assert events == ["neg", "bool"]


def test_rebuilt_inline_function_owns_kwdefaults_mapping():
    @inline_calls(policy="speed")
    def caller(*, scale=2):
        return _kw_helper() * scale

    original = caller.__inline_original__
    assert caller.__inline_stats__.calls_inlined == 1
    assert caller.__kwdefaults__ == original.__kwdefaults__ == {"scale": 2}
    assert caller.__kwdefaults__ is not original.__kwdefaults__
    caller.__kwdefaults__["scale"] = 7
    assert original.__kwdefaults__ == {"scale": 2}


def test_inline_folds_cpython313_unary_positive_intrinsic():
    @inline_calls(policy="speed")
    def caller():
        return _positive_default()

    assert caller() == -9
    assert caller.__inline_stats__.constant_unary_ops_folded >= 1
    assert not any(
        item.opname == "CALL_INTRINSIC_1"
        for item in dis.get_instructions(caller, adaptive=False)
    )
