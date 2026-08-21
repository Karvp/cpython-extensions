from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading
import itertools

from python_extensions import inline_calls, inline_function

_BUILD_COUNTER = itertools.count()


@inline_function(register_only=True)
def _true():
    return True


@inline_function(register_only=True)
def _false():
    return False


@inline_function(register_only=True)
def _choose(flag, value):
    if flag:
        return value + 7
    return value - 11


@inline_function(register_only=True)
def _identity(value):
    return value


def build_constant_chain(hops: int, truth: bool):
    name = f"generated_const_{next(_BUILD_COUNTER)}"
    lines = [f"def {name}(value):", f"    root = {'_true' if truth else '_false'}()"]
    previous = "root"
    for index in range(hops):
        current = f"copy_{index}"
        lines.append(f"    {current} = {previous}")
        previous = current
    lines.append(f"    return _choose({previous}, value)")
    exec("\n".join(lines), globals())
    raw = globals().pop(name)
    return inline_calls(region_dataflow=True)(raw)


def build_dynamic_chain(hops: int):
    name = f"generated_dynamic_{next(_BUILD_COUNTER)}"
    lines = [f"def {name}(value):", "    root = _identity(value)"]
    previous = "root"
    for index in range(hops):
        current = f"copy_{index}"
        lines.append(f"    {current} = {previous}")
        previous = current
    lines.append(f"    return _identity({previous})")
    exec("\n".join(lines), globals())
    raw = globals().pop(name)
    return inline_calls(region_dataflow=True)(raw)


def generated_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0x11DA7A)
    calls = 0
    for index in range(functions):
        truth = bool(index & 1)
        hops = index % 7
        fn = build_constant_chain(hops, truth)
        for _ in range(calls_per_function):
            value = rng.randrange(-100_000, 100_000)
            expected = value + 7 if truth else value - 11
            actual = fn(value)
            if actual != expected:
                raise AssertionError((index, hops, truth, value, expected, actual))
            calls += 1
    print(f"generated region differential: {calls:,} calls passed")


def dynamic_copy_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xC0FFEE11)
    calls = 0
    for index in range(functions):
        fn = build_dynamic_chain(index % 8)
        for _ in range(calls_per_function):
            value = rng.randrange(-1_000_000, 1_000_000)
            actual = fn(value)
            if actual != value:
                raise AssertionError((index, value, actual))
            calls += 1
    print(f"dynamic copy differential: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn = build_constant_chain(6, True)
    barrier = threading.Barrier(threads)
    failures: list[tuple[int, int]] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-100_000, 100_000)
            actual = fn(value)
            expected = value + 7
            if actual != expected:
                failures.append((expected, actual))
                return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded region dataflow: {threads * rounds:,} calls passed")


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
def function(value):
    a = flag(); b = a; c = b; d = c; return choose(d, value)
for i in range(ROUNDS):
    value = (i % 1009) - 500
    if function(value) != value + 7:
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
    print(f"crash-isolated region dataflow: {rounds:,} calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if args.full:
        generated_differential(300, 1_000)
        dynamic_copy_differential(200, 1_000)
        threaded_stress(8, 100_000)
        crash_isolated(2_000_000)
    else:
        generated_differential(60, 300)
        dynamic_copy_differential(40, 300)
        threaded_stress(8, 10_000)
        crash_isolated(250_000)


if __name__ == "__main__":
    main()
