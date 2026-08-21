from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function, verify_code


def build_middle_case(index: int):
    ns = {"inline_calls": inline_calls, "inline_function": inline_function}
    k = index % 17 + 1
    source = f'''
def helper_{index}(x):
    a = x + {k}
    b = x + {k + 1}
    u = a * 2 + a * 3 + 4 * a
    m1 = b * 5
    d = x * 11
    d + 1
    abs(d)
    m2 = b * 6
    c = x + {k + 2}
    n = b * 7 + c * 8 + 1 * b
    z = c * 9 + c * 10 + 1 * c
    return u + m1 + m2 + n + z
'''
    exec(source, ns)
    helper = inline_function(register_only=True)(ns[f"helper_{index}"])
    ns["helper"] = helper
    exec("def dense(x):\n    return helper(x)\n", ns)
    dense = inline_calls(
        policy="speed", stack_strategy="density", shared_regions=False
    )(ns["dense"])
    exec("def speed(x):\n    return helper(x)\n", ns)
    speed = inline_calls(
        policy="speed", stack_strategy="speed", shared_regions=False
    )(ns["speed"])
    assert dense.__inline_stats__.stack_middle_splits == 1
    assert verify_code(dense.__code__).valid
    assert verify_code(speed.__code__).valid
    return helper, dense, speed


def build_deep_case(index: int):
    ns = {"inline_calls": inline_calls, "inline_function": inline_function}
    k = index % 13 + 1
    source = f'''
def helper_deep_{index}(x):
    a = x + {k}
    out = (x, x + 2, x + 3, a * 2 + 3 * a)
    return out
'''
    exec(source, ns)
    helper = inline_function(register_only=True)(ns[f"helper_deep_{index}"])
    ns["helper"] = helper
    exec("def dense(x):\n    return helper(x)\n", ns)
    dense = inline_calls(
        policy="always", stack_strategy="density", shared_regions=False
    )(ns["dense"])
    assert dense.__inline_stats__.stack_resident_values >= 1
    assert verify_code(dense.__code__).valid
    return helper, dense


def generated_differential(functions: int, rounds: int) -> None:
    rng = random.Random(0x9130A11C)
    calls = 0
    for index in range(functions):
        helper, dense, speed = build_middle_case(index)
        deep_helper, deep = build_deep_case(index)
        for _ in range(rounds):
            value = rng.randint(-100_000, 100_000)
            expected = helper(value)
            if dense(value) != expected or speed(value) != expected:
                raise AssertionError((index, value, expected, dense(value), speed(value)))
            deep_expected = deep_helper(value)
            if deep(value) != deep_expected:
                raise AssertionError((index, value, deep_expected, deep(value)))
            calls += 3
    print(f"segmented-lifetime generated differential: {functions} functions / {calls:,} transformed calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    helper, dense, speed = build_middle_case(999_991)
    deep_helper, deep = build_deep_case(999_991)
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed ^ 0x9130)
        barrier.wait()
        for _ in range(rounds):
            value = rng.randint(-50_000, 50_000)
            expected = helper(value)
            if dense(value) != expected or speed(value) != expected:
                failures.append((seed, value, expected, dense(value), speed(value)))
                return
            if deep(value) != deep_helper(value):
                failures.append((seed, value, "deep"))
                return

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"segmented-lifetime threaded stress: {threads * rounds * 3:,} transformed calls passed")


def crash_isolated(rounds: int) -> None:
    source = r'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def helper(x):
    a=x+1
    b=x+2
    u=a*2+a*3+4*a
    m1=b*5
    d=x*11
    d+1
    abs(d)
    m2=b*6
    c=x+3
    n=b*7+c*8+1*b
    z=c*9+c*10+1*c
    return u+m1+m2+n+z
@inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
def dense(x): return helper(x)
@inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
def speed(x): return helper(x)
if dense.__inline_stats__.stack_middle_splits != 1:
    raise SystemExit(8)
for i in range(ROUNDS):
    x=(i & 4095)-2048
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
    print(f"segmented-lifetime crash-isolated: {rounds * 2:,} transformed calls passed")


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
