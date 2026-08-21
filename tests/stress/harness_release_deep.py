from __future__ import annotations

import argparse
import asyncio
import gc
import json
import random
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

from python_extensions import (
    clear_inline_registry,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    unregister_inline_function,
    verify_code,
)
from python_extensions.inline import InlineRecursionError
from python_extensions.switch import case, fallthrough, switch


@enable_switch(mode="portable")
def portable_fallthrough(value: object) -> int:
    out = 0
    with switch(value):
        if case(0):
            out += 1
            fallthrough()
        if case(1):
            out += 10
            fallthrough()
        if case("x"):
            out += 100
        if case():
            out += 1000
    return out


@enable_switch(mode="portable")
def portable_guarded(value: object, guard: bool) -> str:
    with switch(value):
        if case(1, when=guard):
            return "guarded"
        elif case(1):
            return "one"
        elif case("x"):
            return "x"
        elif case():
            return "default"


@enable_switch(mode="portable", case_key_mode="typed")
def portable_typed(value: object) -> str:
    with switch(value):
        if case(1):
            return "int"
        if case(1.0):
            return "float"
        if case(True):
            return "bool"
        if case("1"):
            return "str"
        if case():
            return "default"


@enable_switch(mode="portable", compact_routes="auto")
def compacted(value: int) -> int:
    total = 0
    with switch(value):
        if case(0):
            total += 1
            fallthrough()
        if case(1):
            total += 2
            fallthrough()
        if case(2):
            total += 3
            fallthrough()
        if case(3):
            total += 4
            fallthrough()
        if case():
            total = total * 3 + 7
            total ^= 0x55
            total += 11
            total *= 2
            total -= 9
    return total


def _live_body(decorator):
    @decorator
    def route(value: int) -> int:
        with switch(value):
            if case(0): return 10
            if case(1): return 11
            if case(2): return 12
            if case(3): return 13
            if case(4): return 14
            if case(5): return 15
            if case(6): return 16
            if case(7): return 17
            if case(): return -1
    return route


LIVE_ISOLATED = _live_body(enable_switch(mode="isolated"))
LIVE_THREAD_LOCAL = _live_body(enable_switch(mode="thread_local"))
LIVE_PER_CALL = _live_body(enable_switch(mode="per_call"))
LIVE_FAST = _live_body(enable_switch(mode="fast"))


@enable_goto
def goto_machine(value: int) -> int:
    total = 0
    label .loop
    if value <= 0:
        goto .done
    if value & 1:
        goto .odd
    total += value * 2
    goto .step
    label .odd
    total += value * 3
    label .step
    value -= 1
    goto .loop
    label .done
    return total


def goto_reference(value: int) -> int:
    total = 0
    while value > 0:
        total += value * (3 if value & 1 else 2)
        value -= 1
    return total


def switch_reference_fallthrough(value: object) -> int:
    if value == 0 and hash(value) == hash(0):
        return 1 + 10 + 100
    if value == 1 and hash(value) == hash(1):
        return 10 + 100
    if value == "x":
        return 100
    return 1000


def switch_reference_typed(value: object) -> str:
    if type(value) is int and value == 1:
        return "int"
    if type(value) is float and value == 1.0:
        return "float"
    if type(value) is bool and value is True:
        return "bool"
    if type(value) is str and value == "1":
        return "str"
    return "default"


def run_switch(rounds: int) -> int:
    rng = random.Random(0x51A7C4)
    values = [0, 1, 2, 7, "x", "1", True, 1.0, None, (1, 2)]
    calls = 0
    for _ in range(rounds):
        value = rng.choice(values)
        assert portable_fallthrough(value) == switch_reference_fallthrough(value)
        guard = bool(rng.getrandbits(1))
        expected = "guarded" if value == 1 and guard else ("one" if value == 1 else "x" if value == "x" else "default")
        assert portable_guarded(value, guard) == expected
        assert portable_typed(value) == switch_reference_typed(value)
        calls += 3
    for value in range(-4, 8):
        result = compacted(value)
        # Differential reference preserving explicit fallthrough sequence.
        total = 0
        if value == 0:
            total += 1; total += 2; total += 3; total += 4
        elif value == 1:
            total += 2; total += 3; total += 4
        elif value == 2:
            total += 3; total += 4
        elif value == 3:
            total += 4
        # case(3) falls through, so every matched 0..3 route reaches default.
        total = total * 3 + 7; total ^= 0x55; total += 11; total *= 2; total -= 9
        assert result == total
        calls += 1
    return calls


def run_live(rounds: int) -> int:
    calls = 0
    for func, count in (
        (LIVE_FAST, rounds),
        (LIVE_ISOLATED, rounds),
        (LIVE_THREAD_LOCAL, rounds),
        (LIVE_PER_CALL, max(1, rounds // 20)),
    ):
        for i in range(count):
            value = (i * 17) % 13 - 2
            expected = value + 10 if 0 <= value <= 7 else -1
            assert func(value) == expected
        calls += count

    per_worker = max(1, rounds // 8)
    barrier = threading.Barrier(8)

    def worker(seed: int) -> int:
        barrier.wait()
        total = 0
        for i in range(per_worker):
            value = (i * 19 + seed) % 12 - 2
            expected = value + 10 if 0 <= value <= 7 else -1
            assert LIVE_ISOLATED(value) == expected
            assert LIVE_THREAD_LOCAL(value) == expected
            total += 2
        return total

    with ThreadPoolExecutor(max_workers=8) as pool:
        calls += sum(pool.map(worker, range(8)))
    return calls


def run_goto(rounds: int) -> int:
    rng = random.Random(0x6070)
    for _ in range(rounds):
        value = rng.randrange(-3, 80)
        assert goto_machine(value) == goto_reference(value)
    assert verify_code(goto_machine.__code__).valid
    return rounds


def run_registry(rounds: int) -> int:
    clear_inline_registry()
    calls = 0

    def worker(index: int) -> int:
        namespace = {
            "__name__": f"deep_registry_{index}",
            "inline_function": inline_function,
            "inline_calls": inline_calls,
        }
        local_calls = 0
        for step in range(rounds):
            bias = index * 100_000 + step
            exec(
                f"def helper(x, _bias={bias}):\n    return x + _bias\n",
                namespace,
            )
            helper = inline_function(register_only=True)(namespace["helper"])
            namespace["helper"] = helper
            exec("def caller(x):\n    return helper(x)\n", namespace)
            caller = inline_calls(policy="always")(namespace["caller"])
            assert caller(7) == 7 + bias
            assert caller.__inline_stats__.calls_inlined == 1
            assert unregister_inline_function(helper)
            local_calls += 1
        return local_calls

    with ThreadPoolExecutor(max_workers=8) as pool:
        calls += sum(pool.map(worker, range(8)))

    # Failure-atomicity churn against one lexical global registration slot.
    namespace = {
        "__name__": "deep_registry_failure",
        "inline_function": inline_function,
    }
    for _ in range(max(1, rounds // 4)):
        exec(
            "def recursive(n):\n"
            "    return 0 if n <= 0 else recursive(n - 1) + 1\n",
            namespace,
        )
        failed = namespace["recursive"]
        try:
            inline_function(failed)
        except InlineRecursionError:
            pass
        else:
            raise AssertionError("recursive registration unexpectedly succeeded")
        assert not any(name.startswith("__inline_") for name in failed.__dict__)
        local_registry = tuple(
            name for name in __import__("python_extensions").registered_inline_functions()
            if name.startswith("deep_registry_failure.")
        )
        assert not local_registry
        calls += 1

    clear_inline_registry()
    gc.collect()
    return calls


def _verify_nested(code: types.CodeType) -> int:
    count = 1
    result = verify_code(code, raise_on_error=False)
    assert result.valid, (code.co_name, result.errors)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            count += _verify_nested(const)
    return count


def run_verifier(rounds: int) -> int:
    verified = 0
    for i in range(rounds):
        mod = i % 6
        if mod == 0:
            source = f"def f_{i}(n):\n    out = 0\n    for x in range(n):\n        if x % 3 == 0:\n            continue\n        out += x * {i % 11 + 1}\n    return out\n"
        elif mod == 1:
            source = f"def f_{i}(x):\n    try:\n        return ({i + 7} // x) + x\n    except ZeroDivisionError:\n        return -1\n    finally:\n        x = x + 1\n"
        elif mod == 2:
            source = f"def f_{i}(xs):\n    return sum((x + {i % 5}) * y for x in xs for y in range(x & 7))\n"
        elif mod == 3:
            source = f"def f_{i}(x):\n    match x:\n        case [a, b, *rest]:\n            return a, b, rest\n        case {{'k': v}}:\n            return v\n        case _:\n            return None\n"
        elif mod == 4:
            source = f"async def f_{i}(n):\n    total = 0\n    for x in range(n):\n        await asyncio.sleep(0)\n        total += x\n    return total\n"
        else:
            source = f"def f_{i}(n):\n    yield from range(n)\n    return {i}\n"
        namespace = {"asyncio": asyncio}
        exec(compile(source, f"<verify-{i}>", "exec"), namespace)
        verified += _verify_nested(namespace[f"f_{i}"].__code__)
    return verified


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        switch_rounds, live_rounds, goto_rounds, registry_rounds, verifier_rounds = 20_000, 10_000, 20_000, 50, 120
    else:
        switch_rounds, live_rounds, goto_rounds, registry_rounds, verifier_rounds = 600_000, 250_000, 700_000, 500, 1000

    started = time.perf_counter()
    counts = {
        "switch_differential_calls": run_switch(switch_rounds),
        "live_switch_calls": run_live(live_rounds),
        "goto_differential_calls": run_goto(goto_rounds),
        "registry_transactions": run_registry(registry_rounds),
        "verified_code_objects": run_verifier(verifier_rounds),
    }
    elapsed = time.perf_counter() - started
    counts["total_runtime_operations"] = (
        counts["switch_differential_calls"]
        + counts["live_switch_calls"]
        + counts["goto_differential_calls"]
        + counts["registry_transactions"]
    )
    print(json.dumps({"status": "passed", "elapsed_seconds": round(elapsed, 3), **counts}, indent=2))


if __name__ == "__main__":
    main()
