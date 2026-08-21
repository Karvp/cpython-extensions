from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def right_use(x):
    temp = x + 1
    return temp * 2 + temp


@inline_function(register_only=True)
def deep_use(x):
    temp = x + 1
    return (temp + 2) * (temp + 3) + temp


@inline_function(register_only=True)
def builtin_call_use(x):
    temp = x + 1
    return abs(temp) + temp


@inline_function(register_only=True)
def subscript_use(x):
    temp = x + 1
    return {temp: x}[temp]


@inline_calls(policy="always", shared_regions=False)
def optimized(x):
    return right_use(x), deep_use(x), builtin_call_use(x), subscript_use(x)


def baseline(x):
    return right_use(x), deep_use(x), builtin_call_use(x), subscript_use(x)


def randomized(rounds: int) -> None:
    rng = random.Random(0x57AC_913)
    for index in range(rounds):
        value = rng.randrange(-100_000, 100_001)
        expected = baseline(value)
        actual = optimized(value)
        if actual != expected:
            raise AssertionError((index, value, expected, actual))
    print(f"stack-resident randomized differential: {rounds:,} rounds passed")


def threaded(threads: int, rounds: int) -> None:
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed ^ 0x5A17)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-10_000, 10_001)
            if optimized(value) != baseline(value):
                failures.append((seed, value))
                return

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"stack-resident threaded differential: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    program = f'''
from python_extensions import inline_calls, inline_function, verify_code
@inline_function(register_only=True)
def helper(x):
    t=x+1
    return (t+2)*(t+3)+t
@inline_calls(policy="always", shared_regions=False)
def f(x):
    return helper(x)
verify_code(f.__code__)
assert f.__inline_stats__.stack_resident_values >= 1
for i in range({rounds}):
    x=i%2001-1000
    t=x+1
    expected=(t+2)*(t+3)+t
    if f(x)!=expected:
        raise SystemExit(11)
'''
    subprocess.run([sys.executable, "-X", "faulthandler", "-c", program], check=True)
    print(f"stack-resident crash-isolated calls: {rounds:,} passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        randomized(100_000)
        threaded(8, 10_000)
        crash_isolated(500_000)
    else:
        randomized(1_000_000)
        threaded(8, 100_000)
        crash_isolated(2_000_000)
    assert optimized.__inline_stats__.stack_resident_values >= 3
    assert verify_code(optimized.__code__).valid
    print("inline stack-resident v9 harness passed")


if __name__ == "__main__":
    main()
