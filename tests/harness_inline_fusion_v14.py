from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading

from python_extensions.inline import inline_calls, inline_function


@inline_function(register_only=True)
def inc(x):
    return x + 1

@inline_function(register_only=True)
def mix(x):
    return x * 3 - 7

@inline_function(register_only=True)
def flag_true():
    return True

@inline_function(register_only=True)
def branch(flag, x):
    if flag:
        return x + 11
    return x - 13

@inline_calls(fusion_strategy="safe")
def safe_pipeline(x):
    a = inc(x)
    b = mix(a)
    return b ^ 0x55, a, b

@inline_calls(fusion_strategy="aggressive")
def aggressive_pipeline(x):
    a = inc(x)
    b = mix(a)
    return b ^ 0x55

@inline_calls(fusion_strategy="safe")
def constant_pipeline(x):
    flag = flag_true()
    return branch(flag, x), locals().get("flag")


def randomized(rounds: int) -> None:
    rng = random.Random(0xF0510)
    for i in range(rounds):
        x = rng.randrange(-10_000_000, 10_000_000)
        a = x + 1
        b = a * 3 - 7
        if safe_pipeline(x) != (b ^ 0x55, a, b):
            raise AssertionError((i, x, safe_pipeline(x)))
        if aggressive_pipeline(x) != (b ^ 0x55):
            raise AssertionError((i, x, aggressive_pipeline(x)))
        if constant_pipeline(x) != (x + 11, True):
            raise AssertionError((i, x, constant_pipeline(x)))
    print(f"randomized fusion: {rounds:,} rounds passed")


def threaded(threads: int, rounds: int) -> None:
    barrier = threading.Barrier(threads)
    failures: list[object] = []
    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            x = rng.randrange(-1_000_000, 1_000_000)
            a = x + 1
            b = a * 3 - 7
            if aggressive_pipeline(x) != (b ^ 0x55):
                failures.append((seed, x))
                return
    ws = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for w in ws: w.start()
    for w in ws: w.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded fusion: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    code = f'''
import sys
sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'src')!r})
from python_extensions.inline import inline_calls, inline_function
@inline_function(register_only=True)
def a(x): return x + 1
@inline_function(register_only=True)
def b(x): return x * 3 - 7
@inline_calls(fusion_strategy="aggressive")
def f(x):
    y = a(x)
    z = b(y)
    return z ^ 85
for i in range({rounds}):
    x = (i % 10007) - 5003
    expected = (((x + 1) * 3 - 7) ^ 85)
    if f(x) != expected:
        raise AssertionError((i, x, f(x), expected))
print("child-ok")
'''
    proc = subprocess.run([sys.executable, "-X", "faulthandler", "-c", code], text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout + proc.stderr)
    if "child-ok" not in proc.stdout:
        raise RuntimeError(proc.stdout + proc.stderr)
    print(f"crash-isolated fusion: {rounds:,} calls passed")


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument('--quick', action='store_true')
    ns=ap.parse_args()
    if ns.quick:
        randomized(100_000); threaded(8,10_000); crash_isolated(500_000)
    else:
        randomized(1_000_000); threaded(8,100_000); crash_isolated(2_000_000)

if __name__ == '__main__': main()
