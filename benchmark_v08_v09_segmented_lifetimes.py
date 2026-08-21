from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

CHILD = r'''
import json, statistics, timeit
try:
    import os
    os.sched_setaffinity(0, {0})
except Exception:
    pass
from python_extensions import inline_calls, inline_function

@inline_function(register_only=True)
def speed_helper(x):
    temp = x + 1
    return temp * 2 + temp

@inline_function(register_only=True)
def middle_helper(x):
    a = x + 1
    b = x + 2
    a * 2
    a * 3
    4 * a
    b * 5
    d = x * 11
    d + 1
    abs(d)
    b * 6
    c = x + 3
    b * 7 + c * 8 + 1 * b
    c * 9
    c * 10
    1 * c
    return x

@inline_function(register_only=True)
def deep_helper(x):
    a = x + 1
    out = (x, x + 2, a * 2 + 3 * a)
    return out

@inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
def speed(x): return speed_helper(x)
@inline_calls(policy="always", stack_strategy="density", shared_regions=False)
def middle(x): return middle_helper(x)
@inline_calls(policy="always", stack_strategy="density", shared_regions=False)
def deep(x): return deep_helper(x)

out = {}
for name, fn in (("speed", speed), ("middle", middle), ("deep", deep)):
    for _ in range(20000): fn(123)
    samples = [t * 1e9 / 400000 for t in timeit.repeat(lambda f=fn: f(123), number=400000, repeat=9)]
    out[name] = {
        "ns": statistics.median(samples),
        "code": len(fn.__code__.co_code),
        "locals": fn.__code__.co_nlocals,
        "stats": repr(fn.__inline_stats__),
    }
print(json.dumps(out))
'''


def measure(src: Path, processes: int) -> dict[str, dict[str, float | int | str]]:
    rows: list[dict[str, dict[str, float | int | str]]] = []
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    for _ in range(processes):
        proc = subprocess.run(
            [sys.executable, "-c", CHILD],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        rows.append(json.loads(proc.stdout))
    result = {}
    for name in rows[0]:
        result[name] = {
            "ns": statistics.median(float(row[name]["ns"]) for row in rows),
            "code": int(rows[0][name]["code"]),
            "locals": int(rows[0][name]["locals"]),
            "stats": rows[0][name]["stats"],
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-src", required=True, type=Path)
    parser.add_argument("--current-src", required=True, type=Path)
    parser.add_argument("--processes", type=int, default=5)
    args = parser.parse_args()
    old = measure(args.baseline_src, args.processes)
    new = measure(args.current_src, args.processes)
    print(sys.version)
    print(f"{'case':<12} {'v0.8 ns':>10} {'v0.9 ns':>10} {'ratio':>8} {'v0.8 bytes':>11} {'v0.9 bytes':>11} {'locals':>10}")
    for name in ("speed", "middle", "deep"):
        ratio = old[name]["ns"] / new[name]["ns"]
        print(
            f"{name:<12} {old[name]['ns']:10.2f} {new[name]['ns']:10.2f} {ratio:8.3f} "
            f"{old[name]['code']:11d} {new[name]['code']:11d} "
            f"{old[name]['locals']} -> {new[name]['locals']}"
        )


if __name__ == "__main__":
    main()
