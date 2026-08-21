from __future__ import annotations

import dis

from python_extensions import case, enable_switch, switch


def test_control_heavy_two_route_plan_uses_boolean_dispatch():
    @enable_switch(mode="portable")
    def route(value, sink):
        with switch(value):
            if case(1, 2, 3, 4):
                try:
                    sink.append("hit")
                finally:
                    sink.append("done")
            if case():
                try:
                    sink.append("miss")
                finally:
                    sink.append("done")
        return len(sink)

    sink = []
    assert route(2, sink) == 2
    assert sink == ["hit", "done"]
    sink.clear()
    assert route(9, sink) == 2
    assert sink == ["miss", "done"]
    report = route.__python_extensions_report__.as_dict()
    assert report["portable_binary_route_plans"] == 1
    assert report["portable_balanced_plans"] == 0
    # The route discriminator is a direct truth branch, not COMPARE_OP < 1.
    ops = [item.opname for item in dis.get_instructions(route, adaptive=False)]
    assert "COMPARE_OP" not in ops


def test_binary_route_preserves_unhashable_default_semantics():
    @enable_switch(mode="portable")
    def route(value):
        with switch(value):
            if case(1):
                try:
                    return "hit"
                finally:
                    pass
            if case():
                try:
                    return "default"
                finally:
                    pass

    assert route([]) == "default"


def test_binary_route_typed_numeric_identity():
    @enable_switch(mode="portable", case_key_mode="typed")
    def route(value):
        with switch(value):
            if case(1):
                try:
                    return "int"
                finally:
                    pass
            if case():
                try:
                    return "other"
                finally:
                    pass

    assert route(1) == "int"
    assert route(True) == "other"
    assert route(1.0) == "other"


def test_line_table_cleanup_fails_closed_on_exception_table_roundtrip():
    @enable_switch(mode="portable")
    def route(value):
        with switch(value):
            if case(1):
                try:
                    return "hit"
                finally:
                    pass
            if case():
                try:
                    return "default"
                finally:
                    pass

    assert route(1) == "hit"
    assert route(2) == "default"
    from python_extensions import verify_code
    assert verify_code(route.__code__).valid
    # bytecode 0.17 can mis-encode this 3.13 exception-table shape when used
    # solely to normalize locations. The optimizer must retain CPython's valid
    # original code instead of accepting that optional rewrite.
    fallback = route.__pyswitch_line_location_fallback__
    assert fallback is None or fallback.startswith(("verify:", "decode:", "encode:", "bytecode-"))
