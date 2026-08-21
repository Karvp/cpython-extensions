from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def ephemeral(x):
    a = x
    b = a + 1
    c = b * 2
    return c


@inline_function(register_only=True)
def copy_value(x):
    alias = x
    return alias * alias + alias


@inline_function(register_only=True)
def constant_path(x):
    enabled = True
    if enabled:
        return x * 3 + 1
    return -x


@inline_function(register_only=True)
def slot_a(x):
    a = x + 1
    return a * a + a


@inline_function(register_only=True)
def slot_b(x):
    b = x * 2
    return b * b + b


@inline_calls(policy="always", shared_regions=False)
def optimized(x):
    return ephemeral(x), copy_value(x), constant_path(x), slot_b(slot_a(x))


def baseline(x):
    return ephemeral(x), copy_value(x), constant_path(x), slot_b(slot_a(x))


@inline_function(register_only=True)
def maybe_unbound(flag):
    if flag:
        temp = 11
    return temp


@inline_calls(policy="always", shared_regions=False)
def unbound(flag):
    return maybe_unbound(flag)


def randomized(rounds: int) -> None:
    rng = random.Random(0xD4A7AF10)
    for index in range(rounds):
        value = rng.randrange(-10_000, 10_001)
        expected = baseline(value)
        actual = optimized(value)
        if actual != expected:
            raise AssertionError((index, value, expected, actual))
    print(f"dataflow randomized differential: {rounds:,} rounds passed")


def threaded(threads: int, rounds: int) -> None:
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed ^ 0x51A)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-2000, 2001)
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
    print(f"dataflow threaded differential: {threads * rounds:,} calls passed")


def checked_unbound(rounds: int) -> None:
    for _ in range(rounds):
        if unbound(True) != 11:
            raise AssertionError("bound result mismatch")
        try:
            unbound(False)
        except UnboundLocalError:
            pass
        else:
            raise AssertionError("checked local was incorrectly coalesced/propagated")
    print(f"dataflow checked-unbound semantics: {rounds:,} rounds passed")


def crash_isolated(rounds: int) -> None:
    program = f'''
from python_extensions import inline_calls, inline_function, verify_code
@inline_function(register_only=True)
def a(x):
    t=x
    u=t+1
    return u*2
@inline_function(register_only=True)
def b(x):
    t=x+3
    return t*t
@inline_calls(policy="always", shared_regions=False)
def f(x):
    return b(a(x))
verify_code(f.__code__)
for i in range({rounds}):
    x=i%997-498
    expected=((x+1)*2+3)**2
    if f(x)!=expected:
        raise SystemExit(9)
'''
    subprocess.run([sys.executable, "-X", "faulthandler", "-c", program], check=True)
    print(f"dataflow crash-isolated calls: {rounds:,} passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        randomized(100_000)
        threaded(8, 10_000)
        checked_unbound(10_000)
        crash_isolated(500_000)
    else:
        randomized(1_000_000)
        threaded(8, 100_000)
        checked_unbound(100_000)
        crash_isolated(2_000_000)
    assert verify_code(optimized.__code__).valid
    assert verify_code(unbound.__code__).valid
    print("inline dataflow v8 harness passed")


if __name__ == "__main__":
    main()
