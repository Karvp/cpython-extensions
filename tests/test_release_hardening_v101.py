from __future__ import annotations

import dis

import pytest

from python_extensions.inline import (
    InlineRecursionError,
    clear_inline_registry,
    inline_calls,
    inline_function,
    registered_inline_functions,
    unregister_inline_function,
)


def teardown_function() -> None:
    clear_inline_registry()


def _has_runtime_call(func) -> bool:
    return any(item.opname.startswith("CALL") for item in dis.get_instructions(func))


def test_failed_registration_is_identity_and_metadata_atomic() -> None:
    namespace = globals()
    exec(
        "def FAILED_RECURSIVE(value):\n"
        "    return 0 if value <= 0 else FAILED_RECURSIVE(value - 1) + 1\n",
        namespace,
    )
    recursive = namespace["FAILED_RECURSIVE"]
    before = dict(recursive.__dict__)
    try:
        with pytest.raises(InlineRecursionError):
            inline_function(recursive)

        assert registered_inline_functions() == ()
        assert recursive.__dict__ == before

        namespace["FAILED_ALIAS"] = recursive

        @inline_calls(policy="always")
        def caller(value: int) -> int:
            return FAILED_ALIAS(value)

        assert caller(7) == 7
        assert _has_runtime_call(caller)
        assert caller.__inline_stats__.calls_inlined == 0
    finally:
        namespace.pop("FAILED_ALIAS", None)
        namespace.pop("FAILED_RECURSIVE", None)


def test_replacing_registration_revokes_old_alias_identity() -> None:
    namespace = globals()
    exec("def REPLACEABLE_V101(value):\n    return value + 1\n", namespace)
    first = inline_function(register_only=True)(namespace["REPLACEABLE_V101"])
    namespace["OLD_REPLACEABLE_ALIAS"] = first
    exec("def REPLACEABLE_V101(value):\n    return value + 100\n", namespace)
    second = inline_function(register_only=True)(namespace["REPLACEABLE_V101"])
    assert second(1) == 101

    try:
        @inline_calls(policy="always")
        def old_caller(value: int) -> int:
            return OLD_REPLACEABLE_ALIAS(value)

        assert old_caller(5) == 6
        assert _has_runtime_call(old_caller)
        assert old_caller.__inline_stats__.calls_inlined == 0
    finally:
        namespace.pop("OLD_REPLACEABLE_ALIAS", None)
        namespace.pop("REPLACEABLE_V101", None)


def test_unregister_transformed_registration_revokes_original_identity() -> None:
    namespace = globals()

    @inline_function(register_only=True)
    def registry_helper_v101(value: int) -> int:
        return value + 1

    namespace["REGISTRY_HELPER_V101"] = registry_helper_v101
    exec(
        "def REGISTRY_OUTER_V101(value):\n"
        "    return REGISTRY_HELPER_V101(value) * 2\n",
        namespace,
    )
    outer = namespace["REGISTRY_OUTER_V101"]
    transformed = inline_function(outer)
    original = transformed.__inline_original__
    assert transformed is not original
    assert unregister_inline_function(transformed)

    namespace["UNREGISTERED_ORIGINAL_ALIAS"] = original
    try:
        @inline_calls(policy="always")
        def caller(value: int) -> int:
            return UNREGISTERED_ORIGINAL_ALIAS(value)

        assert caller(5) == 12
        assert _has_runtime_call(caller)
        assert caller.__inline_stats__.calls_inlined == 0
    finally:
        namespace.pop("UNREGISTERED_ORIGINAL_ALIAS", None)
        namespace.pop("REGISTRY_OUTER_V101", None)
        namespace.pop("REGISTRY_HELPER_V101", None)


def test_same_named_methods_in_distinct_classes_coexist() -> None:
    class Left:
        @inline_function(register_only=True)
        @staticmethod
        def apply(value: int) -> int:
            return value + 10

    class Right:
        @inline_function(register_only=True)
        @staticmethod
        def apply(value: int) -> int:
            return value + 100

    globals()["REGISTRY_LEFT_V101"] = Left
    globals()["REGISTRY_RIGHT_V101"] = Right
    try:
        @inline_calls(policy="always")
        def caller(value: int) -> tuple[int, int]:
            return REGISTRY_LEFT_V101.apply(value), REGISTRY_RIGHT_V101.apply(value)

        assert caller(3) == (13, 103)
        attrs = [item.argval for item in dis.get_instructions(caller) if item.opname == "LOAD_ATTR"]
        assert "apply" not in attrs
        names = registered_inline_functions()
        assert any(name.endswith("Left.apply") for name in names)
        assert any(name.endswith("Right.apply") for name in names)
    finally:
        globals().pop("REGISTRY_LEFT_V101", None)
        globals().pop("REGISTRY_RIGHT_V101", None)


def test_distinct_factory_functions_with_same_qualname_coexist() -> None:
    def factory(delta: int):
        @inline_function(register_only=True, freeze_closures=True)
        def apply(value: int) -> int:
            return value + delta

        return apply

    first = factory(4)
    second = factory(9)
    globals()["FACTORY_FIRST_V101"] = first
    globals()["FACTORY_SECOND_V101"] = second
    try:
        @inline_calls(policy="always")
        def caller(value: int) -> tuple[int, int]:
            return FACTORY_FIRST_V101(value), FACTORY_SECOND_V101(value)

        assert caller(5) == (9, 14)
        assert not _has_runtime_call(caller)
    finally:
        globals().pop("FACTORY_FIRST_V101", None)
        globals().pop("FACTORY_SECOND_V101", None)


def test_failed_replacement_restores_previous_registration() -> None:
    namespace = globals()
    exec("def RESTORE_SLOT_V101(value):\n    return value + 3\n", namespace)
    previous = inline_function(register_only=True)(namespace["RESTORE_SLOT_V101"])
    namespace["RESTORE_OLD_ALIAS_V101"] = previous
    exec(
        "def RESTORE_SLOT_V101(value):\n"
        "    return 0 if value <= 0 else RESTORE_SLOT_V101(value - 1) + 1\n",
        namespace,
    )
    failed = namespace["RESTORE_SLOT_V101"]
    try:
        with pytest.raises(InlineRecursionError):
            inline_function(failed)
        assert not any(name.startswith("__inline_") for name in failed.__dict__)

        @inline_calls(policy="always")
        def caller(value: int) -> int:
            return RESTORE_OLD_ALIAS_V101(value)

        assert caller(7) == 10
        assert not _has_runtime_call(caller)
    finally:
        namespace.pop("RESTORE_OLD_ALIAS_V101", None)
        namespace.pop("RESTORE_SLOT_V101", None)


def test_ephemeral_local_registration_is_removed_by_weakref_callback() -> None:
    import gc
    import python_extensions.inline as inline_module

    def make_one():
        @inline_function(register_only=True)
        def ephemeral(value: int) -> int:
            return value + 1

        return ephemeral

    ephemeral = make_one()
    key = inline_module._registry_key_for_function(ephemeral)
    assert key in inline_module._registry
    del ephemeral
    for _ in range(3):
        gc.collect()
    assert key not in inline_module._registry


def test_switch_accepts_qualified_root_api_markers() -> None:
    import python_extensions as pe

    @pe.enable_switch(mode="portable")
    def qualified(value: int) -> list[str]:
        result: list[str] = []
        with pe.switch(value):
            if pe.case(1):
                result.append("one")
                pe.fallthrough()
            if pe.case(2):
                result.append("two")
            if pe.case():
                result.append("default")
        return result

    assert qualified(1) == ["one", "two"]
    assert qualified(2) == ["two"]
    assert qualified(9) == ["default"]


def test_switch_accepts_marker_aliases_by_exact_identity() -> None:
    import python_extensions as pe

    switch_alias = pe.switch
    case_alias = pe.case

    @pe.enable_switch(mode="portable")
    def aliased(value: int) -> str:
        with switch_alias(value):
            if case_alias(3):
                return "three"
            if case_alias():
                return "other"
        raise AssertionError("unreachable")

    assert aliased(3) == "three"
    assert aliased(8) == "other"
