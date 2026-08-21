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


def build_constant_merge(depth: int, same: bool):
    name = f"cfg_const_{next(_COUNTER)}"
    lines = [f"def {name}(value, bits):", "    root = _true()"]
    prev = "root"
    for level in range(depth):
        alias = f"alias_{level}"
        bit = f"bool(bits & {1 << level})"
        lines += [
            f"    if {bit}:",
            f"        {alias} = {prev}",
            "    else:",
            f"        {alias} = {prev if same or level + 1 < depth else 'False'}",
        ]
        prev = alias
    lines.append(f"    return _choose({prev}, value)")
    exec("\n".join(lines), globals())
    raw = globals().pop(name)
    return inline_calls(region_dataflow=True)(raw)


def build_dynamic_merge(depth: int):
    name = f"cfg_dynamic_{next(_COUNTER)}"
    lines = [f"def {name}(value, bits):", "    root = _identity(value)"]
    prev = "root"
    for level in range(depth):
        alias = f"alias_{level}"
        bit = f"bool(bits & {1 << level})"
        lines += [
            f"    if {bit}:",
            f"        {alias} = {prev}",
            "    else:",
            f"        {alias} = {prev}",
        ]
        prev = alias
    lines.append(f"    return _identity({prev})")
    exec("\n".join(lines), globals())
    raw = globals().pop(name)
    return inline_calls(policy="always", region_dataflow=True)(raw)


def generated_constant_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xC6F012)
    calls = 0
    for index in range(functions):
        depth = 1 + index % 5
        same = bool(index & 1)
        fn = build_constant_merge(depth, same)
        mask = (1 << depth) - 1
        for _ in range(calls_per_function):
            value = rng.randrange(-100_000, 100_000)
            bits = rng.randrange(mask + 1)
            expected_flag = True if same else bool(bits & (1 << (depth - 1)))
            expected = value + 7 if expected_flag else value - 11
            actual = fn(value, bits)
            if actual != expected:
                raise AssertionError((index, depth, same, value, bits, expected, actual))
            calls += 1
    print(f"generated CFG constant differential: {calls:,} calls passed")


def generated_dynamic_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xD1A6CF6)
    calls = 0
    for index in range(functions):
        depth = 1 + index % 6
        fn = build_dynamic_merge(depth)
        mask = (1 << depth) - 1
        for _ in range(calls_per_function):
            value = rng.randrange(-1_000_000, 1_000_000)
            bits = rng.randrange(mask + 1)
            actual = fn(value, bits)
            if actual != value:
                raise AssertionError((index, depth, value, bits, actual))
            calls += 1
    print(f"generated CFG dynamic differential: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn = build_constant_merge(5, True)
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-100_000, 100_000)
            bits = rng.randrange(32)
            actual = fn(value, bits)
            if actual != value + 7:
                failures.append((value, bits, actual))
                return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded CFG dataflow: {threads * rounds:,} calls passed")


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
def function(value, b1, b2):
    root = flag()
    if b1: a = root
    else: a = root
    if b2: b = a
    else: b = a
    return choose(b, value)
for i in range(ROUNDS):
    value = (i % 2003) - 1000
    if function(value, bool(i & 1), bool(i & 2)) != value + 7:
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
    print(f"crash-isolated CFG dataflow: {rounds:,} calls passed")


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
        generated_constant_differential(60, 300)
        generated_dynamic_differential(40, 300)
        threaded_stress(8, 10_000)
        crash_isolated(250_000)


if __name__ == "__main__":
    main()
