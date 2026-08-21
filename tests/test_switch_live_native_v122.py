from __future__ import annotations

import sys
import importlib

import pytest

from python_extensions import case, enable_switch, switch
switch_module = importlib.import_module("python_extensions.switch")


pytest.importorskip("python_extensions._livegate")


def _compile_cases(
    keys,
    *,
    case_key_mode: str = "python",
    live_engine: str = "native",
    mode: str = "fast",
):
    lines = ["def route(value):", "    with switch(value):"]
    for index, key in enumerate(keys):
        lines.extend(
            [
                f"        if case({key!r}):",
                f"            return {index}",
            ]
        )
    lines.extend(["        if case():", "            return -1"])
    source = "\n".join(lines) + "\n"
    namespace = {"switch": switch, "case": case}
    exec(compile(source, "<native-live-v122>", "exec"), namespace)
    return enable_switch(
        mode=mode,
        live_engine=live_engine,
        case_key_mode=case_key_mode,
        source=source,
    )(namespace["route"])


def _info(fn):
    return fn.__pyswitch_native_dispatch_info__[0]


def test_native_engine_is_explicit_and_ctypes_remains_available():
    native = _compile_cases(range(8), live_engine="native")
    ctypes_fn = _compile_cases(range(8), live_engine="ctypes")

    assert native.__pyswitch_live_engine__ == "native-fused-v1"
    assert native.__pyswitch_native_accelerated__ is True
    assert ctypes_fn.__pyswitch_live_engine__ == "ctypes-store-v1"
    assert ctypes_fn.__pyswitch_native_accelerated__ is False
    assert [native(i) for i in range(-1, 10)] == [
        ctypes_fn(i) for i in range(-1, 10)
    ]


def test_native_contiguous_integer_lane():
    fn = _compile_cases(range(64))
    info = _info(fn)
    assert info["lookup_strategy"] == "contiguous-int"
    assert info["dense_span"] == 64
    assert [fn(i) for i in (-1, 0, 7, 63, 64)] == [-1, 0, 7, 63, -1]


def test_native_sparse_integer_hash_lane():
    keys = [1 + index * 1009 for index in range(64)]
    fn = _compile_cases(keys)
    info = _info(fn)
    assert info["lookup_strategy"] == "int-hash"
    assert info["int_hash_capacity"] >= 128
    for index in (0, 1, 17, 42, 63):
        assert fn(keys[index]) == index
    assert fn(keys[-1] + 1) == -1


def test_python_key_mode_disables_integer_lane_for_mixed_keys():
    keys = [1, 1009, 2017, 3023, "marker"]
    fn = _compile_cases(keys, case_key_mode="python")
    info = _info(fn)
    assert info["lookup_strategy"] == "none"
    # Normal dict semantics intentionally make True collide with integer 1.
    assert fn(True) == 0
    assert fn("marker") == 4


def test_typed_mode_can_optimize_integer_partition_without_cross_type_collisions():
    keys = [1, 1009, 2017, 3023, "marker"]
    fn = _compile_cases(keys, case_key_mode="typed")
    info = _info(fn)
    assert info["lookup_strategy"] == "int-hash"
    assert fn(1) == 0
    assert fn(True) == -1
    assert fn(1.0) == -1
    assert fn("marker") == 4


def test_huge_integer_cases_fall_back_only_when_the_selector_overflows_i64():
    huge = 10**100
    keys = [1, 1009, 2017, 3023, huge]
    fn = _compile_cases(keys)
    info = _info(fn)
    assert info["lookup_strategy"] == "int-hash"
    assert info["int_has_huge"] is True
    assert fn(2017) == 2
    assert fn(huge) == 4
    assert fn(huge + 1) == -1


def test_integer_subclass_uses_python_hash_semantics_not_raw_lane():
    keys = [1 + index * 1009 for index in range(16)]
    fn = _compile_cases(keys)

    class ObservedInt(int):
        calls = 0

        def __hash__(self):
            type(self).calls += 1
            return super().__hash__()

    value = ObservedInt(keys[5])
    assert fn(value) == 5
    assert ObservedInt.calls == 1

    class FailingInt(int):
        def __hash__(self):
            raise TypeError("user hash failure")

    with pytest.raises(TypeError, match="user hash failure"):
        fn(FailingInt(keys[2]))


def test_native_subject_expression_is_evaluated_once():
    source = """\
def route(box):
    with switch(box.pop()):
        if case(1):
            return 10
        if case(2):
            return 20
        if case():
            return -1
"""
    namespace = {"switch": switch, "case": case}
    exec(compile(source, "<native-live-once-v122>", "exec"), namespace)
    fn = enable_switch(mode="fast", live_engine="native", source=source)(
        namespace["route"]
    )
    values = [99, 1]
    assert fn(values) == 10
    assert values == [99]


def test_native_live_gate_survives_local_pep669_line_and_jump_monitoring():
    if not hasattr(sys, "monitoring"):
        pytest.skip("PEP 669 monitoring unavailable")

    fn = _compile_cases([1, 2, 3, 4])
    monitoring = sys.monitoring
    tool_id = next(
        (tool for tool in range(6) if monitoring.get_tool(tool) is None),
        None,
    )
    if tool_id is None:
        pytest.skip("no free sys.monitoring tool id")

    seen = 0

    def callback(*_args):
        nonlocal seen
        seen += 1

    events = monitoring.events.LINE | monitoring.events.JUMP
    monitoring.use_tool_id(tool_id, "pyswitch-native-v122-test")
    try:
        monitoring.register_callback(tool_id, monitoring.events.LINE, callback)
        monitoring.register_callback(tool_id, monitoring.events.JUMP, callback)
        monitoring.set_local_events(tool_id, fn.__code__, events)
        for _ in range(100):
            assert [fn(value) for value in (1, 4, 99)] == [0, 3, -1]
        assert seen > 0
    finally:
        monitoring.set_local_events(tool_id, fn.__code__, 0)
        monitoring.register_callback(tool_id, monitoring.events.LINE, None)
        monitoring.register_callback(tool_id, monitoring.events.JUMP, None)
        monitoring.free_tool_id(tool_id)

    assert [fn(value) for value in (1, 4, 99)] == [0, 3, -1]


def test_free_threaded_build_guard_rejects_live_backend(monkeypatch):
    monkeypatch.setattr(switch_module, "_FREE_THREADED_BUILD", True)
    assert "free-threaded" in switch_module._live_runtime_reason()
