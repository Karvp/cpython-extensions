"""Stress harness for allocation-free multi-type exact-type switch routing."""
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
    exec(compile(source, "<pyswitch-typed-router-v185-harness>", "exec"), ns)
    first = next(line for line in source.splitlines() if line.startswith(("def ", "async def ")))
    name = first.split("(", 1)[0].split()[-1]
    return enable_switch(mode="portable", case_key_mode="typed", source=source)(ns[name])


def _mixed_direct():
    source = '''def classify(x):
    with switch(x):
        if case(1): return 101
        if case(2): return 102
        if case(1.5): return 201
        if case(2.5): return 202
        if case(True): return 301
        if case(False): return 302
        if case("a"): return 401
        if case("b"): return 402
        if case(b"a"): return 501
        if case(b"b"): return 502
        if case((1,)): return 601
        if case((2,)): return 602
        if case(None): return 701
        if case(1j): return 801
        if case(2j): return 802
        if case(): return -1
'''
    return _compile(source)


def _expr():
    source = '''def expr(x):
    with switch(x):
        if case(1): return len(str(x)) + 101
        if case(1.5): return len(str(x)) + 103
        if case(True): return len(str(x)) + 107
        if case("a"): return len(str(x)) + 109
        if case(b"a"): return len(str(x)) + 113
        if case(): return len(str(x)) + -1
'''
    return _compile(source)


def _stmt():
    source = '''def stmt(x):
    with switch(x):
        if case(1):
            y = len(str(x)) + 101
            y *= 3
            return y
        if case(1.5):
            y = len(str(x)) + 103
            y *= 3
            return y
        if case(True):
            y = len(str(x)) + 107
            y *= 3
            return y
        if case("a"):
            y = len(str(x)) + 109
            y *= 3
            return y
        if case():
            y = len(str(x)) + -1
            y *= 3
            return y
'''
    return _compile(source)


def _balanced():
    source = '''def balanced(x, flag):
    with switch(x):
        if case(1, when=flag): return 10
        if case(1): return 11
        if case(1.5): return 20
        if case(True): return 30
        if case("a"): return 40
        if case(b"a"): return 50
        if case(): return -1
'''
    return _compile(source)


def _direct_reference(x):
    t = type(x)
    tables = {
        int: {1: 101, 2: 102},
        float: {1.5: 201, 2.5: 202},
        bool: {True: 301, False: 302},
        str: {"a": 401, "b": 402},
        bytes: {b"a": 501, b"b": 502},
        tuple: {(1,): 601, (2,): 602},
        type(None): {None: 701},
        complex: {1j: 801, 2j: 802},
    }
    table = tables.get(t)
    if table is None:
        try:
            hash(x)
        except TypeError:
            if t.__hash__ is None:
                return -1
            raise
        return -1
    return table.get(x, -1)


def run(profile: str) -> None:
    if profile == "full":
        direct_loops = 1_500_000
        template_loops = 650_000
        balanced_loops = 500_000
        collision_loops = 350_000
        generated_loops = 500_000
        threaded = 600_000
        async_calls = 80_000
    else:
        direct_loops = 80_000
        template_loops = 30_000
        balanced_loops = 30_000
        collision_loops = 20_000
        generated_loops = 30_000
        threaded = 30_000
        async_calls = 4_000

    calls = 0
    direct = _mixed_direct()
    assert direct.__pyswitch_typed_partition_plan_count__ == 1
    assert direct.__pyswitch_typed_partition_type_count__ == 8
    values = [
        1, 2, 3, 1.5, 2.5, 3.5, True, False, "a", "b", "x",
        b"a", b"b", b"x", (1,), (2,), (3,), None,
        1j, 2j, 3j, range(2), frozenset({3}), [], {}, set(),
    ]
    for i in range(direct_loops):
        x = values[(i * 37 + 11) % len(values)]
        assert direct(x) == _direct_reference(x)
    calls += direct_loops

    expr = _expr()
    stmt = _stmt()
    assert expr.__pyswitch_typed_partition_type_count__ == 5
    assert stmt.__pyswitch_typed_partition_type_count__ == 4
    expr_offsets = {int: 101, float: 103, bool: 107, str: 109, bytes: 113}
    expr_keys = {(int, 1), (float, 1.5), (bool, True), (str, "a"), (bytes, b"a")}
    stmt_offsets = {int: 101, float: 103, bool: 107, str: 109}
    stmt_keys = {(int, 1), (float, 1.5), (bool, True), (str, "a")}
    template_values = [1, 1.5, True, "a", b"a", 2, 2.0, False, "x", b"x"]
    for i in range(template_loops):
        x = template_values[(i * 13 + 3) % len(template_values)]
        key = (type(x), x)
        offset = expr_offsets[type(x)] if key in expr_keys else -1
        assert expr(x) == len(str(x)) + offset
        offset2 = stmt_offsets[type(x)] if key in stmt_keys else -1
        assert stmt(x) == (len(str(x)) + offset2) * 3
        calls += 2

    balanced = _balanced()
    assert balanced.__pyswitch_typed_partition_type_count__ == 5
    balanced_values = [1, 1.5, True, "a", b"a", 2, 2.0, False, "x", []]
    for i in range(balanced_loops):
        x = balanced_values[i % len(balanced_values)]
        flag = bool(i & 1)
        if type(x) is int and x == 1:
            expected = 10 if flag else 11
        elif type(x) is float and x == 1.5:
            expected = 20
        elif type(x) is bool and x is True:
            expected = 30
        elif type(x) is str and x == "a":
            expected = 40
        elif type(x) is bytes and x == b"a":
            expected = 50
        else:
            expected = -1
        assert balanced(x, flag) == expected
    calls += balanced_loops

    # Cross-type equal hashes must never make the subject dictionaries compare
    # values from distinct exact-type partitions.
    events = []
    class A:
        def __init__(self, value): self.value = value
        def __hash__(self): return 7
        def __eq__(self, other):
            if not isinstance(other, A):
                raise AssertionError("A cross-type equality")
            events.append(("A", self.value, other.value))
            return self.value == other.value
    class B:
        def __init__(self, value): self.value = value
        def __hash__(self): return 7
        def __eq__(self, other):
            if not isinstance(other, B):
                raise AssertionError("B cross-type equality")
            events.append(("B", self.value, other.value))
            return self.value == other.value
    extra = {}
    lines = ["def colliding(x):", "    with switch(x):"]
    for prefix, cls in (("A", A), ("B", B)):
        for i in range(8):
            name = f"{prefix}{i}"
            extra[name] = cls(i)
            lines += [f"        if case({name}):", f"            return {i + (0 if prefix == 'A' else 100)}"]
    lines += ["        if case():", "            return -1"]
    colliding = _compile("\n".join(lines) + "\n", extra=extra)
    assert colliding.__pyswitch_typed_partition_type_count__ == 2
    for i in range(collision_loops):
        value = (i * 17) % 11
        if i & 1:
            assert colliding(A(value)) == (value if value < 8 else -1)
        else:
            assert colliding(B(value)) == (100 + value if value < 8 else -1)
    calls += collision_loops

    # Generated 3-7-type functions exercise router construction/finalization.
    rng = random.Random(0x185A11)
    type_specs = [
        (int, [10, 11, 12]),
        (float, [10.5, 11.5, 12.5]),
        (bool, [True, False]),
        (str, ["g0", "g1", "g2"]),
        (bytes, [b"g0", b"g1", b"g2"]),
        (tuple, [(10,), (11,), (12,)]),
        (complex, [10j, 11j, 12j]),
    ]
    generated = []
    for width in range(3, 8):
        ns_extra = {}
        lines = [f"def gen{width}(x):", "    with switch(x):"]
        expected = {}
        counter = 0
        for _type, keys in type_specs[:width]:
            for key in keys:
                name = f"K{width}_{counter}"
                ns_extra[name] = key
                expected[(type(key), key)] = counter
                lines += [f"        if case({name}):", f"            return {counter}"]
                counter += 1
        lines += ["        if case():", "            return -1"]
        fn = _compile("\n".join(lines) + "\n", extra=ns_extra)
        assert fn.__pyswitch_typed_partition_type_count__ == width
        generated.append((fn, expected, [key for _t, keys in type_specs[:width] for key in keys]))
    for i in range(generated_loops):
        fn, expected, keys = generated[i % len(generated)]
        if i % 5:
            x = keys[rng.randrange(len(keys))]
        else:
            x = None
        assert fn(x) == expected.get((type(x), x), -1)
    calls += generated_loops

    class RaisingHash:
        def __hash__(self):
            raise TypeError("multi-router subject hash exploded")
    for fn, args in ((direct, (RaisingHash(),)), (expr, (RaisingHash(),)), (balanced, (RaisingHash(), False))):
        try:
            fn(*args)
        except TypeError as exc:
            assert str(exc) == "multi-router subject hash exploded"
        else:
            raise AssertionError("user hash TypeError was swallowed")
        calls += 1

    thread_values = [values[rng.randrange(len(values))] for _ in range(threaded)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        actual = list(pool.map(direct, thread_values, chunksize=256))
    expected = [_direct_reference(x) for x in thread_values]
    assert actual == expected
    calls += threaded

    async_source = '''async def async_typed(x):
    with switch(x):
        if case(1): return 10
        if case(1.5): return 20
        if case(True): return 30
        if case("a"): return 40
        if case(): return -1
'''
    async_typed = _compile(async_source)
    assert async_typed.__pyswitch_typed_partition_type_count__ == 4
    async_values = [1, 1.5, True, "a", 2, 2.0, False, "x", []]

    async def exercise_async():
        done = 0
        batch = 1000 if profile == "full" else 250
        while done < async_calls:
            n = min(batch, async_calls - done)
            xs = [async_values[(done + i) % len(async_values)] for i in range(n)]
            got = await asyncio.gather(*(async_typed(x) for x in xs))
            want = []
            for x in xs:
                if type(x) is int and x == 1: want.append(10)
                elif type(x) is float and x == 1.5: want.append(20)
                elif type(x) is bool and x is True: want.append(30)
                elif type(x) is str and x == "a": want.append(40)
                else: want.append(-1)
            assert got == want
            done += n
    asyncio.run(exercise_async())
    calls += async_calls

    print(f"pyswitch typed router {profile}: {calls:,}/{calls:,} calls passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["quick", "full"], default="quick")
    args = parser.parse_args()
    run(args.profile)
