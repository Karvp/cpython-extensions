from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


def build_case(index: int, tail_uses: int = 4):
    ns = {"inline_calls": inline_calls, "inline_function": inline_function}
    # a is deliberately more attractive for whole-lifetime residency, while b
    # crosses a and then survives into two later zero-stack statements.
    tail = " + ".join("b" for _ in range(max(1, tail_uses - 2)))
    # Keep ``a`` more profitable for whole-lifetime residency than ``b`` even as
    # the suffix length varies, so density scheduling consistently creates a split.
    a_terms = " + ".join(f"a*{factor}" for factor in range(2, tail_uses + 6))
    source = f'''\ndef helper_{index}(x):\n    a = x + 1\n    b = x + 2\n    y = {a_terms} + b*11 + a\n    z = b*4 + b\n    return y + z + b{(' + ' + tail) if tail else ''}\n'''
    exec(source, ns)
    helper = inline_function(register_only=True)(ns[f"helper_{index}"])
    ns["helper"] = helper

    def make(name: str, strategy: str):
        exec(f"def {name}(x):\n    return helper(x)\n", ns)
        fn = inline_calls(
            policy="speed", stack_strategy=strategy, shared_regions=False
        )(ns[name])
        assert verify_code(fn.__code__).valid
        return fn

    return helper, make(f"speed_{index}", "speed"), make(f"density_{index}", "density"), make(f"off_{index}", "off")


def generated_differential(functions: int, rounds: int) -> None:
    rng = random.Random(0x81251EED)
    executions = 0
    split_count = 0
    for index in range(functions):
        tail_uses = rng.randint(3, 8)
        helper, speed, density, off = build_case(index, tail_uses)
        for _ in range(rounds):
            value = rng.randint(-100_000, 100_000)
            expected = helper(value)
            assert speed(value) == expected
            assert density(value) == expected
            assert off(value) == expected
            executions += 3
        if density.__inline_stats__.stack_split_values:
            split_count += 1
            assert density.__inline_stats__.stack_split_instruction_cost >= 1
            assert density.__inline_stats__.stack_split_reads >= 3
        assert speed.__inline_stats__.stack_split_values == 0
    if split_count != functions:
        raise AssertionError((split_count, functions))
    print(f"generated live-range split differential: {functions} functions / {executions:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    helper, speed, density, off = build_case(999_999, 7)
    failures: list[object] = []
    barrier = threading.Barrier(threads)

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randint(-10_000, 10_000)
            expected = helper(value)
            for fn in (speed, density, off):
                actual = fn(value)
                if actual != expected:
                    failures.append((seed, value, expected, actual))
                    return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded live-range split: {threads * rounds * 3:,} transformed calls passed")


def crash_isolated(rounds: int) -> None:
    source = r'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def helper(x):
    a=x+1
    b=x+2
    y=a*2+a*3+a*4+a*5+a*6+a*7+a*8+a*9+a*10+b*11+a
    z=b*4+b
    return y+z+b+b+b+b
@inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
def dense(x): return helper(x)
@inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
def speed(x): return helper(x)
if dense.__inline_stats__.stack_split_values != 1:
    raise SystemExit(8)
for i in range(ROUNDS):
    x=(i & 2047)-1024
    expected=helper(x)
    if dense(x) != expected or speed(x) != expected:
        raise SystemExit(7)
print("ok")
'''.replace("ROUNDS", str(rounds))
    env = dict(os.environ)
    root = os.path.dirname(os.path.dirname(__file__))
    env["PYTHONPATH"] = os.path.join(root, "src")
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", source],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "ok":
        raise AssertionError((proc.returncode, proc.stdout, proc.stderr))
    print(f"crash-isolated live-range split: {rounds * 2:,} transformed calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--functions", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=500)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--thread-rounds", type=int, default=50_000)
    parser.add_argument("--subprocess-rounds", type=int, default=1_000_000)
    args = parser.parse_args()
    generated_differential(args.functions, args.rounds)
    threaded_stress(args.threads, args.thread_rounds)
    crash_isolated(args.subprocess_rounds)


if __name__ == "__main__":
    main()
