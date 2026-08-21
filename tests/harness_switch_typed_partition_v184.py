"""Stress harness for allocation-free exact-type portable switch routing."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import random

from python_extensions import case, enable_switch, switch


def _compile(source: str, *, extra=None):
    ns = {"switch": switch, "case": case}
    if extra:
        ns.update(extra)
    exec(compile(source, "<pyswitch-typed-partition-v184-harness>", "exec"), ns)
    first = next(line for line in source.splitlines() if line.startswith(("def ", "async def ")))
    name = first.split("(", 1)[0].split()[-1]
    return enable_switch(mode="portable", case_key_mode="typed", source=source)(ns[name])


def _direct_int(n: int):
    lines = ["def direct_int(x):", "    with switch(x):"]
    for i in range(n):
        lines += [f"        if case({i}):", f"            return {1000 + i}"]
    lines += ["        if case():", "            return -1"]
    return _compile("\n".join(lines) + "\n")


def _direct_str(n: int):
    lines = ["def direct_str(x):", "    with switch(x):"]
    for i in range(n):
        lines += [f"        if case('k{i}'):", f"            return {2000 + i}"]
    lines += ["        if case():", "            return -1"]
    return _compile("\n".join(lines) + "\n")


def _expr():
    source = '''def expr(x):
    with switch(x):
        if case(0): return x + 101
        if case(1): return x + 103
        if case(2): return x + 107
        if case(3): return x + 109
        if case(): return x + -1
'''
    return _compile(source)


def _stmt():
    source = '''def stmt(x):
    with switch(x):
        if case(0):
            y = x + 101
            y *= 3
            return y
        if case(1):
            y = x + 103
            y *= 3
            return y
        if case(2):
            y = x + 107
            y *= 3
            return y
        if case(3):
            y = x + 109
            y *= 3
            return y
        if case():
            y = x + -1
            y *= 3
            return y
'''
    return _compile(source)


def _balanced():
    source = '''def balanced(x, flag):
    with switch(x):
        if case(0, when=flag):
            return 100
        if case(0):
            return 101
        if case(1):
            return 102
        if case(2):
            return 103
        if case():
            return -1
'''
    return _compile(source)


def _mixed():
    source = '''def mixed(x):
    with switch(x):
        if case(1): return "int"
        if case(1.0): return "float"
        if case(True): return "bool"
        if case("1"): return "str"
        if case(): return "miss"
'''
    return _compile(source)


def _reference_int(x, n):
    if type(x) is int:
        return 1000 + x if 0 <= x < n else -1
    try:
        hash(x)
    except TypeError:
        # Intrinsically unhashable builtins used by the harness are misses.
        if type(x).__hash__ is None:
            return -1
        raise
    return -1


def run(profile: str) -> None:
    if profile == "full":
        direct_loops = 1_200_000
        str_loops = 500_000
        template_loops = 700_000
        balanced_loops = 500_000
        collision_loops = 350_000
        threaded = 500_000
        async_calls = 60_000
        mixed_loops = 250_000
    else:
        direct_loops = 60_000
        str_loops = 30_000
        template_loops = 40_000
        balanced_loops = 30_000
        collision_loops = 20_000
        threaded = 30_000
        async_calls = 3_000
        mixed_loops = 20_000

    calls = 0
    direct = _direct_int(64)
    assert direct.__pyswitch_typed_partition_plan_count__ == 1
    values = [0, 1, 31, 63, 64, 100, True, False, 1.0, 63.0, "1", (), [], {}]
    for i in range(direct_loops):
        x = values[(i * 17 + 5) % len(values)]
        assert direct(x) == _reference_int(x, 64)
    calls += direct_loops

    strings = _direct_str(32)
    assert strings.__pyswitch_typed_partition_plan_count__ == 1
    str_values = ["k0", "k7", "k31", "miss", 1, True, b"k0", ()]
    for i in range(str_loops):
        x = str_values[(i * 11 + 3) % len(str_values)]
        expected = 2000 + int(x[1:]) if type(x) is str and x.startswith("k") and x[1:].isdigit() and int(x[1:]) < 32 else -1
        assert strings(x) == expected
    calls += str_loops

    expr = _expr()
    stmt = _stmt()
    assert expr.__pyswitch_typed_partition_plan_count__ == 1
    assert stmt.__pyswitch_typed_partition_plan_count__ == 1
    for i in range(template_loops):
        x = (i * 19) % 11 - 3
        ex = x + [101, 103, 107, 109][x] if type(x) is int and 0 <= x < 4 else x - 1
        assert expr(x) == ex
        st = ex * 3
        assert stmt(x) == st
        calls += 2

    balanced = _balanced()
    assert balanced.__pyswitch_typed_partition_plan_count__ == 1
    for i in range(balanced_loops):
        x = [0, 1, 2, 3, True, 1.0][i % 6]
        flag = bool(i & 1)
        if type(x) is int and x == 0:
            expected = 100 if flag else 101
        elif type(x) is int and x == 1:
            expected = 102
        elif type(x) is int and x == 2:
            expected = 103
        else:
            expected = -1
        assert balanced(x, flag) == expected
    calls += balanced_loops

    # Same-type forced collisions: route lookup must preserve ordinary raw-dict
    # hash/equality behavior inside the proven exact type.
    class Key:
        def __init__(self, value):
            self.value = value
        def __hash__(self):
            return 7
        def __eq__(self, other):
            return isinstance(other, Key) and self.value == other.value

    keys = [Key(i) for i in range(12)]
    lines = ["def colliding(x):", "    with switch(x):"]
    extra = {}
    for i, key in enumerate(keys):
        extra[f"K{i}"] = key
        lines += [f"        if case(K{i}):", f"            return {i}"]
    lines += ["        if case():", "            return -1"]
    colliding = _compile("\n".join(lines) + "\n", extra=extra)
    assert colliding.__pyswitch_typed_partition_plan_count__ == 1
    for i in range(collision_loops):
        v = (i * 13) % 17
        assert colliding(Key(v)) == (v if v < 12 else -1)
    calls += collision_loops

    mixed = _mixed()
    assert mixed.__pyswitch_typed_partition_plan_count__ == 1
    assert mixed.__pyswitch_typed_partition_type_count__ == 4
    mixed_values = [1, 1.0, True, "1", False, 2, 2.0, "x"]
    expected_mixed = {int: "int", float: "float", bool: "bool", str: "str"}
    for i in range(mixed_loops):
        x = mixed_values[i % len(mixed_values)]
        expected = expected_mixed.get(type(x), "miss") if x in (1, 1.0, True, "1") else "miss"
        assert mixed(x) == expected
    calls += mixed_loops

    class RaisingHash:
        def __hash__(self):
            raise TypeError("typed harness hash exploded")
    for fn in (direct, strings, expr, stmt, balanced):
        try:
            fn(RaisingHash()) if fn is not balanced else fn(RaisingHash(), False)
        except TypeError as exc:
            assert str(exc) == "typed harness hash exploded"
        else:
            raise AssertionError("user hash TypeError was swallowed")
        calls += 1

    rng = random.Random(0x1847EED)
    thread_values = [values[rng.randrange(len(values))] for _ in range(threaded)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        actual = list(pool.map(direct, thread_values, chunksize=256))
    expected = [_reference_int(x, 64) for x in thread_values]
    assert actual == expected
    calls += threaded

    async_source = '''async def async_typed(x):
    with switch(x):
        if case(0): return 10
        if case(1): return 11
        if case(2): return 12
        if case(): return -1
'''
    async_typed = _compile(async_source)
    assert async_typed.__pyswitch_typed_partition_plan_count__ == 1

    async def exercise_async():
        batch = 1000 if profile == "full" else 250
        completed = 0
        while completed < async_calls:
            count = min(batch, async_calls - completed)
            xs = [(completed + i) % 7 for i in range(count)]
            got = await asyncio.gather(*(async_typed(x) for x in xs))
            want = [10 + x if x in (0, 1, 2) else -1 for x in xs]
            assert got == want
            completed += count
    asyncio.run(exercise_async())
    calls += async_calls

    print(f"pyswitch typed partition {profile}: {calls:,}/{calls:,} calls passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["quick", "full"], default="quick")
    args = parser.parse_args()
    run(args.profile)
