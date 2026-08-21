#!/usr/bin/env python3
"""Application-style stress coverage for the public API."""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from python_extensions import (
    case,
    clear_inline_registry,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    optimize_extensions,
    registered_inline_functions,
    switch,
    verify_code,
)


@enable_switch
def command(kind, value):
    with switch(kind):
        if case("add"):
            return value + 4
        if case("mul"):
            return value * 3
        if case("neg"):
            return -value
        if case():
            return value


@enable_switch(case_key_mode="typed")
def typed(value):
    with switch(value):
        if case(1):
            return "int"
        if case(1.0):
            return "float"
        if case(True):
            return "bool"
        if case():
            return "other"


@enable_switch
def generator_route(kind, n):
    with switch(kind):
        if case("up"):
            yield from range(n)
        if case("down"):
            yield from range(n, 0, -1)
        if case():
            return


@enable_switch
async def async_route(kind, value):
    await asyncio.sleep(0)
    with switch(kind):
        if case("inc"):
            return value + 1
        if case("double"):
            return value * 2
        if case():
            return value


@inline_function(register_only=True)
def affine(value, scale=5, bias=3):
    return value * scale + bias


@inline_calls(policy="speed", binding="frozen")
def inline_frozen(value):
    return affine(value)


@inline_calls(policy="always", binding="guarded")
def inline_guarded(value):
    return affine(value)


@inline_calls(policy="always", binding="guarded")
async def inline_async(value):
    await asyncio.sleep(0)
    return affine(value)


@enable_goto
def goto_sum(n):
    total = 0
    label .loop
    if n <= 0:
        goto .done
    total += n
    n -= 1
    goto .loop
    label .done
    return total


@optimize_extensions(switch=True, inline=False, goto=True)
def mini_vm(op, value):
    with switch(op):
        if case("skip"):
            goto .done
        if case("inc"):
            value += 1
        if case("double"):
            value *= 2
        if case():
            value -= 1
    label .done
    return value


TRANSFORMED = (
    command,
    typed,
    generator_route,
    async_route,
    inline_frozen,
    inline_guarded,
    inline_async,
    goto_sum,
    mini_vm,
)


def _reference_command(kind, value):
    if kind == "add":
        return value + 4
    if kind == "mul":
        return value * 3
    if kind == "neg":
        return -value
    return value


def _run_async(rounds: int) -> int:
    async def scenario():
        tasks = []
        expected = []
        for i in range(rounds):
            kind = ("inc", "double", "other")[i % 3]
            value = i % 97
            tasks.append(async_route(kind, value))
            expected.append(value + 1 if kind == "inc" else value * 2 if kind == "double" else value)
        got = await asyncio.gather(*tasks)
        assert got == expected

        inline_tasks = [inline_async(i % 101) for i in range(rounds)]
        inline_got = await asyncio.gather(*inline_tasks)
        assert inline_got == [affine(i % 101) for i in range(rounds)]

    asyncio.run(scenario())
    return rounds * 2


def run(scale: float) -> dict[str, int]:
    rng = random.Random(0xC01313)
    counts: dict[str, int] = {}

    rounds = max(1, int(250_000 * scale))
    kinds = ("add", "mul", "neg", "other", "")
    for _ in range(rounds):
        kind = rng.choice(kinds)
        value = rng.randint(-50_000, 50_000)
        assert command(kind, value) == _reference_command(kind, value)
    counts["switch_random"] = rounds

    typed_values = (1, 1.0, True, False, 2, "1", None)
    expected = {int: "int", float: "float", bool: "bool"}
    typed_rounds = max(1, int(150_000 * scale))
    for _ in range(typed_rounds):
        value = rng.choice(typed_values)
        want = expected.get(type(value), "other") if value in (1, 1.0, True) else "other"
        if type(value) is bool and value is False:
            want = "other"
        if type(value) is int and value != 1:
            want = "other"
        if type(value) is float and value != 1.0:
            want = "other"
        assert typed(value) == want
    counts["typed_switch"] = typed_rounds

    gen_rounds = max(1, int(25_000 * scale))
    for i in range(gen_rounds):
        n = i % 12
        assert list(generator_route("up", n)) == list(range(n))
        assert list(generator_route("down", n)) == list(range(n, 0, -1))
    counts["generator_switch"] = gen_rounds * 2

    inline_rounds = max(1, int(250_000 * scale))
    for _ in range(inline_rounds):
        value = rng.randint(-10_000, 10_000)
        want = value * 5 + 3
        assert inline_frozen(value) == want
        assert inline_guarded(value) == want
    counts["inline_stable"] = inline_rounds * 2

    # Guarded semantics must track mutation; frozen semantics intentionally do not.
    old_defaults = affine.__defaults__
    affine.__defaults__ = (7, 11)
    try:
        assert inline_guarded(2) == affine(2) == 25
        assert inline_frozen(2) == 13
    finally:
        affine.__defaults__ = old_defaults
    counts["guarded_deopt"] = 2

    goto_rounds = max(1, int(200_000 * scale))
    for _ in range(goto_rounds):
        n = rng.randint(0, 40)
        assert goto_sum(n) == n * (n + 1) // 2
    counts["goto_loop"] = goto_rounds

    vm_rounds = max(1, int(150_000 * scale))
    for _ in range(vm_rounds):
        op = rng.choice(("skip", "inc", "double", "other"))
        value = rng.randint(-1000, 1000)
        want = value if op == "skip" else value + 1 if op == "inc" else value * 2 if op == "double" else value - 1
        assert mini_vm(op, value) == want
    counts["composition"] = vm_rounds

    async_rounds = max(1, int(2_000 * scale))
    counts["async"] = _run_async(async_rounds)

    per_thread = max(1, int(50_000 * scale))
    workers = 8

    def threaded(seed: int) -> int:
        local = random.Random(seed)
        calls = 0
        for _ in range(per_thread):
            value = local.randint(-1000, 1000)
            kind = local.choice(kinds)
            assert command(kind, value) == _reference_command(kind, value)
            assert inline_guarded(value) == value * 5 + 3
            n = local.randint(0, 20)
            assert goto_sum(n) == n * (n + 1) // 2
            calls += 3
        return calls

    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts["threaded"] = sum(pool.map(threaded, range(workers)))

    for function in TRANSFORMED:
        result = verify_code(function.__code__, raise_on_error=False)
        assert result.valid, (function.__qualname__, result.errors)
    counts["verified_code_objects"] = len(TRANSFORMED)

    # Registration churn should not deadlock and should clean up ephemeral entries.
    churn = max(1, int(1_000 * scale))
    baseline = set(registered_inline_functions())
    for i in range(churn):
        def factory(delta):
            @inline_function(register_only=True, freeze_closures=True)
            def ephemeral(value):
                return value + delta
            return ephemeral
        fn = factory(i)
        assert fn(1) == i + 1
        del fn
        if i % 50 == 0:
            gc.collect()
    gc.collect()
    assert baseline.issubset(set(registered_inline_functions()))
    counts["registry_churn"] = churn

    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    if args.scale <= 0:
        parser.error("--scale must be > 0")
    started = time.perf_counter()
    counts = run(args.scale)
    report = {
        "status": "pass",
        "scale": args.scale,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "operations": sum(counts.values()),
        "sections": counts,
        "threads": threading.active_count(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
