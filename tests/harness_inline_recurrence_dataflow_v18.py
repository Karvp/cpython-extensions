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
def _even(i, value):
    if i % 2 == 0:
        return value + 7
    return value - 11


@inline_function(register_only=True)
def _low(i, value):
    if (i & 3) == 3:
        return value + 5
    return value - 9


@inline_function(register_only=True)
def _nonnegative(i, value):
    if i >= 0:
        return value + 1
    return value - 1


def build_affine(kind: int):
    name = f"recurrence_affine_{next(_COUNTER)}"
    if kind == 0:
        source = f'''def {name}(value, count):
    i = 0
    total = 0
    while count > 0:
        total += _even(i, value)
        i += 2
        count -= 1
    return total
'''
        expected = lambda value, count: count * (value + 7)
    elif kind == 1:
        source = f'''def {name}(value, count):
    i = 1
    total = 0
    while count > 0:
        total += _even(i, value)
        i += 2
        count -= 1
    return total
'''
        expected = lambda value, count: count * (value - 11)
    elif kind == 2:
        source = f'''def {name}(value, count):
    i = 3
    total = 0
    while count > 0:
        total += _low(i, value)
        i += 4
        count -= 1
    return total
'''
        expected = lambda value, count: count * (value + 5)
    else:
        source = f'''def {name}(value, count):
    i = 0
    total = 0
    while count > 0:
        total += _nonnegative(i, value)
        i += 3
        count -= 1
    return total
'''
        expected = lambda value, count: count * (value + 1)
    namespace = globals()
    exec(source, namespace)
    raw = namespace.pop(name)
    return inline_calls(region_dataflow=True)(raw), expected


def build_dynamic():
    name = f"recurrence_dynamic_{next(_COUNTER)}"
    source = f'''def {name}(value, count, step):
    i = 0
    total = 0
    while count > 0:
        total += _even(i, value)
        i += step
        count -= 1
    return total
'''
    namespace = globals()
    exec(source, namespace)
    raw = namespace.pop(name)
    return inline_calls(region_dataflow=True)(raw)


def generated_affine_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xAFF114E)
    calls = 0
    for index in range(functions):
        fn, expected_fn = build_affine(index % 4)
        for _ in range(calls_per_function):
            value = rng.randrange(-100_000, 100_000)
            count = rng.randrange(0, 25)
            expected = expected_fn(value, count)
            actual = fn(value, count)
            if actual != expected:
                raise AssertionError((index, value, count, expected, actual))
            calls += 1
        if fn.__inline_stats__.cfg_affine_recurrences < 1:
            raise AssertionError("affine recurrence was not discovered")
    print(f"generated affine recurrence differential: {calls:,} calls passed")


def generated_dynamic_control(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xD1A5E7)
    calls = 0
    for _ in range(functions):
        fn = build_dynamic()
        for _ in range(calls_per_function):
            value = rng.randrange(-10_000, 10_000)
            count = rng.randrange(0, 16)
            step = rng.choice((-3, -2, -1, 1, 2, 3))
            i = 0
            expected = 0
            for _iteration in range(count):
                expected += value + 7 if i % 2 == 0 else value - 11
                i += step
            actual = fn(value, count, step)
            if actual != expected:
                raise AssertionError((value, count, step, expected, actual))
            calls += 1
        if fn.__inline_stats__.cfg_affine_recurrences != 0:
            raise AssertionError("dynamic step was incorrectly promoted")
    print(f"generated dynamic-step control: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn, expected_fn = build_affine(0)
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-100_000, 100_000)
            count = rng.randrange(0, 30)
            expected = expected_fn(value, count)
            actual = fn(value, count)
            if actual != expected:
                failures.append((value, count, expected, actual))
                return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded affine recurrence: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    program = r'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def even(i, value):
    if i % 2 == 0:
        return value + 7
    return value - 11
@inline_calls(region_dataflow=True)
def run(value, count):
    i = 0
    total = 0
    while count > 0:
        total += even(i, value)
        i += 2
        count -= 1
    return total
rounds = ROUND_COUNT
checksum = 0
for index in range(rounds):
    value = (index % 97) - 48
    count = index % 13
    actual = run(value, count)
    expected = count * (value + 7)
    if actual != expected:
        raise AssertionError((index, actual, expected))
    checksum ^= actual
print(checksum)
'''.replace("ROUND_COUNT", str(rounds))
    completed = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", program],
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    print(f"crash-isolated affine recurrence: {rounds:,} calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        generated_affine_differential(40, 250)
        generated_dynamic_control(20, 200)
        threaded_stress(4, 5_000)
        crash_isolated(100_000)
    else:
        generated_affine_differential(300, 1_000)
        generated_dynamic_control(200, 1_000)
        threaded_stress(8, 100_000)
        crash_isolated(2_000_000)


if __name__ == "__main__":
    main()
