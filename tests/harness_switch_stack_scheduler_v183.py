"""Differential stress harness for depth-aware pyswitch stack-carrier scheduling."""
from __future__ import annotations

import argparse
import concurrent.futures
import gc
import random
import sys

from python_extensions import case, enable_switch, switch


def _compile_pair(expr: str, *, name: str):
    case_values = [11, 17, 23, 29, 31, 37, 41, 43]
    default_value = 47
    switch_lines = [f"def {name}(x, seq):", "    with switch(x):"]
    ref_lines = [f"def ref_{name}(x, seq):"]
    for key, value in enumerate(case_values):
        rendered = expr.replace("K", str(value))
        switch_lines += [f"        if case({key}):", f"            return {rendered}"]
        ref_lines += [f"    {'if' if key == 0 else 'elif'} x == {key}:", f"        return {rendered}"]
    rendered_default = expr.replace("K", str(default_value))
    switch_lines += ["        if case():", f"            return {rendered_default}"]
    ref_lines += ["    else:", f"        return {rendered_default}"]
    source = "\n".join(switch_lines) + "\n"
    ref_source = "\n".join(ref_lines) + "\n"
    ns = {"switch": switch, "case": case, "probe1": probe1, "probe2": probe2}
    exec(compile(source + ref_source, f"<scheduler-{name}>", "exec"), ns)
    candidate = enable_switch(mode="portable", source=source)(ns[name])
    return candidate, ns[f"ref_{name}"]


def probe1(value):
    return value * 3 + 1


def probe2(a, b):
    return a * 5 + b * 7


EXPRESSIONS = [
    "K + x",
    "x + K",
    "K - x",
    "x - K",
    "K * (x + 1)",
    "(x + 1) * K",
    "(K, x)",
    "(x, K)",
    "[K, x]",
    "[x, K]",
    "seq[K % len(seq)]",
    "K in seq",
    "x < K",
    "K < x",
    "probe1(K)",
    "probe2(x, K)",
    "f'{x}:{K}'",
    "{'k': K, 'x': x}",
]

MULTI_EXPRESSIONS = [
    "x * A + B - C",
    "A + x * B - C",
    "(A, x, B, C)",
    "[A, x, B, C]",
    "probe2(A + x, B) + C",
]


def _compile_multi(expr: str, *, name: str):
    payloads = [
        (2, 101, 7), (3, 103, 11), (5, 107, 13), (7, 109, 17),
        (11, 113, 19), (13, 127, 23), (17, 131, 29), (19, 137, 31),
    ]
    default = (23, 139, 37)
    sw = [f"def {name}(x):", "    with switch(x):"]
    ref = [f"def ref_{name}(x):"]
    for key, (a, b, c) in enumerate(payloads):
        rendered = expr.replace("A", str(a)).replace("B", str(b)).replace("C", str(c))
        sw += [f"        if case({key}):", f"            return {rendered}"]
        ref += [f"    {'if' if key == 0 else 'elif'} x == {key}:", f"        return {rendered}"]
    rendered = expr.replace("A", str(default[0])).replace("B", str(default[1])).replace("C", str(default[2]))
    sw += ["        if case():", f"            return {rendered}"]
    ref += ["    else:", f"        return {rendered}"]
    source = "\n".join(sw) + "\n"
    ref_source = "\n".join(ref) + "\n"
    ns = {"switch": switch, "case": case, "probe2": probe2}
    exec(compile(source + ref_source, f"<scheduler-multi-{name}>", "exec"), ns)
    candidate = enable_switch(mode="portable", source=source)(ns[name])
    return candidate, ns[f"ref_{name}"]


def _outcome(fn, *args):
    try:
        return ("return", fn(*args))
    except BaseException as exc:  # differential harness deliberately includes user exceptions
        return ("raise", type(exc), str(exc))


def run(profile: str) -> None:
    if profile == "full":
        loops = 90_000
        multi_loops = 80_000
        threaded = 300_000
        traced = 40_000
        churn = 600
    else:
        loops = 5_000
        multi_loops = 4_000
        threaded = 20_000
        traced = 2_000
        churn = 60

    rng = random.Random(0x1835CA1E)
    seq = tuple(range(97))
    calls = 0
    compiled = []

    for index, expression in enumerate(EXPRESSIONS):
        fn, ref = _compile_pair(expression, name=f"shape_{index}")
        assert fn.__pyswitch_stack_payload_plan_count__ == 1, (
            expression, fn.__pyswitch_stack_payload_fallbacks__
        )
        assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
        compiled.append((fn, ref))
        for i in range(loops):
            x = (i * 17 + index * 13) % 15 - 3
            actual = _outcome(fn, x, seq)
            expected = _outcome(ref, x, seq)
            assert actual == expected, (expression, x, actual, expected)
        calls += loops

    for index, expression in enumerate(MULTI_EXPRESSIONS):
        fn, ref = _compile_multi(expression, name=f"multi_{index}")
        assert fn.__pyswitch_stack_payload_plan_count__ == 1, (
            expression, fn.__pyswitch_stack_payload_fallbacks__
        )
        compiled.append((fn, ref))
        for i in range(multi_loops):
            x = (i * 19 + index * 7) % 17 - 4
            actual = _outcome(fn, x)
            expected = _outcome(ref, x)
            assert actual == expected, (expression, x, actual, expected)
        calls += multi_loops

    # Explicit exceptional expressions: payload extraction must not leave a
    # corrupt stack when the user operation raises, and the next call must be clean.
    div_source = '''def divshape(x, y):
    with switch(x):
        if case(0): return 10 // y
        if case(1): return 20 // y
        if case(): return 30 // y
'''
    div_ref_source = '''def ref_divshape(x, y):
    if x == 0: return 10 // y
    if x == 1: return 20 // y
    return 30 // y
'''
    ns = {"switch": switch, "case": case}
    exec(compile(div_source + div_ref_source, "<scheduler-div>", "exec"), ns)
    divshape = enable_switch(mode="portable", source=div_source)(ns["divshape"])
    assert divshape.__pyswitch_stack_payload_plan_count__ == 1
    for i in range(loops // 3):
        x = i % 4
        y = 0 if i % 11 == 0 else (i % 9) + 1
        assert _outcome(divshape, x, y) == _outcome(ns["ref_divshape"], x, y)
        if y == 0:
            assert divshape(x, 2) == ns["ref_divshape"](x, 2)
        calls += 1

    # A shared optimized function under contention.
    threaded_fn, threaded_ref = compiled[1]  # x + K, depth-two carrier consumption
    values = [((rng.randrange(-5, 15)), seq) for _ in range(threaded)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        actual = list(executor.map(lambda a: threaded_fn(*a), values, chunksize=256))
    expected = [threaded_ref(*a) for a in values]
    assert actual == expected
    calls += threaded

    # Tracing on the hottest consumed-carrier shape: no synthetic fast locals,
    # no stack corruption, and only valid positive source lines.
    traced_fn, traced_ref = compiled[0]
    seen_lines = []
    def tracer(frame, event, arg):
        if frame.f_code is traced_fn.__code__ and event == "line":
            seen_lines.append(frame.f_lineno)
        return tracer
    sys.settrace(tracer)
    try:
        for i in range(traced):
            x = i % 13 - 2
            assert traced_fn(x, seq) == traced_ref(x, seq)
    finally:
        sys.settrace(None)
    assert seen_lines and min(seen_lines) > 0
    calls += traced

    # Repeated code generation and collection catches stale-table/carrier state.
    for index in range(churn):
        literal = 1000 + index
        source = f'''def ephemeral(x):\n    with switch(x):\n        if case(0): return {literal} + x\n        if case(1): return {literal + 1} + x\n        if case(): return {literal + 2} + x\n'''
        ns = {"switch": switch, "case": case}
        exec(compile(source, "<scheduler-churn>", "exec"), ns)
        fn = enable_switch(mode="portable", source=source)(ns["ephemeral"])
        assert fn(0) == literal
        assert fn(1) == literal + 2
        assert fn(9) == literal + 11
        calls += 3
        del fn
    gc.collect()

    print(f"pyswitch stack scheduler v18.3 {profile}: {calls:,} calls passed")
    print("single shapes:", len(EXPRESSIONS), "multi shapes:", len(MULTI_EXPRESSIONS))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    run(args.profile)


if __name__ == "__main__":
    main()
