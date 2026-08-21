from __future__ import annotations

import dis

from python_extensions import case, enable_switch, fallthrough, switch


def test_general_fallthrough_hoists_source_identical_default_continuation():
    @enable_switch(mode="portable", compact_routes=True)
    def route(value, out):
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

    assert route(1, []) == ("one", "tail", "done")
    assert route(9, []) == ("tail", "done")
    report = route.__python_extensions_report__.as_dict()
    assert report["shared_continuation_plans"] == 1
    assert report["shared_continuation_statements"] >= 1
    # This shape is intentionally control-heavy, so the shared continuation
    # belongs to the caller-frame general backend rather than a template helper.
    assert route.__pyswitch_binary_route_plan_count__ == 1
    assert route.__pyswitch_template_plan_count__ == 0
    assert route.__pyswitch_statement_template_plan_count__ == 0


def test_lookalike_route_lines_are_not_hoisted_across_distinct_source_locations():
    @enable_switch(mode="portable", compact_routes=True)
    def route(value, out):
        with switch(value):
            if case(1):
                try:
                    out.append("same")
                finally:
                    out.append("done")
            if case():
                try:
                    out.append("same")
                finally:
                    out.append("done")
        return tuple(out)

    assert route(1, []) == ("same", "done")
    assert route(2, []) == ("same", "done")
    # The text is the same but debugger/coverage locations differ, so the
    # location-aware continuation optimizer must leave both copies alone.
    assert route.__pyswitch_shared_continuation_plan_count__ == 0



def test_shared_continuation_compaction_is_opt_in_for_speed_first_default():
    @enable_switch(mode="portable")
    def route(value, out):
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

    assert route(1, []) == ("one", "tail", "done")
    assert route.__pyswitch_shared_continuation_plan_count__ == 0
    report = route.__python_extensions_report__.as_dict()
    assert report["compact_routes"] is False


def test_compact_routes_validation():
    import pytest
    with pytest.raises(TypeError, match="compact_routes must be bool or 'auto'"):
        enable_switch(compact_routes=1)
    with pytest.raises(ValueError, match="string mode must be 'auto'"):
        enable_switch(compact_routes="density")


def test_compact_routes_auto_skips_tiny_shared_suffix():
    @enable_switch(mode="portable", compact_routes="auto")
    def route(value, out):
        with switch(value):
            if case(1):
                out.append("one")
                fallthrough()
            if case():
                out.append("tail")
        return tuple(out)

    assert route(1, []) == ("one", "tail")
    assert route(9, []) == ("tail",)
    assert route.__pyswitch_shared_continuation_plan_count__ == 0


def test_compact_routes_auto_accepts_large_shared_suffix():
    @enable_switch(mode="portable", compact_routes="auto")
    def route(value, out):
        with switch(value):
            if case(1):
                out.append("one")
                fallthrough()
            if case():
                try:
                    out.append("a")
                    out.append("b")
                    out.append("c")
                finally:
                    out.append("done")
        return tuple(out)

    assert route(1, []) == ("one", "a", "b", "c", "done")
    assert route(9, []) == ("a", "b", "c", "done")
    assert route.__pyswitch_shared_continuation_plan_count__ == 1
