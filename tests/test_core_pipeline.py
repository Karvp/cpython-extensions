from __future__ import annotations

import dis

from python_extensions import (
    TransformationReport,
    case,
    enable_goto,
    enable_switch,
    explain_extensions,
    inline_function,
    optimize_extensions,
    switch,
    verify_code,
)


@inline_function(register_only=True)
def _inc(value):
    return value + 1


@optimize_extensions(
    switch={"case_key_mode": "typed", "mode": "portable"},
    inline={"policy": "speed"},
    goto=True,
)
def combined(value, rounds):
    total = 0
    label .loop
    total = _inc(total)
    with switch(value):
        if case(1):
            bonus = 10
        if case(True):
            bonus = 20
        if case():
            bonus = 30
    if total < rounds:
        goto .loop
    return total + bonus


@enable_goto
def simple_goto(value):
    if value:
        goto .done
    value = 4
    label .done
    return value


@enable_switch(mode="portable")
def simple_switch(value):
    with switch(value):
        if case(1):
            return "one"
        if case():
            return "other"


def test_combined_pipeline_semantics_and_order():
    assert combined(1, 5) == 15
    assert combined(True, 5) == 25
    assert combined("x", 5) == 35
    assert combined.__python_extensions_pipeline__ == ("switch", "inline", "goto")


def test_combined_pipeline_removed_inline_call():
    names = [item.opname for item in dis.get_instructions(combined, adaptive=False)]
    # The helper call itself must be gone. Other CALL opcodes are not expected in
    # this fixture after switch lowering, but inspect LOAD_GLOBAL as the exact proof.
    assert not any(
        item.opname == "LOAD_GLOBAL" and item.argval == "_inc"
        for item in dis.get_instructions(combined, adaptive=False)
    )


def test_reports_accumulate_across_pipeline():
    reports = combined.__python_extensions_reports__
    assert [report.feature for report in reports[-3:]] == ["switch", "inline", "goto"]
    assert all(isinstance(report, TransformationReport) for report in reports)
    text = explain_extensions(combined)
    assert "switch" in text
    assert "inline" in text
    assert "goto" in text
    assert "switch -> inline -> goto" in text


def test_standalone_reports_and_verifier():
    assert simple_goto(1) == 1
    assert simple_goto(0) == 4
    assert simple_goto.__python_extensions_report__.feature == "goto"
    assert simple_switch(1) == "one"
    assert simple_switch(9) == "other"
    assert simple_switch.__python_extensions_report__.feature == "switch"
    result = verify_code(combined.__code__)
    assert result.valid
    assert result.reachable_blocks > 0


def test_bare_optimize_extensions_is_validation_only():
    @optimize_extensions
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    assert add.__python_extensions_pipeline__ == ()


def test_optimize_extensions_forwards_switch_compact_routes_option():
    from python_extensions import case, fallthrough, optimize_extensions, switch

    @optimize_extensions(switch={"compact_routes": True})
    def flow(value, out):
        with switch(value):
            if case(1):
                out.append("one")
                fallthrough()
            if case():
                try:
                    out.append("tail")
                finally:
                    out.append("done")
        return tuple(out)

    assert flow(1, []) == ("one", "tail", "done")
    report = flow.__python_extensions_report__.as_dict()
    assert report["compact_routes"] is True
    assert report["shared_continuation_plans"] == 1
