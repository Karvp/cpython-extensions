"""Compatibility/stress harness for pyswitch 0.18.2 opt-in live backends.

The portable backend is the production contract.  This harness keeps the
explicit CPython-3.13 live modes honest: sequential fast dispatch, per-thread
clones, depth-isolated recursion, per-call suspendables, typed keys, tracing,
and exceptional hash behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import random
import sys
from typing import Any

from python_extensions import case, enable_switch, switch


def compile_source(source: str, name: str, mode: str, *, typed: bool = False,
                   prefix_lines: int = 0, expose_debug: bool = False):
    filename = f"<pyswitch-live-v182-{name}-{mode}>"
    ns = {"switch": switch, "case": case}
    exec(compile("\n" * prefix_lines + source, filename, "exec"), ns)
    fn = enable_switch(
        mode=mode,
        source=source,
        case_key_mode="typed" if typed else "python",
        expose_debug=expose_debug,
        max_cached_depth=8,
    )(ns[name])
    # Mirror decorator syntax: recursive global-name calls resolve to the
    # decorated callable after definition-time rebinding.
    ns[name] = fn
    return fn, filename


BASIC_SOURCE = '''def basic(value):
    with switch(value):
        if case(0): return value + 10
        if case(1): return value + 20
        if case(2): return value + 30
        if case(3): return value + 40
        if case(): return value + 100
'''

TYPED_SOURCE = '''def typed(value):
    with switch(value):
        if case(1): return "int"
        if case(1.0): return "float"
        if case(True): return "bool"
        if case("1"): return "str"
        if case(): return "miss"
'''

RECURSIVE_SOURCE = '''def recursive(depth, selector):
    with switch(selector):
        if case(0): amount = 1
        if case(1): amount = 2
        if case(): amount = 3
    if depth <= 0:
        return amount
    return amount + recursive(depth - 1, selector)
'''

GEN_SOURCE = '''def gen(value, count):
    with switch(value):
        if case(0): base = 10
        if case(1): base = 20
        if case(): base = 30
    for index in range(count):
        received = yield base + index
        if received is not None:
            base += received
'''

CORO_SOURCE = '''async def coro(value):
    await asyncio.sleep(0)
    with switch(value):
        if case(0): result = 10
        if case(1): result = 20
        if case(): result = 30
    await asyncio.sleep(0)
    return result + value
'''

HASH_SOURCE = '''def hashy(value):
    with switch(value):
        if case(1): return 10
        if case(2): return 20
        if case(): return 30
'''


class RaisingHash:
    def __hash__(self):
        raise TypeError("hash exploded")


def ref_basic(value: int) -> int:
    return value + ({0: 10, 1: 20, 2: 30, 3: 40}.get(value, 100))


def run(profile: str) -> int:
    if profile == "full":
        loops = dict(fast=220_000, thread=320_000, isolated=420_000,
                     per_call=55_000, typed_each=30_000, trace_each=700,
                     recursive=4_000, generator=12_000, coroutine=12_000,
                     unhashable=15_000)
    else:
        loops = dict(fast=20_000, thread=30_000, isolated=40_000,
                     per_call=5_000, typed_each=3_000, trace_each=80,
                     recursive=300, generator=1_000, coroutine=1_000,
                     unhashable=1_000)

    calls = 0
    rng = random.Random(0x5181825)

    # Shared fast mode is deliberately sequential only.
    fast, _ = compile_source(BASIC_SOURCE, "basic", "fast")
    for _ in range(loops["fast"]):
        value = rng.randrange(-8, 12)
        assert fast(value) == ref_basic(value)
    calls += loops["fast"]

    # One clone per thread: safe across threads, intentionally not re-entry-safe.
    thread_local, _ = compile_source(BASIC_SOURCE, "basic", "thread_local")
    values = [rng.randrange(-8, 12) for _ in range(loops["thread"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        actual = list(pool.map(thread_local, values, chunksize=256))
    assert actual == [ref_basic(value) for value in values]
    calls += len(values)

    # Per-depth isolation handles both contention and re-entry.
    isolated, _ = compile_source(BASIC_SOURCE, "basic", "isolated")
    values = [rng.randrange(-8, 12) for _ in range(loops["isolated"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        actual = list(pool.map(isolated, values, chunksize=256))
    assert actual == [ref_basic(value) for value in values]
    calls += len(values)

    per_call, _ = compile_source(BASIC_SOURCE, "basic", "per_call")
    for _ in range(loops["per_call"]):
        value = rng.randrange(-8, 12)
        assert per_call(value) == ref_basic(value)
    calls += loops["per_call"]

    # Exact-type key identity must remain identical across every live wrapper.
    typed_values: tuple[tuple[Any, str], ...] = (
        (1, "int"), (1.0, "float"), (True, "bool"), ("1", "str"),
        (False, "miss"), (2, "miss"), ([], "miss"),
    )
    for mode in ("fast", "thread_local", "isolated", "per_call"):
        typed, _ = compile_source(TYPED_SOURCE, "typed", mode, typed=True)
        for index in range(loops["typed_each"]):
            value, expected = typed_values[index % len(typed_values)]
            assert typed(value) == expected
        calls += loops["typed_each"]

    # Trace line tables must stay in the original physical source range even
    # for generated isolation wrappers.
    for mode in ("fast", "thread_local", "isolated", "per_call"):
        traced, filename = compile_source(
            BASIC_SOURCE, "basic", mode, prefix_lines=96, expose_debug=True
        )
        seen = []
        def tracer(frame, event, arg):
            if frame.f_code.co_filename == filename and event == "line":
                assert frame.f_lineno >= 97
                seen.append(frame.f_lineno)
            return tracer
        sys.settrace(tracer)
        try:
            for index in range(loops["trace_each"]):
                value = index % 6
                assert traced(value) == ref_basic(value)
                calls += 1
        finally:
            sys.settrace(None)
        assert seen and all(line > 0 for line in seen)

    # Deep self-recursion stays on the isolation wrapper/private self cell.
    for mode in ("isolated", "per_call"):
        recursive, _ = compile_source(RECURSIVE_SOURCE, "recursive", mode)
        for index in range(loops["recursive"]):
            depth = 1 + index % 20
            selector = index % 3
            amount = selector + 1
            assert recursive(depth, selector) == amount * (depth + 1)
            calls += 1

    # Suspendable functions require per-depth/per-call isolation.
    for mode in ("isolated", "per_call"):
        gen, _ = compile_source(GEN_SOURCE, "gen", mode)
        for index in range(loops["generator"]):
            value = index % 3
            base = 10 if value == 0 else 20 if value == 1 else 30
            iterator = gen(value, 3)
            assert next(iterator) == base
            assert iterator.send(2) == base + 3
            assert next(iterator) == base + 4
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise AssertionError("generator failed to stop")
            calls += 1

    async def coroutine_stress() -> int:
        total = 0
        for mode in ("isolated", "per_call"):
            coro, _ = compile_source(
                CORO_SOURCE, "coro", mode, expose_debug=False
            )
            # The source uses asyncio as a global in the recompiled function.
            coro.__globals__["asyncio"] = asyncio
            batch = 600 if profile == "full" else 100
            remaining = loops["coroutine"]
            while remaining:
                size = min(batch, remaining)
                values = [rng.randrange(-3, 5) for _ in range(size)]
                results = await asyncio.gather(*(coro(v) for v in values))
                expected = [
                    (10 if v == 0 else 20 if v == 1 else 30) + v
                    for v in values
                ]
                assert results == expected
                total += size
                remaining -= size
        return total
    calls += asyncio.run(coroutine_stress())

    # Intrinsically unhashable values miss; user hash TypeErrors propagate.
    for mode in ("fast", "isolated", "per_call"):
        hashy, _ = compile_source(HASH_SOURCE, "hashy", mode)
        for _ in range(loops["unhashable"]):
            assert hashy([]) == 30
            calls += 1
        for _ in range(100 if profile == "full" else 20):
            try:
                hashy(RaisingHash())
            except TypeError as exc:
                assert str(exc) == "hash exploded"
            else:
                raise AssertionError("user __hash__ TypeError was swallowed")
            calls += 1

    print(f"pyswitch live compatibility v18.2 {profile}: {calls:,} calls passed")
    print("isolated cache:", isolated.__pyswitch_cache_info__())
    return calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    run(args.profile)


if __name__ == "__main__":
    main()
