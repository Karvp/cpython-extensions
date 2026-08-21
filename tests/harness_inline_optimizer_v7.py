from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def hot_formula(x, mode="fast", scale=3, bias=4):
    if mode == "fast":
        return x * x + scale * 2 + bias - 1
    return -x


@inline_calls(policy="always", shared_regions=False)
def optimized_formula(x):
    return hot_formula(x)


@inline_function(register_only=True)
def local_heavy(x):
    a = x + 1
    b = a * 3
    c = b - 7
    return c * c + a


@inline_calls(policy="always", shared_regions=False)
def optimized_chain(x):
    a = local_heavy(x)
    b = local_heavy(a)
    c = local_heavy(b)
    return local_heavy(c)


def raw_chain(x):
    a = local_heavy(x)
    b = local_heavy(a)
    c = local_heavy(b)
    return local_heavy(c)


@inline_function(register_only=True)
def maybe_unbound(flag):
    if flag:
        value = 11
    return value


@inline_calls(policy="always", shared_regions=False)
def unbound_pair(a, b):
    return maybe_unbound(a), maybe_unbound(b)


def randomized(rounds: int) -> None:
    rng = random.Random(0xA11A5)
    for index in range(rounds):
        value = rng.randrange(-10000, 10001)
        expected = hot_formula(value)
        actual = optimized_formula(value)
        if actual != expected:
            raise AssertionError((index, value, expected, actual))
        expected_chain = raw_chain(value)
        actual_chain = optimized_chain(value)
        if actual_chain != expected_chain:
            raise AssertionError((index, value, expected_chain, actual_chain))
    print(f"randomized differential: {rounds:,} rounds passed")


def threaded(threads: int, rounds: int) -> None:
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            x = rng.randrange(-1000, 1001)
            if optimized_formula(x) != hot_formula(x):
                failures.append((seed, x))
                return
            if optimized_chain(x) != raw_chain(x):
                failures.append((seed, x, "chain"))
                return

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded differential: {threads * rounds:,} calls passed")


def unbound_semantics(rounds: int) -> None:
    for _ in range(rounds):
        assert unbound_pair(True, True) == (11, 11)
        try:
            unbound_pair(True, False)
        except UnboundLocalError:
            pass
        else:
            raise AssertionError("stale synthetic local escaped local-slot reuse proof")
    print(f"unbound-local semantics: {rounds:,} rounds passed")


def crash_isolated(rounds: int) -> None:
    program = f'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def h(x, mode="fast", scale=3):
    if mode == "fast":
        return x*x + scale*2
    return -x
@inline_calls(policy="always", shared_regions=False)
def f(x):
    return h(x)
for i in range({rounds}):
    x = i % 997 - 498
    y = f(x)
    if y != h(x):
        raise SystemExit(3)
'''
    subprocess.run([sys.executable, "-X", "faulthandler", "-c", program], check=True)
    print(f"crash-isolated optimized calls: {rounds:,} passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        randomized(100_000)
        threaded(8, 10_000)
        unbound_semantics(10_000)
        crash_isolated(500_000)
    else:
        randomized(1_000_000)
        threaded(8, 100_000)
        unbound_semantics(100_000)
        crash_isolated(2_000_000)
    for func in (optimized_formula, optimized_chain, unbound_pair):
        assert verify_code(func.__code__).valid
    print("inline optimizer harness passed")


if __name__ == "__main__":
    main()
