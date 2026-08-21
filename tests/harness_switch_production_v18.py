"""Differential/stress certification for the production portable switch backend."""
from __future__ import annotations

import argparse
import concurrent.futures
import random

from python_extensions import case, enable_switch, fallthrough, switch


@enable_switch(mode="portable")
def literal64(value):
    with switch(value):
        if case(0, 1, 2, 3, 4, 5, 6, 7):
            return 101
        if case(8, 9, 10, 11, 12, 13, 14, 15):
            return 202
        if case(16, 17, 18, 19, 20, 21, 22, 23):
            return 303
        if case(24, 25, 26, 27, 28, 29, 30, 31):
            return 404
        if case(32, 33, 34, 35, 36, 37, 38, 39):
            return 505
        if case(40, 41, 42, 43, 44, 45, 46, 47):
            return 606
        if case(48, 49, 50, 51, 52, 53, 54, 55):
            return 707
        if case(56, 57, 58, 59, 60, 61, 62, 63):
            return 808
        if case():
            return -1


@enable_switch(mode="portable")
def template64(value):
    with switch(value):
        if case(0, 1, 2, 3, 4, 5, 6, 7):
            return value + 101
        if case(8, 9, 10, 11, 12, 13, 14, 15):
            return value + 202
        if case(16, 17, 18, 19, 20, 21, 22, 23):
            return value + 303
        if case(24, 25, 26, 27, 28, 29, 30, 31):
            return value + 404
        if case(32, 33, 34, 35, 36, 37, 38, 39):
            return value + 505
        if case(40, 41, 42, 43, 44, 45, 46, 47):
            return value + 606
        if case(48, 49, 50, 51, 52, 53, 54, 55):
            return value + 707
        if case(56, 57, 58, 59, 60, 61, 62, 63):
            return value + 808
        if case():
            return -1


@enable_switch(mode="portable")
def statement64(value):
    with switch(value):
        if case(0, 1, 2, 3, 4, 5, 6, 7):
            y = value + 101
            y *= 2
            return y
        if case(8, 9, 10, 11, 12, 13, 14, 15):
            y = value + 202
            y *= 2
            return y
        if case(16, 17, 18, 19, 20, 21, 22, 23):
            y = value + 303
            y *= 2
            return y
        if case(24, 25, 26, 27, 28, 29, 30, 31):
            y = value + 404
            y *= 2
            return y
        if case(32, 33, 34, 35, 36, 37, 38, 39):
            y = value + 505
            y *= 2
            return y
        if case(40, 41, 42, 43, 44, 45, 46, 47):
            y = value + 606
            y *= 2
            return y
        if case(48, 49, 50, 51, 52, 53, 54, 55):
            y = value + 707
            y *= 2
            return y
        if case(56, 57, 58, 59, 60, 61, 62, 63):
            y = value + 808
            y *= 2
            return y
        if case():
            return -1


@enable_switch(mode="portable", case_key_mode="typed")
def typed(value):
    with switch(value):
        if case(1):
            return "int"
        if case(1.0):
            return "float"
        if case(True):
            return "bool"
        if case("1"):
            return "str"
        if case((1,)):
            return "tuple"
        if case():
            return "miss"


@enable_switch(mode="portable")
def guarded(value, flags):
    with switch(value):
        if case(1, when=flags & 1):
            return 11
        if case(1, when=flags & 2):
            return 12
        if case(1):
            return 13
        if case(2, when=flags & 4):
            return 24
        if case(2):
            return 25
        if case():
            return -1


@enable_switch(mode="portable")
def falling(value):
    out = []
    with switch(value):
        if case(0):
            out.append(0)
            fallthrough()
        if case(1):
            out.append(1)
            fallthrough()
        if case(2):
            out.append(2)
        if case():
            out.append(9)
    return tuple(out)


def bucket(value: int) -> int:
    if not 0 <= value < 64:
        return -1
    return (101, 202, 303, 404, 505, 606, 707, 808)[value // 8]


def ref_literal(value: int) -> int:
    return bucket(value)


def ref_template(value: int) -> int:
    b = bucket(value)
    return -1 if b < 0 else value + b


def ref_statement(value: int) -> int:
    b = bucket(value)
    return -1 if b < 0 else (value + b) * 2


def ref_guarded(value: int, flags: int) -> int:
    if value == 1:
        if flags & 1:
            return 11
        if flags & 2:
            return 12
        return 13
    if value == 2:
        return 24 if flags & 4 else 25
    return -1


def ref_falling(value: int) -> tuple[int, ...]:
    if value == 0:
        return (0, 1, 2)
    if value == 1:
        return (1, 2)
    if value == 2:
        return (2,)
    return (9,)


def generated_differential(seed: int, functions: int, probes: int) -> int:
    rng = random.Random(seed)
    calls = 0
    for index in range(functions):
        count = rng.randint(2, 48)
        keys = rng.sample(range(-200, 400), count)
        payloads = [rng.randint(-1_000_000, 1_000_000) for _ in keys]
        lines = ["def candidate(value):", "    with switch(value):"]
        for key, payload in zip(keys, payloads):
            lines.extend((f"        if case({key!r}):", f"            return {payload!r}"))
        default = rng.randint(-1_000_000, 1_000_000)
        lines.extend(("        if case():", f"            return {default!r}"))
        source = "\n".join(lines) + "\n"
        namespace = {"switch": switch, "case": case}
        exec(compile(source, f"<pyswitch-fuzz-{index}>", "exec"), namespace)
        candidate = enable_switch(mode="portable", source=source)(namespace["candidate"])
        reference = dict(zip(keys, payloads))
        for _ in range(probes):
            value = rng.randint(-250, 450)
            expected = reference.get(value, default)
            actual = candidate(value)
            if actual != expected:
                raise AssertionError((index, value, expected, actual))
            calls += 1
    return calls


def run(profile: str) -> int:
    if profile == "full":
        counts = dict(literal=900_000, template=700_000, statement=700_000,
                      typed=350_000, guarded=350_000, falling=150_000,
                      threaded=500_000)
        generated = (200, 150)
    else:
        counts = dict(literal=50_000, template=40_000, statement=40_000,
                      typed=20_000, guarded=20_000, falling=10_000,
                      threaded=40_000)
        generated = (30, 30)

    calls = 0
    rng = random.Random(0x51817)
    for i in range(counts["literal"]):
        value = rng.randrange(-16, 80)
        assert literal64(value) == ref_literal(value)
    calls += counts["literal"]

    for i in range(counts["template"]):
        value = rng.randrange(-16, 80)
        assert template64(value) == ref_template(value)
    calls += counts["template"]

    for i in range(counts["statement"]):
        value = rng.randrange(-16, 80)
        assert statement64(value) == ref_statement(value)
    calls += counts["statement"]

    typed_values = (1, 1.0, True, "1", (1,), False, 2, "x", [], {})
    typed_expected = ("int", "float", "bool", "str", "tuple", "miss", "miss", "miss", "miss", "miss")
    for i in range(counts["typed"]):
        position = i % len(typed_values)
        assert typed(typed_values[position]) == typed_expected[position]
    calls += counts["typed"]

    for i in range(counts["guarded"]):
        value = rng.randrange(0, 4)
        flags = rng.randrange(0, 8)
        assert guarded(value, flags) == ref_guarded(value, flags)
    calls += counts["guarded"]

    for i in range(counts["falling"]):
        value = rng.randrange(0, 5)
        assert falling(value) == ref_falling(value)
    calls += counts["falling"]

    thread_values = [rng.randrange(-16, 80) for _ in range(counts["threaded"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        actual = list(executor.map(statement64, thread_values, chunksize=256))
    expected = [ref_statement(value) for value in thread_values]
    assert actual == expected
    calls += counts["threaded"]

    calls += generated_differential(0xA11CE, *generated)
    return calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    calls = run(args.profile)
    print(f"pyswitch production v18 {args.profile}: {calls:,} differential/stress calls passed")
    print("backends:", literal64.__pyswitch_backend__, template64.__pyswitch_backend__, statement64.__pyswitch_backend__, guarded.__pyswitch_backend__)


if __name__ == "__main__":
    main()
