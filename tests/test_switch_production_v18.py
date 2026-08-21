from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import pytest

from python_extensions import (
    DuplicateCaseError,
    SwitchSyntaxError,
    case,
    enable_switch,
    fallthrough,
    switch,
)


class Token(Enum):
    A = "a"
    B = "b"


class EqualOneWrongHash:
    def __hash__(self):
        return 99_991

    def __eq__(self, other):
        return other == 1


class EqualOneUnhashable:
    __hash__ = None

    def __eq__(self, other):
        return other == 1


class HashRaisesTypeError:
    def __hash__(self):
        raise TypeError("hash exploded")


@enable_switch(mode="portable")
def hash_semantics(value):
    with switch(value):
        if case(1):
            return "one"
        if case():
            return "default"


@enable_switch(mode="portable", case_key_mode="typed")
def typed_semantics(value):
    with switch(value):
        if case(1):
            return "int"
        if case(1.0):
            return "float"
        if case(True):
            return "bool"
        if case():
            return "default"


@enable_switch(mode="portable")
def constant_identity(value):
    with switch(value):
        if case(1):
            selected = (1_000_001, 2_000_002)
        if case(2):
            selected = (3, 4)
        if case(3):
            selected = (5, 6)
        if case():
            selected = (7, 8)
    canonical = (1_000_001, 2_000_002)
    return selected is canonical


@enable_switch(mode="portable")
def template_identity(value):
    target = (8_000_001, 9_000_002)
    with switch(value):
        if case(1):
            return id(target) == id((8_000_001, 9_000_002))
        if case(2):
            return id(target) == id((3, 4))
        if case(3):
            return id(target) == id((5, 6))
        if case():
            return id(target) == id((7, 8))


@enable_switch(mode="portable")
def heterogeneous_type_error(value):
    with switch(value):
        if case(1):
            return int("not-an-int")
        if case(2):
            return "ok".upper()
        if case():
            return len(())


@enable_switch(mode="portable")
def guarded(value, flag):
    with switch(value):
        if case(1, when=flag):
            return "guarded"
        elif case(1):
            return "fallback-one"
        elif case():
            return "default"


@enable_switch(mode="portable")
def falling(value):
    out = []
    with switch(value):
        if case(1):
            out.append("one")
            fallthrough()
        if case(2):
            out.append("two")
            fallthrough()
        if case():
            out.append("default")
    return tuple(out)


_counter = 0


def _subject_once(value):
    global _counter
    _counter += 1
    return value


@enable_switch(mode="portable", case_key_mode="typed")
def subject_once(value):
    with switch(_subject_once(value)) as chosen:
        if case(1):
            return chosen, "int"
        if case(True):
            return chosen, "bool"
        if case():
            return chosen, "default"


@enable_switch(mode="portable")
def enum_switch(value):
    with switch(value):
        if case(Token.A):
            return 10
        if case(Token.B):
            return 20
        if case():
            return -1


@enable_switch(mode="portable")
def recursive_switch(n):
    with switch(n):
        if case(0):
            return 0
        if case():
            return 1 + recursive_switch(n - 1)


@enable_switch(mode="portable")
def generator_switch(value):
    with switch(value):
        if case(1):
            yield "one"
        if case():
            yield "default"


@enable_switch(mode="portable")
async def coroutine_switch(value):
    await asyncio.sleep(0)
    with switch(value):
        if case(1):
            return "one"
        if case():
            return "default"


@enable_switch(mode="portable")
async def async_generator_switch(value):
    with switch(value):
        if case(1):
            yield "one"
        if case():
            yield "default"


@enable_switch(mode="portable")
def multiple_switches(left, right):
    with switch(left):
        if case(1):
            a = 10
        if case():
            a = 20
    with switch(right):
        if case("x"):
            b = 1
        if case():
            b = 2
    return a + b


def test_hash_semantics_match_dict_lookup_not_native_match():
    assert hash_semantics(1) == "one"
    assert hash_semantics(True) == "one"
    assert hash_semantics(1.0) == "one"
    assert hash_semantics(EqualOneWrongHash()) == "default"
    assert hash_semantics(EqualOneUnhashable()) == "default"


def test_hash_typeerror_from_hashable_subject_is_not_swallowed():
    with pytest.raises(TypeError, match="hash exploded"):
        hash_semantics(HashRaisesTypeError())


def test_typed_identity_exact_type():
    assert typed_semantics(1) == "int"
    assert typed_semantics(1.0) == "float"
    assert typed_semantics(True) == "bool"
    assert typed_semantics(False) == "default"


def test_literal_constant_identity_is_preserved():
    assert constant_identity(1) is True
    assert constant_identity(2) is False
    assert constant_identity.__pyswitch_backend__ == "portable-direct-value-v18"


def test_expression_template_constant_identity_is_preserved():
    assert template_identity(1) is True
    assert template_identity(2) is False
    assert template_identity.__pyswitch_backend__ == "portable-expression-template-v18"


def test_user_typeerror_in_selected_body_propagates():
    with pytest.raises(ValueError):
        heterogeneous_type_error(1)
    assert heterogeneous_type_error(2) == "OK"
    assert heterogeneous_type_error(99) == 0


def test_guarded_duplicate_keys_are_ordered():
    assert guarded(1, True) == "guarded"
    assert guarded(1, False) == "fallback-one"
    assert guarded(2, True) == "default"


def test_unconditional_duplicate_remains_rejected():
    with pytest.raises(DuplicateCaseError):
        @enable_switch(mode="portable")
        def bad(value):
            with switch(value):
                if case(1):
                    return 1
                if case(1):
                    return 2
                if case():
                    return 3


def test_fallthrough_keeps_caller_control_flow():
    assert falling(1) == ("one", "two", "default")
    assert falling(2) == ("two", "default")
    assert falling(3) == ("default",)


def test_subject_evaluated_once_in_typed_mode_and_alias_preserved():
    global _counter
    _counter = 0
    assert subject_once(1) == (1, "int")
    assert _counter == 1
    assert subject_once(True) == (True, "bool")
    assert _counter == 2


def test_qualified_enum_constants():
    assert enum_switch(Token.A) == 10
    assert enum_switch(Token.B) == 20
    assert enum_switch("a") == -1


def test_recursion_portable():
    assert recursive_switch(20) == 20


def test_generator_coroutine_and_async_generator():
    assert list(generator_switch(1)) == ["one"]
    assert list(generator_switch(9)) == ["default"]
    assert asyncio.run(coroutine_switch(1)) == "one"

    async def collect():
        return [item async for item in async_generator_switch(9)]

    assert asyncio.run(collect()) == ["default"]


def test_multiple_switch_metadata_and_semantics():
    assert multiple_switches(1, "x") == 11
    assert multiple_switches(0, "y") == 22
    assert multiple_switches.__pyswitch_switch_count__ == 2


def test_portable_is_thread_safe_under_shared_function():
    inputs = [1, True, 1.0, 2, "x"] * 1000
    expected = [hash_semantics(v) for v in inputs]
    with ThreadPoolExecutor(max_workers=12) as pool:
        actual = list(pool.map(hash_semantics, inputs))
    assert actual == expected


def test_nested_lexical_function_is_not_implicitly_rewritten():
    @enable_switch(mode="portable")
    def outer(value):
        def inner(inner_value):
            with switch(inner_value):
                if case(1):
                    return "inner"
                if case():
                    return "inner-default"

        with switch(value):
            if case(1):
                return "outer"
            if case():
                return inner(1)

    assert outer(1) == "outer"
    assert outer.__pyswitch_switch_count__ == 1
    with pytest.raises(RuntimeError, match=r"switch\(\) requires @enable_switch"):
        outer(2)


def test_nested_decorated_function_can_opt_in_independently():
    @enable_switch(mode="portable")
    def outer(value):
        @enable_switch(mode="portable")
        def inner(inner_value):
            with switch(inner_value):
                if case(1):
                    return "inner"
                if case():
                    return "inner-default"

        with switch(value):
            if case(1):
                return "outer"
            if case():
                return inner(1)

    assert outer(1) == "outer"
    assert outer(2) == "inner"
    assert outer.__pyswitch_switch_count__ == 1


def test_auto_live_rejection_falls_back_with_typed_semantics_and_report():
    @enable_switch(mode="auto", live_threshold=0, case_key_mode="typed")
    def fallback(value):
        with switch(value):
            if case(1, when=True):
                return "int"
            if case():
                return "default"

    assert fallback(1) == "int"
    assert fallback(True) == "default"
    assert fallback.__pyswitch_case_key_mode__ == "typed"
    assert fallback.__pyswitch_mode__ == "portable"
    assert fallback.__python_extensions_report__.feature == "switch"


def test_metadata_signature_and_doc_are_preserved():
    @enable_switch(mode="portable")
    def sample(a: int, /, b: int = 2, *, c: int = 3) -> int:
        "sample doc"
        with switch(a):
            if case(1):
                return b + c
            if case():
                return -1

    assert str(inspect.signature(sample)) == "(a: 'int', /, b: 'int' = 2, *, c: 'int' = 3) -> 'int'"
    assert sample.__doc__ == "sample doc"
    assert sample(1, c=5) == 7


@pytest.mark.parametrize(
    "kwargs,exc",
    [
        ({"mode": []}, TypeError),
        ({"unsafe_shared_slot": 1}, TypeError),
        ({"source": 123}, TypeError),
        ({"live_threshold": True}, TypeError),
        ({"portable_match_threshold": 1.5}, TypeError),
        ({"max_cached_depth": -1}, ValueError),
        ({"expose_debug": 1}, TypeError),
        ({"case_key_mode": []}, TypeError),
    ],
)
def test_configuration_validation(kwargs, exc):
    with pytest.raises(exc):
        enable_switch(**kwargs)


def test_empty_switch_rejected():
    with pytest.raises(SwitchSyntaxError):
        @enable_switch(mode="portable")
        def empty(value):
            with switch(value):
                pass


def test_generated_runtime_dependencies_are_hygienic():
    @enable_switch(mode="portable", case_key_mode="typed")
    def classify(value, type, TypeError):
        # These parameters deliberately shadow builtins used internally by
        # older generated switch code.
        with switch(value):
            if case(1):
                return "int"
            if case(True):
                return "bool"
            if case():
                return "default"

    poison = lambda *_args: (_ for _ in ()).throw(AssertionError("shadow used"))
    assert classify(1, poison, RuntimeError) == "int"
    assert classify(True, poison, RuntimeError) == "bool"
    assert classify([], poison, RuntimeError) == "default"

@enable_switch(mode="portable")
def partial_template(value):
    with switch(value):
        if case(1):
            return value + 10
        if case(2):
            return value + 20
        if case(3):
            return value + 30
        if case():
            return -1


@enable_switch(mode="portable")
def partial_template_default_body(value, log):
    with switch(value):
        if case(1):
            return value * 10
        if case(2):
            return value * 20
        if case():
            log.append("miss")
            return len(log)


@enable_switch(mode="portable")
def partial_template_no_default(value):
    with switch(value):
        if case(1):
            return value + 100
        if case(2):
            return value + 200
    return "after"


def test_partial_template_handles_structurally_different_default():
    assert partial_template.__pyswitch_backend__ == "portable-expression-template-v18"
    assert partial_template(1) == 11
    assert partial_template(2) == 22
    assert partial_template(9) == -1
    assert partial_template([]) == -1


def test_partial_template_retains_arbitrary_default_body_in_caller_frame():
    log = []
    assert partial_template_default_body(1, log) == 10
    assert log == []
    assert partial_template_default_body(9, log) == 1
    assert log == ["miss"]


def test_partial_template_without_default_continues_after_switch():
    assert partial_template_no_default(1) == 101
    assert partial_template_no_default(9) == "after"

_meta_hash_log = []


class _LoggingMeta(type):
    def __getattribute__(cls, name):
        if name == "__hash__":
            _meta_hash_log.append(name)
        return super().__getattribute__(name)


class _MetaUnhashable(metaclass=_LoggingMeta):
    __hash__ = None


@enable_switch(mode="portable")
def metaclass_unhashable(value):
    with switch(value):
        if case(1):
            return "hit"
        if case():
            return "miss"


class _RejectingTarget:
    @property
    def value(self):
        return 0

    @value.setter
    def value(self, new_value):
        raise TypeError(f"setter rejected {new_value}")


@enable_switch(mode="portable")
def direct_assignment_target_error(key, target):
    with switch(key):
        if case(1):
            target.value = 10
        if case():
            target.value = 20


@enable_switch(mode="portable")
def switch_loop_control(values):
    out = []
    for value in values:
        with switch(value):
            if case(0):
                continue
            if case(1):
                break
            if case():
                out.append(value)
    return out


class _DemoMethods:
    @enable_switch(mode="portable")
    @staticmethod
    def static_outer(value):
        with switch(value):
            if case("x"):
                return 1
            if case():
                return 0

    @classmethod
    @enable_switch(mode="portable")
    def class_outer(cls, value):
        with switch(value):
            if case("x"):
                return cls.__name__
            if case():
                return "miss"


def test_unhashable_detection_does_not_invoke_metaclass_attribute_hook():
    _meta_hash_log.clear()
    assert metaclass_unhashable(_MetaUnhashable()) == "miss"
    assert _meta_hash_log == []


def test_direct_assignment_target_type_error_is_not_swallowed():
    with pytest.raises(TypeError, match="setter rejected 10"):
        direct_assignment_target_error(1, _RejectingTarget())
    with pytest.raises(TypeError, match="setter rejected 20"):
        direct_assignment_target_error(9, _RejectingTarget())


def test_break_and_continue_remain_in_enclosing_loop():
    assert switch_loop_control([2, 0, 3, 1, 4]) == [2, 3]


def test_staticmethod_and_classmethod_decorator_composition():
    assert _DemoMethods.static_outer("x") == 1
    assert _DemoMethods.static_outer("y") == 0
    assert _DemoMethods.class_outer("x") == "_DemoMethods"
    assert _DemoMethods.class_outer("y") == "miss"

@enable_switch(mode="portable")
def statement_template(value):
    with switch(value):
        if case(1):
            y = value + 10
            y *= 2
            return y
        if case(2):
            y = value + 20
            y *= 2
            return y
        if case(3):
            y = value + 30
            y *= 2
            return y
        if case():
            return -1


@enable_switch(mode="portable")
def statement_template_branchless(value):
    with switch(value):
        if case(1):
            y = value + 10
            return y * 2
        if case(2):
            y = value + 20
            return y * 3
        if case():
            y = value + 30
            return y * 4


@enable_switch(mode="portable")
def statement_template_user_error(value):
    with switch(value):
        if case(1):
            y = int("bad-one")
            return y + 10
        if case(2):
            y = int("bad-two")
            return y + 20
        if case():
            return -1


def test_statement_template_handles_multi_statement_routes_in_o1_table_path():
    assert statement_template.__pyswitch_backend__ == "portable-statement-template-v18"
    assert statement_template.__pyswitch_statement_template_plan_count__ == 1
    assert statement_template(1) == 22
    assert statement_template(2) == 44
    assert statement_template(3) == 66
    assert statement_template(9) == -1
    assert statement_template([]) == -1


def test_statement_template_can_include_same_shape_default_without_miss_branch():
    assert statement_template_branchless.__pyswitch_backend__ == "portable-statement-template-v18"
    assert statement_template_branchless(1) == 22
    assert statement_template_branchless(2) == 66
    assert statement_template_branchless(9) == 156


def test_statement_template_user_type_error_is_not_treated_as_lookup_failure():
    with pytest.raises(ValueError, match="invalid literal"):
        statement_template_user_error(1)


def test_duplicate_equivalent_keys_inside_one_guarded_clause_are_rejected():
    source = '''def bad(value, flag):\n    with switch(value):\n        if case(1, True, when=flag):\n            return "hit"\n        if case():\n            return "miss"\n'''
    namespace = {"switch": switch, "case": case}
    exec(source, namespace)
    with pytest.raises(DuplicateCaseError):
        enable_switch(mode="portable", source=source)(namespace["bad"])
