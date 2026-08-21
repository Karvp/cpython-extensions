from __future__ import annotations

from python_extensions import case, enable_switch, switch


def _source(routes: int = 16, sites: int = 2) -> str:
    lines = [
        "def hotloop(sequence, rounds):",
        "    acc = 1",
        "    state = 2",
        "    for _ in range(rounds):",
        "        for value in sequence:",
    ]
    for site in range(sites):
        lines.append("            with switch(value):")
        for route in range(routes):
            lines.extend(
                [
                    f"                if case({route}):",
                    "                    if state < 0:",
                    "                        acc ^= 1",
                    f"                    acc += {route + site + 1}",
                ]
            )
        lines.extend(
            [
                "                if case():",
                "                    if state < 0:",
                "                        acc ^= 1",
                f"                    acc -= {site + 1}",
            ]
        )
    lines.append("    return acc ^ state")
    return "\n".join(lines) + "\n"


def _compile(source: str, mode: str):
    namespace = {"switch": switch, "case": case}
    exec(compile(source, f"<live-hotloop-contract-{mode}>", "exec"), namespace)
    return enable_switch(mode=mode, source=source)(namespace["hotloop"])


def test_live_hotloop_benchmark_reaches_intended_real_backends_and_matches():
    source = _source()
    portable = _compile(source, "portable")
    fast = _compile(source, "fast")

    assert portable.__pyswitch_backend__ == "portable-balanced-v18"
    assert portable.__pyswitch_balanced_plan_count__ == 2
    assert portable.__pyswitch_switch_count__ == 2
    assert portable.__pyswitch_direct_plan_count__ == 0
    assert portable.__pyswitch_template_plan_count__ == 0
    assert portable.__pyswitch_statement_template_plan_count__ == 0
    assert portable.__pyswitch_binary_route_plan_count__ == 0

    assert fast.__pyswitch_backend__ == "cpython313-live-inline-v18"
    assert len(fast.__pyswitch_gate_offsets__) == 2
    assert len(fast.__pyswitch_gate_units__) == 2

    sequence = (0, 15, 3, -1, 16, 7, 1, 14)
    assert portable(sequence, 50) == fast(sequence, 50)


def test_live_gate_locator_handles_extended_arg_constants_across_many_sites():
    # Four 64-route sites push the later synthetic marker constants beyond the
    # one-byte LOAD_CONST operand range.  CPython then prefixes those loads with
    # EXTENDED_ARG; the live gate must begin at that prefix, not after it.
    source = _source(routes=64, sites=4)
    portable = _compile(source, "portable")
    fast = _compile(source, "fast")

    assert portable.__pyswitch_balanced_plan_count__ == 4
    assert fast.__pyswitch_backend__ == "cpython313-live-inline-v18"
    assert len(fast.__pyswitch_gate_offsets__) == 4
    assert all(width in (1, 2, 4) for width in fast.__pyswitch_gate_units__)

    sequence = (0, 63, 17, 32, -1, 64, 5, 61)
    assert portable(sequence, 25) == fast(sequence, 25)


def test_live_gate_locator_handles_extended_arg_local_indexes():
    lines = ["def high_locals(value):"]
    for index in range(300):
        lines.append(f"    x{index} = {index}")
    lines.append("    with switch(value):")
    for route in range(4):
        lines.extend(
            [
                f"        if case({route}):",
                "            if x299 < 0:",
                "                x0 ^= 1",
                f"            return x0 + {route + 1}",
            ]
        )
    lines.extend(
        [
            "        if case():",
            "            if x299 < 0:",
            "                x0 ^= 1",
            "            return -1",
        ]
    )
    source = "\n".join(lines) + "\n"
    namespace = {"switch": switch, "case": case}
    exec(compile(source, "<live-high-locals-contract>", "exec"), namespace)

    portable = enable_switch(mode="portable", source=source)(namespace["high_locals"])
    fast = enable_switch(mode="fast", source=source)(namespace["high_locals"])

    assert len(fast.__code__.co_varnames) > 255
    assert portable.__pyswitch_backend__ == "portable-balanced-v18"
    assert fast.__pyswitch_backend__ == "cpython313-live-inline-v18"
    assert [portable(value) for value in (-1, 0, 1, 2, 3, 4)] == [
        fast(value) for value in (-1, 0, 1, 2, 3, 4)
    ]
