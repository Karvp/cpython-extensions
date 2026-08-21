"""Stress/differential harness for pyswitch 0.18.2 stack payload lowering."""
from __future__ import annotations

import argparse
import concurrent.futures
import gc
import random
import sys
from typing import Any, Callable

from python_extensions import case, enable_switch, switch


def compile_switch(source: str, name: str, *, typed: bool = False, extra=None):
    ns = {"switch": switch, "case": case}
    if extra:
        ns.update(extra)
    exec(compile(source, f"<stack-payload-{name}>", "exec"), ns)
    fn = enable_switch(
        mode="portable",
        case_key_mode="typed" if typed else "python",
        source=source,
    )(ns[name])
    return fn


def hidden_payloads(frame) -> tuple[str, ...]:
    return tuple(sorted(k for k in frame.f_locals if k.startswith("__pyswitch_")))


def combine(*values: Any):
    return sum(values)


def make_expression_switch(shape: int, *, typed: bool = False):
    payloads = [index * 17 + 5 for index in range(32)]
    default = 997
    expressions = {
        0: lambda k: f"value + {k}",
        1: lambda k: f"{k} + value",
        2: lambda k: f"combine(value, {k})",
        3: lambda k: f"combine({k}, value)",
        4: lambda k: f"combine(value, value + 1, {k})",
        5: lambda k: f"(value, {k}, value + 1)",
        6: lambda k: f"[value, {k}]",
        7: lambda k: f"{{'value': value, 'payload': {k}}}",
        8: lambda k: f"value * 2 + {k}",
    }
    expr = expressions[shape]
    lines = ["def candidate(value):", "    with switch(value):"]
    for key, payload in enumerate(payloads):
        lines.extend((f"        if case({key}):", f"            return {expr(payload)}"))
    lines.extend(("        if case():", f"            return {expr(default)}"))
    fn = compile_switch(
        "\n".join(lines) + "\n", "candidate", typed=typed, extra={"combine": combine}
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1, (
        shape, fn.__pyswitch_stack_payload_fallbacks__, fn.__code__.co_varnames
    )
    assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)

    def reference(value):
        k = payloads[value] if type(value) is int and 0 <= value < len(payloads) else default
        if shape == 0:
            return value + k
        if shape == 1:
            return k + value
        if shape in {2, 3}:
            return value + k
        if shape == 4:
            return value + (value + 1) + k
        if shape == 5:
            return (value, k, value + 1)
        if shape == 6:
            return [value, k]
        if shape == 7:
            return {"value": value, "payload": k}
        if shape == 8:
            return value * 2 + k
        raise AssertionError(shape)

    return fn, reference



def make_multi_payload_switch(kind: str):
    lines = ["def multi_payload(value):", "    with switch(value):"]
    for key in range(24):
        multiplier = key + 2
        offset = key * 11 + 7
        bias = key % 5 + 1
        if kind == "arithmetic":
            expression = f"value * {multiplier} + {offset} - {bias}"
        elif kind == "call":
            expression = f"combine(value, {multiplier}, {offset}) - {bias}"
        else:
            raise AssertionError(kind)
        lines.extend((f"        if case({key}):", f"            return {expression}"))
    if kind == "arithmetic":
        default_expression = "value * 31 + 997 - 9"
    else:
        default_expression = "combine(value, 31, 997) - 9"
    lines.extend(("        if case():", f"            return {default_expression}"))
    fn = compile_switch(
        "\n".join(lines) + "\n", "multi_payload", extra={"combine": lambda a, b, c: a * b + c}
    )
    assert fn.__pyswitch_stack_payload_plan_count__ == 1, fn.__pyswitch_stack_payload_fallbacks__
    assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)

    def reference(value: int):
        if 0 <= value < 24:
            multiplier = value + 2
            offset = value * 11 + 7
            bias = value % 5 + 1
        else:
            multiplier, offset, bias = 31, 997, 9
        return value * multiplier + offset - bias

    return fn, reference

def make_statement_switch():
    source = '''def statement(value, observer):
    with switch(value):
        if case(0):
            a = observer(value) + 11
            b = a * 2
            return b + 3
        if case(1):
            a = observer(value) + 23
            b = a * 2
            return b + 3
        if case(2):
            a = observer(value) + 37
            b = a * 2
            return b + 3
        if case():
            a = observer(value) + 101
            b = a * 2
            return b + 3
'''
    fn = compile_switch(source, "statement")
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    return fn


def make_outer_exception_switch():
    source = '''def outer(value, callback):
    try:
        with switch(value):
            if case(0):
                result = callback(value) + 11
            if case(1):
                result = callback(value) + 23
            if case():
                result = callback(value) + 101
    except LookupError:
        result = -500
    return result, tuple(k for k in locals() if k.startswith("__pyswitch_"))
'''
    fn = compile_switch(source, "outer")
    assert fn.__pyswitch_stack_payload_plan_count__ == 1
    return fn


def make_multi_switch():
    source = '''def multi(a, b, c):
    with switch(a):
        if case(0): x = a + 10
        if case(1): x = a + 20
        if case(): x = a + 30
    with switch(b):
        if case(0): y = b + 40
        if case(1): y = b + 50
        if case(): y = b + 60
    with switch(c):
        if case(0): z = c + 70
        if case(1): z = c + 80
        if case(): z = c + 90
    return x + y + z
'''
    fn = compile_switch(source, "multi")
    assert fn.__pyswitch_stack_payload_plan_count__ == 3
    return fn


def ref_multi(a: int, b: int, c: int) -> int:
    x = a + (10 if a == 0 else 20 if a == 1 else 30)
    y = b + (40 if b == 0 else 50 if b == 1 else 60)
    z = c + (70 if c == 0 else 80 if c == 1 else 90)
    return x + y + z


class DisabledHash:
    __hash__ = None


class RaisingHash:
    def __hash__(self):
        raise TypeError("hash exploded")


def generated_templates(seed: int, functions: int, probes: int) -> int:
    rng = random.Random(seed)
    calls = 0
    for index in range(functions):
        size = rng.randint(2, 48)
        keys = rng.sample(range(-1000, 1000), size)
        payloads = [rng.randint(-10000, 10000) for _ in keys]
        default = rng.randint(-10000, 10000)
        # Alternate payload placement to vary COPY depth around CALL argument stacks.
        if index & 1:
            expr = lambda k: f"combine(value, value + 1, {k})"
            ref = lambda value, k: value + value + 1 + k
        else:
            expr = lambda k: f"combine({k}, value)"
            ref = lambda value, k: k + value
        lines = ["def generated(value):", "    with switch(value):"]
        for key, payload in zip(keys, payloads):
            lines.extend((f"        if case({key}):", f"            return {expr(payload)}"))
        lines.extend(("        if case():", f"            return {expr(default)}"))
        fn = compile_switch("\n".join(lines) + "\n", "generated", extra={"combine": combine})
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
        mapping = dict(zip(keys, payloads))
        for _ in range(probes):
            value = rng.randrange(-1100, 1100)
            payload = mapping.get(value, default)
            assert fn(value) == ref(value, payload)
            calls += 1
    return calls


def trace_and_profile_stress(fn: Callable[[int], Any], rounds: int) -> int:
    trace_events = 0
    profile_events = 0

    def tracer(frame, event, arg):
        nonlocal trace_events
        if frame.f_code is fn.__code__:
            assert hidden_payloads(frame) == ()
            trace_events += 1
        return tracer

    def profiler(frame, event, arg):
        nonlocal profile_events
        if frame.f_code is fn.__code__:
            assert hidden_payloads(frame) == ()
            profile_events += 1

    sys.settrace(tracer)
    try:
        for i in range(rounds):
            fn(i % 40)
    finally:
        sys.settrace(None)

    sys.setprofile(profiler)
    try:
        for i in range(rounds):
            fn(i % 40)
    finally:
        sys.setprofile(None)

    assert trace_events > 0
    assert profile_events > 0
    return rounds * 2


def run(profile: str):
    if profile == "full":
        loops = {
            "expr_each": 320_000,
            "statement": 550_000,
            "outer": 500_000,
            "multi": 650_000,
            "thread": 650_000,
            "unhashable": 180_000,
            "trace": 6_000,
            "multi_payload_each": 420_000,
        }
        generated = (180, 150)
        churn = 400
    else:
        loops = {
            "expr_each": 12_000,
            "statement": 25_000,
            "outer": 20_000,
            "multi": 30_000,
            "thread": 30_000,
            "unhashable": 8_000,
            "trace": 500,
            "multi_payload_each": 20_000,
        }
        generated = (30, 40)
        churn = 50

    rng = random.Random(0x518182)
    calls = 0
    expression_pairs = [make_expression_switch(shape) for shape in range(9)]

    for fn, reference in expression_pairs:
        for _ in range(loops["expr_each"]):
            value = rng.randrange(-8, 40)
            assert fn(value) == reference(value)
        calls += loops["expr_each"]

    multi_payload_pairs = [
        make_multi_payload_switch("arithmetic"),
        make_multi_payload_switch("call"),
    ]
    for fn, reference in multi_payload_pairs:
        for _ in range(loops["multi_payload_each"]):
            value = rng.randrange(-6, 30)
            assert fn(value) == reference(value)
        calls += loops["multi_payload_each"]

    # Typed mode gets the same stack path while preserving bool/int distinction.
    typed, typed_ref = make_expression_switch(2, typed=True)
    for _ in range(loops["expr_each"] // 2):
        value = rng.randrange(-8, 40)
        assert typed(value) == typed_ref(value)
    calls += loops["expr_each"] // 2

    observed = 0

    def observer(value):
        nonlocal observed
        assert hidden_payloads(sys._getframe(1)) == ()
        observed += 1
        return value * 3

    statement = make_statement_switch()
    payloads = {0: 11, 1: 23, 2: 37}
    for _ in range(loops["statement"]):
        value = rng.randrange(-4, 8)
        payload = payloads.get(value, 101)
        assert statement(value, observer) == ((value * 3 + payload) * 2 + 3)
    assert observed == loops["statement"]
    calls += loops["statement"]

    outer = make_outer_exception_switch()
    for i in range(loops["outer"]):
        value = rng.randrange(-4, 6)
        if i % 23 == 0:
            def callback(_value):
                raise LookupError("expected")
            expected = -500
        else:
            callback = lambda v: v * 2
            expected = value * 2 + (11 if value == 0 else 23 if value == 1 else 101)
        result, hidden = outer(value, callback)
        assert result == expected
        assert hidden == ()
    calls += loops["outer"]

    multi = make_multi_switch()
    for _ in range(loops["multi"]):
        values = (rng.randrange(-3, 5), rng.randrange(-3, 5), rng.randrange(-3, 5))
        assert multi(*values) == ref_multi(*values)
    calls += loops["multi"]

    thread_values = [
        (rng.randrange(-3, 5), rng.randrange(-3, 5), rng.randrange(-3, 5))
        for _ in range(loops["thread"])
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        actual = list(executor.map(lambda args: multi(*args), thread_values, chunksize=256))
    assert actual == [ref_multi(*values) for values in thread_values]
    calls += loops["thread"]

    # Intrinsically unhashable subjects take the default route through the
    # exception cleanup path while the selected payload becomes stack-resident.
    unhash_source = '''def unhash(value):
    with switch(value):
        if case(1): return len(value) + 10
        if case(2): return len(value) + 20
        if case(): return len(value) + 30
'''
    unhash = compile_switch(unhash_source, "unhash")
    assert unhash.__pyswitch_stack_payload_plan_count__ == 1
    for i in range(loops["unhashable"]):
        value = [None] * (i % 9)
        assert unhash(value) == len(value) + 30
    calls += loops["unhashable"]

    bad_hash_source = '''def bad_hash(value):
    with switch(value):
        if case(1): return id(value) + 10
        if case(2): return id(value) + 20
        if case(): return id(value) + 30
'''
    bad_hash = compile_switch(bad_hash_source, "bad_hash", extra={"id": id})
    assert bad_hash.__pyswitch_stack_payload_plan_count__ == 1
    for _ in range(1000 if profile == "full" else 100):
        try:
            bad_hash(RaisingHash())
        except TypeError as exc:
            assert str(exc) == "hash exploded"
        else:
            raise AssertionError("custom __hash__ TypeError was swallowed")
        calls += 1

    calls += trace_and_profile_stress(expression_pairs[2][0], loops["trace"])
    calls += generated_templates(0xA182, *generated)

    # Repeated compilation/code-object rebuilding must not retain transformed
    # functions or share payload tables across instances.
    for index in range(churn):
        source = f'''def ephemeral(value):\n    with switch(value):\n        if case(0): return value + {index + 1}\n        if case(): return value + {index + 2}\n'''
        fn = compile_switch(source, "ephemeral")
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
        assert fn(0) == index + 1
        assert fn(4) == index + 6
        calls += 2
        del fn
    gc.collect()

    print(f"pyswitch stack payload v18.2 {profile}: {calls:,} calls passed")
    print("expression shapes:", len(expression_pairs), "stackified")
    print("multi-payload shapes:", len(multi_payload_pairs), "stackified")
    print("statement backend:", statement.__pyswitch_backend__)
    print("multi stack plans:", multi.__pyswitch_stack_payload_plan_count__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    run(args.profile)


if __name__ == "__main__":
    main()
