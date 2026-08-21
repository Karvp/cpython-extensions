from __future__ import annotations

from python_extensions import case, enable_switch, switch


def _compile_switch(size: int, shape: str = "direct"):
    lines = ["def route(value):", "    with switch(value):"]
    for index in range(size):
        if shape == "direct":
            action = f"return {index + 1}"
        elif shape == "expression":
            action = f"return value * 3 + {index}"
        elif shape == "statement":
            action = f"tmp = value + {index}; return tmp ^ {index + 1}"
        else:
            raise ValueError(shape)
        lines.append(f"        if case({index}): {action}")
    lines.append("        if case(): return -1")
    source = "\n".join(lines) + "\n"
    namespace = {"switch": switch, "case": case}
    exec(compile(source, "<switch-scaling-regression>", "exec"), namespace)
    return enable_switch(mode="portable", source=source)(namespace["route"])


def _compile_if_chain(size: int):
    lines = ["def route(value):"]
    for index in range(size):
        keyword = "if" if index == 0 else "elif"
        lines.append(f"    {keyword} value == {index}: return {index + 1}")
    lines.append("    return -1")
    namespace = {}
    exec(compile("\n".join(lines) + "\n", "<if-scaling-regression>", "exec"), namespace)
    return namespace["route"]


def test_direct_value_switch_keeps_constant_bytecode_shape_at_1024_cases():
    small = _compile_switch(8, "direct")
    large = _compile_switch(1024, "direct")
    baseline = _compile_if_chain(1024)

    assert small.__pyswitch_backend__ == "portable-direct-value-v18"
    assert large.__pyswitch_backend__ == "portable-direct-value-v18"
    assert large.__pyswitch_case_count__ == 1024
    # The route table grows with N, but the executable dispatch bytecode does
    # not grow into an N-way comparison chain.
    assert len(large.__code__.co_code) == len(small.__code__.co_code)
    assert len(large.__code__.co_code) <= 128
    assert len(baseline.__code__.co_code) >= 20 * len(large.__code__.co_code)

    for value in range(1024):
        assert large(value) == value + 1
    assert large(-1) == -1
    assert large(9999) == -1


def test_template_switches_remain_bounded_as_route_count_grows():
    for shape, backend, limit in (
        ("expression", "portable-expression-template-v18", 160),
        ("statement", "portable-statement-template-v18", 180),
    ):
        small = _compile_switch(8, shape)
        large = _compile_switch(128, shape)
        assert small.__pyswitch_backend__ == backend
        assert large.__pyswitch_backend__ == backend
        assert len(large.__code__.co_code) == len(small.__code__.co_code)
        assert len(large.__code__.co_code) <= limit
        for value in (0, 1, 31, 63, 127):
            if shape == "expression":
                expected = value * 3 + value
            else:
                expected = (value + value) ^ (value + 1)
            assert large(value) == expected
        assert large(-1) == -1
