from __future__ import annotations

import sys

import pytest

from python_extensions import case, enable_switch, switch


def _compile(source: str, *, case_key_mode: str = "python", extra=None):
    ns = {"switch": switch, "case": case}
    if extra:
        ns.update(extra)
    exec(compile(source, "<pyswitch-stack-payload>", "exec"), ns)
    name = next(
        node.name
        for node in __import__("ast").parse(source).body
        if isinstance(node, (__import__("ast").FunctionDef, __import__("ast").AsyncFunctionDef))
    )
    return enable_switch(
        mode="portable", case_key_mode=case_key_mode, source=source
    )(ns[name])


def _hidden(frame):
    return tuple(sorted(k for k in frame.f_locals if k.startswith("__pyswitch_")))


def test_expression_template_payload_is_stack_resident_during_user_call():
    observed = []

    def probe():
        observed.append(_hidden(sys._getframe(1)))
        return 1

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return probe() + 10
        if case(2):
            return probe() + 20
        if case():
            return probe() + 30
''',
        extra={"probe": probe},
    )
    assert fn.__pyswitch_backend__ == "portable-expression-template-v18"
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert fn.__pyswitch_stack_payload_fallbacks__ == ()
    assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    assert [fn(1), fn(2), fn(9)] == [11, 21, 31]
    assert observed == [(), (), ()]


def test_statement_template_payload_is_stack_resident_across_straight_line_body():
    observed = []

    def probe():
        observed.append(_hidden(sys._getframe(1)))
        return 1

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            y = probe() + 10
            z = y * 2
        if case(2):
            y = probe() + 20
            z = y * 2
        if case():
            y = probe() + 30
            z = y * 2
    return y, z
''',
        extra={"probe": probe},
    )
    assert fn.__pyswitch_backend__ == "portable-statement-template-v18"
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert [fn(1), fn(2), fn(9)] == [(11, 22), (21, 42), (31, 62)]
    assert observed == [(), (), ()]


def test_trace_events_never_expose_stackified_payload_local():
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x + 10
        if case(2):
            return x + 20
        if case():
            return x + 30
'''
    )
    seen = []

    def tracer(frame, event, arg):
        if frame.f_code is fn.__code__:
            seen.append((event, _hidden(frame)))
        return tracer

    sys.settrace(tracer)
    try:
        assert fn(2) == 22
    finally:
        sys.settrace(None)
    assert seen
    assert all(hidden == () for _event, hidden in seen)


def test_outer_exception_handler_drops_stack_carrier_and_has_clean_locals():
    fn = _compile(
        '''def f(x, boom):
    try:
        with switch(x):
            if case(1):
                y = boom() + 10
            if case(2):
                y = boom() + 20
            if case():
                y = boom() + 30
    except TypeError:
        y = 99
        hidden = tuple(k for k in locals() if k.startswith("__pyswitch_"))
    else:
        hidden = tuple(k for k in locals() if k.startswith("__pyswitch_"))
    return y, hidden
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert fn(1, lambda: 1) == (11, ())

    def boom():
        raise TypeError("user expression")

    assert fn(1, boom) == (99, ())


def test_intrinsically_unhashable_subject_uses_default_with_stack_payload():
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return len(x) + 10
        if case(2):
            return len(x) + 20
        if case():
            return len(x) + 30
'''
    )
    assert fn.__pyswitch_backend__ == "portable-expression-template-v18"
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert fn([]) == 30


def test_user_hash_typeerror_still_propagates_with_stack_payload():
    class BadHash:
        def __hash__(self):
            raise TypeError("user hash failure")

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x is x and 10
        if case(2):
            return x is x and 20
        if case():
            return x is x and 30
'''
    )
    # BoolOp introduces a control-flow branch, so this particular template is a
    # conservative non-stackified fallback.  Use a straight-line expression too.
    fn2 = _compile(
        '''def g(x):
    with switch(x):
        if case(1):
            return id(x) + 10
        if case(2):
            return id(x) + 20
        if case():
            return id(x) + 30
''',
        extra={"id": id},
    )
    assert fn2.__pyswitch_stack_payload_plan_count__ == 1
    with pytest.raises(TypeError, match="user hash failure"):
        fn2(BadHash())
    # Keep the branchy template sanity check here as a fail-closed control.
    assert fn.__pyswitch_stack_payload_plan_count__ == 0


def test_typed_key_expression_template_stackifies_without_semantic_collapse():
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x + 10
        if case(True):
            return int(x) + 20
        if case():
            return int(x) + 30
''',
        case_key_mode="typed",
    )
    # Shapes differ for the default/True routes, so use a second typed function
    # whose expression template is deliberately uniform.
    fn2 = _compile(
        '''def g(x):
    with switch(x):
        if case(1):
            return int(x) + 10
        if case(True):
            return int(x) + 20
        if case():
            return int(x) + 30
''',
        case_key_mode="typed",
    )
    assert fn2.__pyswitch_stack_payload_plan_count__ == 1
    assert fn2(1) == 11
    assert fn2(True) == 21
    assert fn2(9) == 39
    assert fn(1) == 11


def test_multiple_switches_remove_each_independent_payload_local():
    fn = _compile(
        '''def f(a, b):
    with switch(a):
        if case(1):
            x = a + 10
        if case():
            x = a + 20
    with switch(b):
        if case(2):
            y = b + 30
        if case():
            y = b + 40
    return x + y
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 2
    assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    assert fn(1, 2) == 43
    assert fn(9, 8) == 77


def test_complex_control_flow_no_longer_enters_statement_template_shortcut():
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            y = x + 10
            try:
                z = 1 // x
            except ZeroDivisionError:
                z = 0
        if case(2):
            y = x + 20
            try:
                z = 1 // x
            except ZeroDivisionError:
                z = 0
        if case():
            y = x + 30
            try:
                z = 1 // x
            except ZeroDivisionError:
                z = 0
    return y, z
'''
    )
    assert fn.__pyswitch_backend__ == "portable-balanced-v18"
    assert fn.__pyswitch_stack_payload_plan_count__ == 0
    assert fn(1) == (11, 1)
    assert fn(2) == (22, 0)
    assert fn(0) == (30, 0)


def test_multi_payload_expression_template_keeps_all_literals_off_fast_locals():
    observed = []

    def probe(value, multiplier, offset):
        observed.append(_hidden(sys._getframe(1)))
        return value * multiplier + offset

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return probe(x, 2, 10)
        if case(2):
            return probe(x, 3, 20)
        if case():
            return probe(x, 4, 30)
''',
        extra={"probe": probe},
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    assert [fn(1), fn(2), fn(9)] == [12, 26, 66]
    assert observed == [(), (), ()]


def test_three_payload_assignment_template_stack_cleanup_is_exact():
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            y = x * 2 + 10 - 1
        if case(2):
            y = x * 3 + 20 - 2
        if case():
            y = x * 4 + 30 - 3
    hidden = tuple(k for k in locals() if k.startswith("__pyswitch_"))
    return y, hidden
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert fn(1) == (11, ())
    assert fn(2) == (24, ())
    assert fn(3) == (39, ())


def test_multi_payload_return_consumes_shallow_last_uses_without_cleanup():
    import dis

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x * 2 + 10 - 1
        if case(2):
            return x * 3 + 20 - 2
        if case():
            return x * 4 + 30 - 3
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    instructions = list(dis.get_instructions(fn, show_caches=False))
    return_index = next(i for i, inst in enumerate(instructions) if inst.opname == "RETURN_VALUE")
    hot_path = instructions[:return_index]
    # Each selected literal is a final use at physical depth two.  The carrier
    # is therefore moved directly into the binary operand slot with SWAP 2 and
    # consumed; no COPY or terminal POP cleanup remains on the hot path.
    assert sum(inst.opname == "SWAP" and inst.arg == 2 for inst in hot_path) == 3
    assert not any(inst.opname in {"COPY", "POP_TOP"} for inst in hot_path)
    assert [fn(1), fn(2), fn(9)] == [11, 24, 63]


def test_left_literal_final_use_consumes_depth_one_carrier_with_zero_opcodes():
    import dis

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return 10 + x
        if case(2):
            return 20 + x
        if case():
            return 30 + x
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    instructions = list(dis.get_instructions(fn, show_caches=False))
    return_index = next(i for i, inst in enumerate(instructions) if inst.opname == "RETURN_VALUE")
    hot_path = instructions[:return_index]
    assert not any(inst.opname in {"COPY", "SWAP", "POP_TOP"} for inst in hot_path)
    assert [fn(1), fn(2), fn(9)] == [11, 22, 39]


def test_fused_tuple_loads_can_consume_payload_without_fast_local_or_cleanup():
    import dis

    left = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return (10, x)
        if case(2):
            return (20, x)
        if case():
            return (30, x)
'''
    )
    right = _compile(
        '''def g(x):
    with switch(x):
        if case(1):
            return (x, 10)
        if case(2):
            return (x, 20)
        if case():
            return (x, 30)
'''
    )
    for fn in (left, right):
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
        assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
        instructions = list(dis.get_instructions(fn, show_caches=False))
        return_index = next(i for i, inst in enumerate(instructions) if inst.opname == "RETURN_VALUE")
        hot_path = instructions[:return_index]
        assert not any(inst.opname in {"COPY", "POP_TOP"} for inst in hot_path)
    assert [left(1), left(2), left(9)] == [(10, 1), (20, 2), (30, 9)]
    assert [right(1), right(2), right(9)] == [(1, 10), (2, 20), (9, 30)]


def test_single_argument_call_consumes_final_depth_three_return_carrier():
    import dis

    def probe(value):
        return value * 2

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return probe(10)
        if case(2):
            return probe(20)
        if case():
            return probe(30)
''',
        extra={"probe": probe},
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    instructions = list(dis.get_instructions(fn, show_caches=False))
    return_index = next(i for i, inst in enumerate(instructions) if inst.opname == "RETURN_VALUE")
    hot_path = instructions[:return_index]
    # LOAD_GLOBAL (+NULL) leaves the sole carrier at depth three.  Because
    # consuming it also removes the return SWAP+POP pair, two rotations are a
    # strict one-instruction win over COPY plus terminal cleanup.
    assert [(inst.opname, inst.arg) for inst in hot_path[-3:-1]] == [
        ("SWAP", 2),
        ("SWAP", 3),
    ]
    assert not any(inst.opname in {"COPY", "POP_TOP"} for inst in hot_path)
    assert [fn(1), fn(2), fn(9)] == [20, 40, 60]


def test_deep_multi_argument_call_keeps_copy_when_rotation_is_not_profitable():
    import dis

    def probe(a, b):
        return a + b

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return probe(x, 10)
        if case(2):
            return probe(x, 20)
        if case():
            return probe(x, 30)
''',
        extra={"probe": probe},
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    instructions = list(dis.get_instructions(fn, show_caches=False))
    return_index = next(i for i, inst in enumerate(instructions) if inst.opname == "RETURN_VALUE")
    hot_path = instructions[:return_index]
    # The carrier is deeper than three once NULL/callable/argument state is on
    # the stack.  A rotation would cost at least as much as COPY+cleanup, so the
    # profitability guard deliberately leaves this shape unchanged.
    assert any(inst.opname == "COPY" for inst in hot_path)
    assert [fn(1), fn(2), fn(9)] == [11, 22, 39]


def test_identical_template_discards_unused_identity_carrier_at_join():
    import dis

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x + 10
        if case(2):
            return x + 10
        if case():
            return x + 10
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    instructions = list(dis.get_instructions(fn, show_caches=False))
    return_index = next(i for i, inst in enumerate(instructions) if inst.opname == "RETURN_VALUE")
    hot_path = instructions[:return_index]
    # The synthetic identity payload exists only to preserve observable hash /
    # equality lookup semantics.  It is never read by the shared template, so
    # discard it once at the join instead of carrying it through user code and
    # paying SWAP+POP at return.
    assert sum(inst.opname == "POP_TOP" for inst in hot_path) == 1
    assert not any(inst.opname == "COPY" for inst in hot_path)
    assert not any(inst.opname == "SWAP" for inst in hot_path)
    assert [fn(1), fn(2), fn(9)] == [11, 12, 19]


def test_sys_monitoring_return_and_line_events_are_safe_with_stack_carriers():
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        pytest.skip("sys.monitoring is unavailable")

    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x * 2 + 10 - 1
        if case(2):
            return x * 3 + 20 - 2
        if case():
            return x * 4 + 30 - 3
'''
    )
    events = monitoring.events
    tool_id = monitoring.OPTIMIZER_ID
    seen_returns = []
    seen_lines = []
    monitoring.use_tool_id(tool_id, "pyswitch-stack-payload-test")
    try:
        def on_return(code, instruction_offset, value):
            if code is fn.__code__:
                seen_returns.append(value)

        def on_line(code, lineno):
            if code is fn.__code__:
                seen_lines.append(lineno)

        monitoring.register_callback(tool_id, events.PY_RETURN, on_return)
        monitoring.register_callback(tool_id, events.LINE, on_line)
        monitoring.set_events(tool_id, events.PY_RETURN | events.LINE)
        assert [fn(1), fn(2), fn(9)] == [11, 24, 63]
    finally:
        monitoring.set_events(tool_id, events.NO_EVENTS)
        monitoring.register_callback(tool_id, events.PY_RETURN, None)
        monitoring.register_callback(tool_id, events.LINE, None)
        monitoring.free_tool_id(tool_id)
    assert seen_returns == [11, 24, 63]
    assert seen_lines
    assert all(line > 0 for line in seen_lines)


def test_stack_payload_transform_fails_closed_off_cpython_313(monkeypatch):
    import importlib

    switch_module = importlib.import_module("python_extensions.switch")
    monkeypatch.setattr(switch_module.platform, "python_implementation", lambda: "PyPy")
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return x + 10
        if case(2):
            return x + 20
        if case():
            return x + 30
'''
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 0
    assert fn.__pyswitch_stack_payload_fallbacks__ == ("unsupported-stack-runtime",)
    assert [fn(1), fn(2), fn(9)] == [11, 22, 39]


def test_portable_backends_do_not_emit_synthetic_line_zero_locations():
    sources = [
        '''def direct(x):
    with switch(x):
        if case(1):
            return 10
        if case(2):
            return 20
        if case():
            return 30
''',
        '''def expression(x):
    with switch(x):
        if case(1):
            return x + 10
        if case(2):
            return x + 20
        if case():
            return x + 30
''',
        '''def statement(x):
    with switch(x):
        if case(1):
            y = x + 10
            z = y * 2
        if case(2):
            y = x + 20
            z = y * 2
        if case():
            y = x + 30
            z = y * 2
    return z
''',
        '''def balanced(x, flag=True):
    with switch(x):
        if case(1, when=flag):
            return 10
        elif case(1):
            return 11
        if case(2):
            return 20
        if case():
            return 30
''',
    ]
    for source in sources:
        fn = _compile(source)
        assert fn.__pyswitch_line_location_fallback__ is None
        assert fn.__pyswitch_synthetic_line_locations_removed__ > 0
        assert all(
            lineno is None or lineno > 0
            for _start, _end, lineno in fn.__code__.co_lines()
        )


def test_unhashable_exception_fallback_trace_never_reports_line_zero():
    fn = _compile(
        '''def f(x):
    with switch(x):
        if case(1):
            return len(x) + 10
        if case(2):
            return len(x) + 20
        if case():
            return len(x) + 30
'''
    )
    seen_lines = []

    def tracer(frame, event, arg):
        if frame.f_code is fn.__code__ and event == "line":
            seen_lines.append(frame.f_lineno)
        return tracer

    sys.settrace(tracer)
    try:
        assert fn([]) == 30
    finally:
        sys.settrace(None)
    assert seen_lines
    assert all(line > 0 for line in seen_lines)


def test_live_backends_never_emit_line_zero_and_preserve_source_offset():
    # Compile the original function well below physical line 1 so the live AST
    # relocation path is exercised, not merely the zero-offset happy path.
    function_source = '''def live_line(x):
    with switch(x):
        if case(1):
            return 10
        if case(2):
            return 20
        if case():
            return 30
'''
    filename = "<pyswitch-live-line-offset>"
    prefixed = "\n" * 47 + function_source

    for mode in ("fast", "thread_local", "isolated", "per_call"):
        ns = {"switch": switch, "case": case}
        exec(compile(prefixed, filename, "exec"), ns)
        original = ns["live_line"]
        assert original.__code__.co_firstlineno == 48
        fn = enable_switch(
            mode=mode,
            source=function_source,
            expose_debug=True,
        )(original)

        template = getattr(fn, "__pyswitch_unsafe_template__", fn)
        assert all(
            lineno is None or lineno > 0
            for _start, _end, lineno in template.__code__.co_lines()
        )

        seen_lines = []

        def tracer(frame, event, arg):
            if frame.f_code.co_filename == filename and event == "line":
                seen_lines.append(frame.f_lineno)
            return tracer

        sys.settrace(tracer)
        try:
            assert fn(2) == 20
        finally:
            sys.settrace(None)

        assert seen_lines
        assert all(line >= 48 for line in seen_lines)
        # The selected case body lives at physical source line 53.
        assert 53 in seen_lines


def test_portable_source_offset_never_reports_pre_function_synthetic_lines():
    function_source = '''def portable_line(x):
    with switch(x):
        if case(1):
            return x + 10
        if case(2):
            return x + 20
        if case():
            return x + 30
'''
    filename = "<pyswitch-portable-line-offset>"
    ns = {"switch": switch, "case": case}
    exec(compile("\n" * 63 + function_source, filename, "exec"), ns)
    original = ns["portable_line"]
    assert original.__code__.co_firstlineno == 64
    fn = enable_switch(mode="portable", source=function_source)(original)

    assert fn.__pyswitch_synthetic_line_locations_removed__ > 0
    assert all(
        lineno is None or lineno >= 64
        for _start, _end, lineno in fn.__code__.co_lines()
    )
    seen = []

    def tracer(frame, event, arg):
        if frame.f_code is fn.__code__ and event == "line":
            seen.append(frame.f_lineno)
        return tracer

    sys.settrace(tracer)
    try:
        assert fn(2) == 22
    finally:
        sys.settrace(None)
    assert seen
    assert min(seen) >= 64
    # The O(1) expression template is one shared code region, so route-specific
    # case-body line events are not promised; the key invariant is that no
    # synthetic pre-function line leaks into tracing.


@pytest.mark.parametrize("mode", ["fast", "thread_local", "isolated", "per_call"])
@pytest.mark.parametrize("case_key_mode", ["python", "typed"])
def test_live_unhashable_miss_and_user_hash_typeerror_match_portable_semantics(
    mode, case_key_mode
):
    source = '''def f(x):
    with switch(x):
        if case(1):
            return 10
        if case(2):
            return 20
        if case():
            return 30
'''
    ns = {"switch": switch, "case": case}
    exec(compile(source, "<pyswitch-live-unhashable>", "exec"), ns)
    fn = enable_switch(
        mode=mode,
        source=source,
        case_key_mode=case_key_mode,
    )(ns["f"])

    assert fn([]) == 30
    assert fn({}) == 30

    class RaisingHash:
        def __hash__(self):
            raise TypeError("user hash exploded")

    with pytest.raises(TypeError, match="user hash exploded"):
        fn(RaisingHash())


@pytest.mark.parametrize("mode", ["fast", "thread_local", "isolated", "per_call"])
def test_live_subject_and_gate_temporaries_are_unbound_before_user_case_body(mode):
    observed = []

    def probe():
        observed.append(_hidden(sys._getframe(1)))
        return 5

    source = '''def f(x):
    with switch(x):
        if case(1):
            return probe() + 10
        if case(2):
            return probe() + 20
        if case():
            return probe() + 30
'''
    ns = {"switch": switch, "case": case, "probe": probe}
    exec(compile(source, "<pyswitch-live-frame-hygiene>", "exec"), ns)
    fn = enable_switch(mode=mode, source=source)(ns["f"])
    ns["f"] = fn

    assert [fn(1), fn(2), fn(9)] == [15, 25, 35]
    assert observed == [(), (), ()]
