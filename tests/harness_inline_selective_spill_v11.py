from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


def build_case(index: int, count: int, permutation: list[int]):
    ns = {"inline_calls": inline_calls, "inline_function": inline_function}
    lines = ["def helper(x):"]
    for i in range(count):
        lines.append(f"    v{i} = x + {i + 1}")
    terms = [f"v{i} * {i + 2}" for i in range(count)]
    terms.extend(f"v{i}" for i in permutation)
    lines.append("    return " + " + ".join(terms))
    exec("\n".join(lines), ns)
    helper = inline_function(register_only=True)(ns["helper"])
    ns["helper"] = helper

    def make(name: str, strategy: str):
        exec(f"def {name}(x):\n    return helper(x)\n", ns)
        fn = inline_calls(
            policy="speed", stack_strategy=strategy, shared_regions=False
        )(ns[name])
        assert verify_code(fn.__code__).valid
        return fn

    return helper, make(f"speed_{index}", "speed"), make(f"dense_{index}", "density"), make(f"off_{index}", "off")


def generated_differential(functions: int, rounds: int) -> None:
    rng = random.Random(0x7110CA7E)
    executions = 0
    for index in range(functions):
        count = rng.randint(2, 9)
        permutation = list(range(count))
        rng.shuffle(permutation)
        helper, speed, dense, off = build_case(index, count, permutation)
        for _ in range(rounds):
            value = rng.randint(-10_000, 10_000)
            expected = helper(value)
            assert speed(value) == expected
            assert dense(value) == expected
            assert off(value) == expected
            executions += 3
        dense_stats = dense.__inline_stats__
        assert dense_stats.stack_scheduler_candidates == (
            dense_stats.stack_resident_values + dense_stats.stack_spilled_values
        )
        assert dense_stats.stack_peak_resident_values <= dense_stats.stack_resident_values
    print(f"generated selective-spill differential: {functions} functions / {executions:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    helper, speed, dense, off = build_case(999_999, 7, [2, 0, 5, 3, 6, 1, 4])
    failures: list[object] = []
    barrier = threading.Barrier(threads)

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randint(-1000, 1000)
            expected = helper(value)
            for fn in (speed, dense, off):
                actual = fn(value)
                if actual != expected:
                    failures.append((seed, value, expected, actual))
                    return

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded selective-spill: {threads * rounds * 3:,} transformed calls passed")


def crash_isolated(rounds: int) -> None:
    source = r'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def helper(x):
    a=x+1; b=x+2; c=x+3; d=x+4; e=x+5; f=x+6; g=x+7; h=x+8
    return a*2+b*3+c*4+d*5+e*6+f*7+g*8+h*9+d+b+h+f+a+g+c+e
@inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
def dense(x): return helper(x)
@inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
def speed(x): return helper(x)
for i in range(ROUNDS):
    x=(i & 1023)-512
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
    print(f"crash-isolated selective-spill: {rounds * 2:,} transformed calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--functions", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=300)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--thread-rounds", type=int, default=50_000)
    parser.add_argument("--subprocess-rounds", type=int, default=1_000_000)
    args = parser.parse_args()
    generated_differential(args.functions, args.rounds)
    threaded_stress(args.threads, args.thread_rounds)
    crash_isolated(args.subprocess_rounds)


if __name__ == "__main__":
    main()
