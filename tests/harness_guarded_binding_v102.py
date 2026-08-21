from __future__ import annotations

import argparse
import asyncio
import functools
import json
import threading
import time
from dataclasses import asdict, dataclass

from python_extensions import __version__, inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def direct_target(value, bias=1):
    a = value + bias
    b = a * 3
    c = b - 5
    return c * c + 7


GLOBAL_ALIAS = direct_target


@inline_calls(policy="always", binding="guarded")
def guarded_direct(value):
    return direct_target(value)


@inline_calls(policy="always", binding="guarded")
def guarded_alias(value):
    return GLOBAL_ALIAS(value)


@inline_function(register_only=True)
def kw_target(value, *, bias=2):
    return (value * 5) + bias


@inline_calls(policy="always", binding="guarded")
def guarded_kw(value):
    return kw_target(value)


class MethodTarget:
    def __init__(self, bias):
        self.bias = bias

    @inline_function(register_only=True)
    def apply(self, value):
        return value * 3 + self.bias


METHOD_INSTANCE = MethodTarget(4)
ORIGINAL_METHOD = MethodTarget.apply


@inline_calls(policy="always", binding="guarded")
def guarded_method(value):
    return METHOD_INSTANCE.apply(value)


@inline_function(register_only=True)
def partial_target(value, *, bias=1):
    return value * 7 + bias


PARTIAL = functools.partial(partial_target, bias=3)


@inline_calls(policy="always", binding="guarded")
def guarded_partial(value):
    return PARTIAL(value)


@inline_function(register_only=True, shared_region=True)
def shared_target(value, bias=1):
    value = value * 3 + bias
    value = value * 5 - bias
    value = value * 7 + bias
    return value


@inline_calls(
    policy="always",
    binding="guarded",
    shared_regions="auto",
    shared_min_body_instructions=1,
)
def guarded_shared(value):
    a = shared_target(value)
    b = shared_target(a)
    return shared_target(b)


@inline_calls(policy="always", binding="guarded")
async def guarded_async(value):
    await asyncio.sleep(0)
    return direct_target(value)


@dataclass
class Result:
    stable_direct: int = 0
    global_rebind: int = 0
    defaults_mutation: int = 0
    kwdefaults_mutation: int = 0
    method_replacement: int = 0
    partial_mutation: int = 0
    shared_mutation: int = 0
    threaded: int = 0
    async_calls: int = 0
    elapsed_seconds: float = 0.0

    @property
    def total_operations(self) -> int:
        return (
            self.stable_direct
            + self.global_rebind
            + self.defaults_mutation
            + self.kwdefaults_mutation
            + self.method_replacement
            + self.partial_mutation
            + self.shared_mutation
            + self.threaded
            + self.async_calls
        )


def run(scale: float = 1.0) -> Result:
    global GLOBAL_ALIAS
    result = Result()
    start = time.perf_counter()

    n = max(1, int(1_000_000 * scale))
    for index in range(n):
        value = index % 10007 - 5000
        expected = direct_target(value)
        actual = guarded_direct(value)
        if actual != expected:
            raise AssertionError(("stable_direct", index, expected, actual))
    result.stable_direct = n

    original_alias = GLOBAL_ALIAS

    def rebound(value):
        return value * 11 - 9

    n = max(1, int(500_000 * scale))
    try:
        for index in range(n):
            if index % 997 == 0:
                GLOBAL_ALIAS = rebound if GLOBAL_ALIAS is original_alias else original_alias
            value = index % 4001 - 2000
            expected = GLOBAL_ALIAS(value)
            actual = guarded_alias(value)
            if actual != expected:
                raise AssertionError(("global_rebind", index, expected, actual))
    finally:
        GLOBAL_ALIAS = original_alias
    result.global_rebind = n

    original_defaults = direct_target.__defaults__
    n = max(1, int(500_000 * scale))
    try:
        for index in range(n):
            if index % 991 == 0:
                direct_target.__defaults__ = (9,) if direct_target.__defaults__ == (1,) else (1,)
            value = index % 3001 - 1500
            expected = direct_target(value)
            actual = guarded_direct(value)
            if actual != expected:
                raise AssertionError(("defaults", index, expected, actual))
    finally:
        direct_target.__defaults__ = original_defaults
    result.defaults_mutation = n

    assert kw_target.__kwdefaults__ is not None
    original_kw = dict(kw_target.__kwdefaults__)
    n = max(1, int(400_000 * scale))
    try:
        for index in range(n):
            if index % 983 == 0:
                current = kw_target.__kwdefaults__["bias"]
                kw_target.__kwdefaults__["bias"] = 17 if current == 2 else 2
            value = index % 2503 - 1250
            expected = kw_target(value)
            actual = guarded_kw(value)
            if actual != expected:
                raise AssertionError(("kwdefaults", index, expected, actual))
    finally:
        kw_target.__kwdefaults__.clear()
        kw_target.__kwdefaults__.update(original_kw)
    result.kwdefaults_mutation = n

    n = max(1, int(400_000 * scale))

    def replacement_method(self, value):
        return value - self.bias

    try:
        for index in range(n):
            if index % 977 == 0:
                MethodTarget.apply = (
                    replacement_method if MethodTarget.apply is ORIGINAL_METHOD else ORIGINAL_METHOD
                )
            value = index % 2003 - 1000
            expected = METHOD_INSTANCE.apply(value)
            actual = guarded_method(value)
            if actual != expected:
                raise AssertionError(("method", index, expected, actual))
    finally:
        MethodTarget.apply = ORIGINAL_METHOD
    result.method_replacement = n

    assert PARTIAL.keywords is not None
    original_partial_keywords = dict(PARTIAL.keywords)
    n = max(1, int(400_000 * scale))
    try:
        for index in range(n):
            if index % 971 == 0:
                PARTIAL.keywords["bias"] = 29 if PARTIAL.keywords["bias"] == 3 else 3
            value = index % 2203 - 1100
            expected = PARTIAL(value)
            actual = guarded_partial(value)
            if actual != expected:
                raise AssertionError(("partial", index, expected, actual))
    finally:
        PARTIAL.keywords.clear()
        PARTIAL.keywords.update(original_partial_keywords)
    result.partial_mutation = n

    original_shared_defaults = shared_target.__defaults__
    n = max(1, int(300_000 * scale))
    try:
        for index in range(n):
            if index % 967 == 0:
                shared_target.__defaults__ = (
                    (13,) if shared_target.__defaults__ == (1,) else (1,)
                )
            value = index % 997 - 498
            expected = shared_target(shared_target(shared_target(value)))
            actual = guarded_shared(value)
            if actual != expected:
                raise AssertionError(("shared", index, expected, actual))
    finally:
        shared_target.__defaults__ = original_shared_defaults
    result.shared_mutation = n

    per_thread = max(1, int(200_000 * scale))
    workers = 8
    barrier = threading.Barrier(workers)
    failures: list[tuple[int, int, int]] = []

    def worker(seed: int) -> None:
        barrier.wait()
        for index in range(per_thread):
            value = ((seed * per_thread + index) % 10007) - 5000
            expected = direct_target(value)
            actual = guarded_direct(value)
            if actual != expected:
                failures.append((value, expected, actual))
                return

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise AssertionError(("threaded", failures[0]))
    result.threaded = per_thread * workers

    async_count = max(1, int(10_000 * scale))

    async def run_async() -> None:
        chunk = 250
        for start_index in range(0, async_count, chunk):
            stop = min(async_count, start_index + chunk)
            values = list(range(start_index, stop))
            actual = await asyncio.gather(*(guarded_async(value) for value in values))
            expected = [direct_target(value) for value in values]
            if actual != expected:
                raise AssertionError(("async", start_index))

    asyncio.run(run_async())
    result.async_calls = async_count

    for function in (
        guarded_direct,
        guarded_alias,
        guarded_kw,
        guarded_method,
        guarded_partial,
        guarded_shared,
        guarded_async,
    ):
        verification = verify_code(function.__code__)
        if not verification.valid:
            raise AssertionError((function.__qualname__, verification.errors))

    result.elapsed_seconds = time.perf_counter() - start
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run(args.scale)
    payload = asdict(result)
    payload["total_operations"] = result.total_operations
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"guarded binding v{__version__}: {result.total_operations:,} operations "
            f"in {result.elapsed_seconds:.3f}s"
        )


if __name__ == "__main__":
    main()
