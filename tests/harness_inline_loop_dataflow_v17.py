from __future__ import annotations

import argparse
import itertools
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function

_COUNTER = itertools.count()


@inline_function(register_only=True)
def _true():
    return True


@inline_function(register_only=True)
def _identity(value):
    return value


@inline_function(register_only=True)
def _choose(flag, value):
    if flag:
        return value + 7
    return value - 11


def build_constant_loop(kind: int):
    name = f"loop_const_{next(_COUNTER)}"
    if kind == 0:
        body = f'''def {name}(value, count):
    produced = _true()
    alias = produced
    total = 0
    while count > 0:
        total += _choose(alias, value)
        count -= 1
    return total
'''
    elif kind == 1:
        body = f'''def {name}(value, count):
    produced = _true()
    alias = produced
    total = 0
    while count > 0:
        count -= 1
        if count & 1:
            total += _choose(alias, value)
            continue
        total += _choose(alias, value)
    return total
'''
    elif kind == 2:
        body = f'''def {name}(value, count):
    produced = _true()
    alias = produced
    total = 0
    for _i in range(count):
        total += _choose(alias, value)
    return total
'''
    else:
        body = f'''def {name}(value, count):
    produced = _true()
    alias = produced
    total = 0
    while count > 0:
        total += _choose(alias, value)
        alias = False
        count -= 1
    return total
'''
    exec(body, globals())
    raw = globals().pop(name)
    return inline_calls(region_dataflow=True)(raw)


def build_dynamic_loop(nested: bool):
    name = f"loop_dynamic_{next(_COUNTER)}"
    if not nested:
        body = f'''def {name}(value, count):
    produced = _identity(value)
    total = 0
    while count > 0:
        alias = produced
        total += alias * 3 + 1
        count -= 1
    return total
'''
    else:
        body = f'''def {name}(value, outer, inner):
    produced = _identity(value)
    total = 0
    while outer > 0:
        produced = _identity(produced + 1)
        remaining = inner
        while remaining > 0:
            alias = produced
            total += alias
            remaining -= 1
        outer -= 1
    return total, produced
'''
    exec(body, globals())
    raw = globals().pop(name)
    return inline_calls(policy="always", region_dataflow=True)(raw)


def generated_constant_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0x1300C0DE)
    calls = 0
    for index in range(functions):
        kind = index % 4
        fn = build_constant_loop(kind)
        for _ in range(calls_per_function):
            value = rng.randrange(-10_000, 10_000)
            count = rng.randrange(0, 12)
            if kind < 3:
                expected = count * (value + 7)
            elif count == 0:
                expected = 0
            else:
                expected = (value + 7) + (count - 1) * (value - 11)
            actual = fn(value, count)
            if actual != expected:
                raise AssertionError((index, kind, value, count, expected, actual))
            calls += 1
    print(f"generated loop constant differential: {calls:,} calls passed")


def generated_dynamic_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xD17A100F)
    calls = 0
    for index in range(functions):
        nested = bool(index & 1)
        fn = build_dynamic_loop(nested)
        for _ in range(calls_per_function):
            value = rng.randrange(-10_000, 10_000)
            if not nested:
                count = rng.randrange(0, 12)
                expected = count * (value * 3 + 1)
                actual = fn(value, count)
                context = (index, value, count)
            else:
                outer = rng.randrange(0, 7)
                inner = rng.randrange(0, 7)
                expected_value = value + outer
                expected_total = sum((value + step) * inner for step in range(1, outer + 1))
                expected = (expected_total, expected_value)
                actual = fn(value, outer, inner)
                context = (index, value, outer, inner)
            if actual != expected:
                raise AssertionError((*context, expected, actual))
            calls += 1
    print(f"generated loop dynamic differential: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn = build_constant_loop(1)
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-100_000, 100_000)
            count = rng.randrange(0, 20)
            actual = fn(value, count)
            expected = count * (value + 7)
            if actual != expected:
                failures.append((value, count, expected, actual))
                return

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded loop dataflow: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    child = r'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def flag(): return True
@inline_function(register_only=True)
def choose(flag, value):
    if flag: return value + 7
    return value - 11
@inline_calls(region_dataflow=True)
def function(value, count):
    produced = flag()
    alias = produced
    total = 0
    while count > 0:
        count -= 1
        if count & 1:
            total += choose(alias, value)
            continue
        total += choose(alias, value)
    return total
for i in range(ROUNDS):
    value = (i % 2003) - 1000
    count = i % 9
    expected = count * (value + 7)
    if function(value, count) != expected:
        raise SystemExit(2)
print('child-ok')
'''.replace("ROUNDS", str(rounds))
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", child],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or "child-ok" not in proc.stdout:
        raise AssertionError((proc.returncode, proc.stdout, proc.stderr))
    print(f"crash-isolated loop dataflow: {rounds:,} calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if args.full:
        generated_constant_differential(300, 1_000)
        generated_dynamic_differential(200, 1_000)
        threaded_stress(8, 100_000)
        crash_isolated(2_000_000)
    else:
        generated_constant_differential(60, 250)
        generated_dynamic_differential(40, 250)
        threaded_stress(8, 10_000)
        crash_isolated(250_000)


if __name__ == "__main__":
    main()
