from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True)
def nested2(x):
    a = x + 1
    b = x + 2
    return a * 3 + b * 4 + b + a


@inline_function(register_only=True)
def crossing2(x):
    a = x + 1
    b = x + 2
    return a * 3 + b * 4 + a + b


@inline_function(register_only=True)
def nested3(x):
    a = x + 1; b = x + 2; c = x + 3
    return a * 2 + b * 3 + c * 4 + c + b + a


@inline_calls(policy="always", shared_regions=False)
def optimized(x):
    return nested2(x), crossing2(x), nested3(x)


def baseline(x):
    return nested2(x), crossing2(x), nested3(x)


def randomized(rounds: int) -> None:
    rng = random.Random(0x601F_10)
    for index in range(rounds):
        value = rng.randrange(-100_000, 100_001)
        expected = baseline(value)
        actual = optimized(value)
        if actual != expected:
            raise AssertionError((index, value, expected, actual))
    print(f"multi-stack randomized differential: {rounds:,} rounds passed")


def threaded(threads: int, rounds: int) -> None:
    failures: list[object] = []
    barrier = threading.Barrier(threads)

    def worker(seed: int) -> None:
        rng = random.Random(seed ^ 0x601F)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randrange(-50_000, 50_001)
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
    print(f"multi-stack threaded differential: {threads * rounds:,} calls passed")


def generated_fuzzer(functions: int, rounds: int) -> None:
    rng = random.Random(0xA110_C601)
    for function_index in range(functions):
        count = rng.randrange(2, 6)
        order = list(range(count))
        rng.shuffle(order)
        lines = ["def helper(x):"]
        for i in range(count):
            lines.append(f"    t{i} = x + {i + 1}")
        weighted = " + ".join(f"t{i} * {i + 2}" for i in range(count))
        tail = " + ".join(f"t{i}" for i in order)
        lines.append(f"    return {weighted} + {tail}")
        lines += ["", "def caller(x):", "    return helper(x)"]
        namespace: dict[str, object] = {}
        exec("\n".join(lines), namespace)
        helper = inline_function(register_only=True)(namespace["helper"])
        namespace["helper"] = helper
        caller = inline_calls(policy="always", shared_regions=False)(namespace["caller"])
        for _ in range(rounds):
            value = rng.randrange(-10_000, 10_001)
            expected = helper(value)
            actual = caller(value)
            if actual != expected:
                raise AssertionError((function_index, count, order, value, expected, actual))
        verify_code(caller.__code__)
    print(f"multi-stack generated fuzzer: {functions:,} functions / {functions * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    program = f'''\nfrom python_extensions import inline_calls, inline_function, verify_code\n@inline_function(register_only=True)\ndef h(x):\n    a=x+1; b=x+2; c=x+3\n    return a*2+b*3+c*4+c+b+a\n@inline_calls(policy="always", shared_regions=False)\ndef f(x):\n    return h(x)\nverify_code(f.__code__)\nassert f.__inline_stats__.stack_resident_values >= 3\nfor i in range({rounds}):\n    x=i%4001-2000\n    a=x+1; b=x+2; c=x+3\n    expected=a*2+b*3+c*4+c+b+a\n    if f(x) != expected:\n        raise SystemExit(17)\n'''
    subprocess.run([sys.executable, "-X", "faulthandler", "-c", program], check=True)
    print(f"multi-stack crash-isolated calls: {rounds:,} passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        randomized(100_000)
        threaded(8, 10_000)
        generated_fuzzer(50, 100)
        crash_isolated(500_000)
    else:
        randomized(1_000_000)
        threaded(8, 100_000)
        generated_fuzzer(300, 1_000)
        crash_isolated(2_000_000)
    assert optimized.__inline_stats__.stack_resident_values >= 4
    assert verify_code(optimized.__code__).valid
    print("inline multi-stack v10 harness passed")


if __name__ == "__main__":
    main()
