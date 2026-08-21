from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys

WORKER = r'''
import json, timeit
from python_extensions import inline_calls, inline_function

@inline_function(register_only=True)
def affine(i):
    return i * 7 + 3

@inline_function(register_only=True)
def scaled(i):
    return i * 5

@inline_calls(region_dataflow=True)
def affine_twice(count):
    i = 0
    total = 0
    while count > 0:
        total += affine(i)
        total += affine(i)
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True)
def scaled_four(count):
    i = 1
    total = 0
    while count > 0:
        total += scaled(i)
        total += scaled(i)
        total += scaled(i)
        total += scaled(i)
        i += 3
        count -= 1
    return total

@inline_calls(region_dataflow=True)
def two_derived(count):
    i = 0
    total = 0
    while count > 0:
        total += i * 7 + 3
        total += i * 7 + 3
        total += i * 11 - 5
        total += i * 11 - 5
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True)
def single_control(count):
    i = 0
    total = 0
    while count > 0:
        total += affine(i)
        i += 2
        count -= 1
    return total

functions = {
    "affine_twice": affine_twice,
    "scaled_four": scaled_four,
    "two_derived": two_derived,
    "single_control": single_control,
}
for function in functions.values():
    for _ in range(3000):
        function(12)

output = {}
for name, function in functions.items():
    value = function(12)
    timings = timeit.repeat(lambda f=function: f(12), number=100000, repeat=5)
    stats = function.__inline_stats__
    output[name] = {
        "ns": min(timings) / 100000 * 1e9,
        "code_bytes": len(function.__code__.co_code),
        "locals": function.__code__.co_nlocals,
        "result": value,
        "strength_values": getattr(stats, "cfg_strength_reduced_values", 0),
        "strength_uses": getattr(stats, "cfg_strength_reduced_uses", 0),
    }
print(json.dumps(output))
'''


def one_process(source_path: str) -> dict[str, dict[str, float | int]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = source_path
    completed = subprocess.run(
        [sys.executable, "-c", WORKER],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout)


def collect(source_path: str, processes: int) -> dict[str, dict[str, float | int]]:
    runs = [one_process(source_path) for _ in range(processes)]
    output: dict[str, dict[str, float | int]] = {}
    for name in runs[0]:
        output[name] = dict(runs[0][name])
        output[name]["ns"] = statistics.median(run[name]["ns"] for run in runs)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-src", required=True)
    parser.add_argument("--current-src", required=True)
    parser.add_argument("--processes", type=int, default=5)
    args = parser.parse_args()

    baseline = collect(args.baseline_src, args.processes)
    current = collect(args.current_src, args.processes)
    print(f"Python: {sys.version.split()[0]}")
    print("scenario                 v0.14 ns   v0.15 ns  speedup   bytes        locals  derived/uses")
    for name in baseline:
        old = baseline[name]
        new = current[name]
        if old["result"] != new["result"]:
            raise AssertionError((name, old["result"], new["result"]))
        speedup = old["ns"] / new["ns"]
        print(
            f"{name:22s} {old['ns']:9.2f} {new['ns']:10.2f}  {speedup:7.3f}x  "
            f"{old['code_bytes']:4d}->{new['code_bytes']:<4d}  "
            f"{old['locals']:2d}->{new['locals']:<2d}  "
            f"{new['strength_values']}/{new['strength_uses']}"
        )


if __name__ == "__main__":
    main()
