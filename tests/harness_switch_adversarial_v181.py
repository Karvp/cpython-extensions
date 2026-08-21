"""Broad differential/stress harness for pyswitch production hardening."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import gc
import random
import sys
from dataclasses import dataclass
from typing import Any

from python_extensions import case, enable_switch, fallthrough, switch


def compile_switch(source: str, name: str, *, typed: bool = False, extra=None):
    ns = {"switch": switch, "case": case, "fallthrough": fallthrough}
    if extra:
        ns.update(extra)
    exec(compile(source, f"<stress-{name}>", "exec"), ns)
    return enable_switch(
        mode="portable",
        case_key_mode="typed" if typed else "python",
        source=source,
    )(ns[name])


def make_direct(size: int):
    lines = ["def direct(value):", "    with switch(value):"]
    for key in range(size):
        lines.extend((f"        if case({key}):", f"            return {key * 3 + 7}"))
    lines.extend(("        if case():", "            return -1"))
    return compile_switch("\n".join(lines) + "\n", "direct")


def make_balanced(size: int):
    # Guards deliberately force the general route compiler. Each route still
    # has deterministic semantics so it can be checked cheaply.
    lines = ["def balanced(value, mask):", "    with switch(value):"]
    for key in range(size):
        lines.extend(
            (
                f"        if case({key}, when=(mask & 1)):",
                f"            return {key * 5 + 11}",
                f"        elif case({key}):",
                f"            return {key * 7 + 13}",
            )
        )
    lines.extend(("        if case():", "            return -1"))
    return compile_switch("\n".join(lines) + "\n", "balanced")


def ref_balanced(value: int, mask: int, size: int) -> int:
    if 0 <= value < size:
        return value * 5 + 11 if mask & 1 else value * 7 + 13
    return -1


class CollisionKey:
    __slots__ = ("value",)

    def __init__(self, value: int):
        self.value = value

    def __hash__(self):
        return 0x51A7

    def __eq__(self, other):
        if isinstance(other, CollisionKey):
            return self.value == other.value
        return NotImplemented


def make_collision_switch(size: int):
    keys = [CollisionKey(index) for index in range(size)]
    extra = {f"K{index}": key for index, key in enumerate(keys)}
    lines = ["def collision(value):", "    with switch(value):"]
    for index in range(size):
        lines.extend((f"        if case(K{index}):", f"            return {index}"))
    lines.extend(("        if case():", "            return -1"))
    return compile_switch("\n".join(lines) + "\n", "collision", extra=extra), keys


def make_typed_mixed():
    values: list[Any] = []
    for i in range(40):
        values.extend((i, float(i), str(i), (i,), bytes([i])))
    extra = {f"K{i}": value for i, value in enumerate(values)}
    lines = ["def typed_mixed(value):", "    with switch(value):"]
    for i in range(len(values)):
        lines.extend((f"        if case(K{i}):", f"            return {i}"))
    lines.extend(("        if case():", "            return -1"))
    return (
        compile_switch("\n".join(lines) + "\n", "typed_mixed", typed=True, extra=extra),
        values,
    )


def make_fallthrough_switch():
    source = '''def falling(value, enabled):
    out = []
    with switch(value):
        if case(0, when=enabled):
            out.append(0)
            fallthrough()
        elif case(0):
            out.append(10)
        if case(1):
            out.append(1)
            fallthrough()
        if case(2):
            out.append(2)
            fallthrough()
        if case(3):
            out.append(3)
        if case():
            out.append(9)
    return tuple(out)
'''
    return compile_switch(source, "falling")


def ref_falling(value: int, enabled: bool):
    if value == 0:
        return (0, 10) if enabled else (10,)
    if value == 1:
        return (1, 2, 3)
    if value == 2:
        return (2, 3)
    if value == 3:
        return (3,)
    return (9,)


def make_nested():
    source = '''def nested(a, b, c):
    out = 0
    with switch(a):
        if case(0):
            out += 100
            with switch(b):
                if case(0):
                    out += 10
                    with switch(c):
                        if case(0):
                            out += 1
                        if case():
                            out += 2
                if case():
                    out += 20
        if case():
            out += 200
    return out
'''
    return compile_switch(source, "nested")


def ref_nested(a: int, b: int, c: int):
    if a != 0:
        return 200
    if b != 0:
        return 120
    return 111 if c == 0 else 112


def make_recursive(mode: str, cache_depth: int = 16):
    outer = '''def factory():
    def recurse(n, selector):
        with switch(selector):
            if case(0):
                amount = 1
            if case():
                amount = 2
        if n == 0:
            return amount
        return amount + recurse(n - 1, selector)
    return recurse
'''
    body = '''def recurse(n, selector):
    with switch(selector):
        if case(0):
            amount = 1
        if case():
            amount = 2
    if n == 0:
        return amount
    return amount + recurse(n - 1, selector)
'''
    ns = {"switch": switch, "case": case}
    exec(outer, ns)
    original = ns["factory"]()
    return enable_switch(mode=mode, source=body, max_cached_depth=cache_depth)(original)


def generated_random_direct(seed: int, functions: int, probes: int):
    rng = random.Random(seed)
    calls = 0
    for index in range(functions):
        count = rng.randint(1, 96)
        keys = rng.sample(range(-5000, 5000), count)
        payloads = [rng.randrange(-(1 << 40), 1 << 40) for _ in keys]
        default = rng.randrange(-(1 << 40), 1 << 40)
        lines = ["def candidate(value):", "    with switch(value):"]
        for key, payload in zip(keys, payloads):
            lines.extend((f"        if case({key}):", f"            return {payload}"))
        lines.extend(("        if case():", f"            return {default}"))
        candidate = compile_switch("\n".join(lines) + "\n", "candidate")
        reference = dict(zip(keys, payloads))
        for _ in range(probes):
            value = rng.randrange(-5500, 5500)
            assert candidate(value) == reference.get(value, default)
            calls += 1
    return calls


async def async_stress(rounds: int):
    source = '''async def async_switch(value):
    await asyncio.sleep(0)
    with switch(value):
        if case(0):
            await asyncio.sleep(0)
            return 10
        if case(1):
            await asyncio.sleep(0)
            return 20
        if case():
            await asyncio.sleep(0)
            return 30
'''
    fn = compile_switch(source, "async_switch", extra={"asyncio": asyncio})
    values = [i % 4 for i in range(rounds)]
    actual = await asyncio.gather(*(fn(value) for value in values))
    expected = [10 if value == 0 else 20 if value == 1 else 30 for value in values]
    assert actual == expected
    return len(values)


def run(profile: str):
    if profile == "full":
        loops = {
            "direct": 1_400_000,
            "balanced": 900_000,
            "collision": 120_000,
            "typed": 700_000,
            "fall": 500_000,
            "nested": 500_000,
            "thread": 500_000,
            "async": 30_000,
            "recursive": 8_000,
        }
        generated = (180, 160)
        sizes = (1024, 256, 48)
    else:
        loops = {
            "direct": 80_000,
            "balanced": 50_000,
            "collision": 8_000,
            "typed": 40_000,
            "fall": 30_000,
            "nested": 30_000,
            "thread": 40_000,
            "async": 2_000,
            "recursive": 500,
        }
        generated = (25, 50)
        sizes = (256, 96, 24)

    direct_size, balanced_size, collision_size = sizes
    direct = make_direct(direct_size)
    balanced = make_balanced(balanced_size)
    collision, collision_keys = make_collision_switch(collision_size)
    typed, typed_values = make_typed_mixed()
    falling = make_fallthrough_switch()
    nested = make_nested()

    calls = 0
    rng = random.Random(0xC0FFEE)

    for _ in range(loops["direct"]):
        value = rng.randrange(-64, direct_size + 64)
        expected = value * 3 + 7 if 0 <= value < direct_size else -1
        assert direct(value) == expected
    calls += loops["direct"]

    for _ in range(loops["balanced"]):
        value = rng.randrange(-32, balanced_size + 32)
        mask = rng.randrange(4)
        assert balanced(value, mask) == ref_balanced(value, mask, balanced_size)
    calls += loops["balanced"]

    for _ in range(loops["collision"]):
        index = rng.randrange(-4, collision_size + 4)
        subject = CollisionKey(index)
        expected = index if 0 <= index < collision_size else -1
        assert collision(subject) == expected
    calls += loops["collision"]

    miss_values = [False, None, frozenset({1}), 3 + 4j]
    for i in range(loops["typed"]):
        if i % 17 == 0:
            value = miss_values[(i // 17) % len(miss_values)]
            expected = -1
        else:
            index = rng.randrange(len(typed_values))
            value = typed_values[index]
            expected = index
        assert typed(value) == expected
    calls += loops["typed"]

    for _ in range(loops["fall"]):
        value = rng.randrange(-2, 6)
        enabled = bool(rng.getrandbits(1))
        assert falling(value, enabled) == ref_falling(value, enabled)
    calls += loops["fall"]

    for _ in range(loops["nested"]):
        a, b, c = (rng.randrange(3), rng.randrange(3), rng.randrange(3))
        assert nested(a, b, c) == ref_nested(a, b, c)
    calls += loops["nested"]

    # Shared portable function under high contention.
    thread_values = [rng.randrange(-64, direct_size + 64) for _ in range(loops["thread"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        actual = list(executor.map(direct, thread_values, chunksize=256))
    expected = [v * 3 + 7 if 0 <= v < direct_size else -1 for v in thread_values]
    assert actual == expected
    calls += loops["thread"]

    calls += asyncio.run(async_stress(loops["async"]))

    # Deep recursive nested closures, including depths above the isolated clone
    # cache. Repeat enough times to exercise cache reuse and ephemeral clones.
    portable_recursive = make_recursive("portable")
    isolated_recursive = make_recursive("isolated", cache_depth=4)
    per_call_recursive = make_recursive("per_call")
    for i in range(loops["recursive"]):
        depth = 4 + i % 28
        selector = i & 1
        amount = 1 if selector == 0 else 2
        expected = amount * (depth + 1)
        assert portable_recursive(depth, selector) == expected
        assert isolated_recursive(depth, selector) == expected
        assert per_call_recursive(depth, selector) == expected
        calls += 3

    calls += generated_random_direct(0xA17E, *generated)

    # Decoration/collection churn: tables and wrappers must remain independent.
    for i in range(300 if profile == "full" else 40):
        source = f'''def ephemeral(value):\n    with switch(value):\n        if case({i}):\n            return {i + 1}\n        if case():\n            return -1\n'''
        fn = compile_switch(source, "ephemeral")
        assert fn(i) == i + 1
        assert fn(-1) == -1
        calls += 2
        del fn
    gc.collect()

    print(f"pyswitch adversarial v18.1 {profile}: {calls:,} calls passed")
    print(
        "backends:",
        direct.__pyswitch_backend__,
        balanced.__pyswitch_backend__,
        collision.__pyswitch_backend__,
        typed.__pyswitch_backend__,
        falling.__pyswitch_backend__,
        nested.__pyswitch_backend__,
    )
    print("sizes:", direct_size, balanced_size, collision_size)
    print("isolated cache:", isolated_recursive.__pyswitch_cache_info__())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    run(args.profile)


if __name__ == "__main__":
    main()
