from __future__ import annotations

import dis

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    optimize_extensions,
    unregister_inline_function,
    switch,
)


@inline_function(register_only=True)
def _descriptor_inc(value):
    return value + 1


def test_goto_accepts_descriptor_outer_order():
    class C:
        @enable_goto
        @staticmethod
        def static(value):
            if value:
                goto .done
            value = 4
            label .done
            return value

        @enable_goto
        @classmethod
        def klass(cls, value):
            if value:
                goto .done
            value = cls.base
            label .done
            return value

        base = 7

    assert C.static(1) == 1
    assert C.static(0) == 4
    assert C.klass(1) == 1
    assert C.klass(0) == 7


def test_inline_calls_accepts_descriptors():
    class C:
        @inline_calls
        @staticmethod
        def static(value):
            return _descriptor_inc(value)

        @inline_calls
        @classmethod
        def klass(cls, value):
            return _descriptor_inc(value) + cls.bias

        bias = 3

    assert C.static(4) == 5
    assert C.klass(4) == 8
    assert not any(i.opname == "LOAD_GLOBAL" and i.argval == "_descriptor_inc" for i in dis.get_instructions(C.static))
    assert not any(i.opname == "LOAD_GLOBAL" and i.argval == "_descriptor_inc" for i in dis.get_instructions(C.klass))


def test_inline_function_register_only_accepts_staticmethod_descriptor():
    class C:
        @inline_function(register_only=True)
        @staticmethod
        def helper(value):
            return value * 2

    assert C.helper(5) == 10
    assert C.__dict__["helper"].__func__.__inline_stats__.calls_inlined == 0


def test_composed_pipeline_accepts_staticmethod_descriptor():
    class C:
        @optimize_extensions(switch=True, inline=True, goto=True)
        @staticmethod
        def run(value, rounds):
            total = 0
            label .again
            total = _descriptor_inc(total)
            with switch(value):
                if case(1):
                    bonus = 10
                if case():
                    bonus = 20
            if total < rounds:
                goto .again
            return total + bonus

    assert C.run(1, 4) == 14
    assert C.run(9, 4) == 24
    fn = C.__dict__["run"].__func__
    assert fn.__python_extensions_pipeline__ == ("switch", "inline", "goto")
    assert [r.feature for r in fn.__python_extensions_reports__[-3:]] == ["switch", "inline", "goto"]


def test_composed_pipeline_accepts_classmethod_descriptor():
    class C:
        bias = 5

        @optimize_extensions(switch=True, inline=True, goto=True)
        @classmethod
        def run(cls, value, rounds):
            total = 0
            label .again
            total = _descriptor_inc(total)
            with switch(value):
                if case(1):
                    bonus = cls.bias
                if case():
                    bonus = 20
            if total < rounds:
                goto .again
            return total + bonus

    assert C.run(1, 4) == 9
    assert C.run(9, 4) == 24
    assert C.__dict__["run"].__func__.__python_extensions_pipeline__ == ("switch", "inline", "goto")

class _InlineDescriptorHelpers:
    bias = 4

    @inline_function(register_only=True)
    @staticmethod
    def twice(value):
        return value * 2

    @inline_function(register_only=True)
    @classmethod
    def plus_bias(cls, value):
        return value + cls.bias


def test_registered_staticmethod_descriptor_call_inlines_through_owner():
    @inline_calls
    def caller(value):
        return _InlineDescriptorHelpers.twice(value)

    assert caller(5) == 10
    assert caller.__inline_stats__.calls_inlined == 1
    assert not any(
        item.opname == "LOAD_GLOBAL" and item.argval == "_InlineDescriptorHelpers"
        for item in dis.get_instructions(caller, adaptive=False)
    )


def test_registered_classmethod_descriptor_call_inlines_with_exact_class_receiver():
    @inline_calls
    def caller(value):
        return _InlineDescriptorHelpers.plus_bias(value)

    assert caller(5) == 9
    assert caller.__inline_stats__.calls_inlined == 1


def test_unregister_inline_function_accepts_bound_classmethod():
    class Local:
        @inline_function(register_only=True)
        @classmethod
        def helper(cls, value):
            return value + 1

    assert unregister_inline_function(Local.helper) is True
    assert unregister_inline_function(Local.helper) is False
