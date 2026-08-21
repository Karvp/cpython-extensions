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

@inline_calls(region_dataflow=True, policy="speed")
def rare_pair(count, use_affine):
    i = 0
    total = 0
    while count > 0:
        if use_affine:
            total += affine(i)
            total += affine(i)
        else:
            total += 1
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True, policy="speed")
def changing_pair(count):
    i = 0
    total = 0
    while count > 0:
        if i % 5 < 2:
            total += affine(i)
            total += affine(i)
        else:
            total += 1
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True, policy="speed")
def single_control(count, use_affine):
    i = 0
    total = 0
    while count > 0:
        if use_affine:
            total += affine(i)
        else:
            total += 1
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True, policy="speed")
def global_branch(count, left):
    i = 0
    total = 0
    while count > 0:
        if left:
            total += affine(i)
        else:
            total += affine(i)
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True, policy="speed")
def pre_post_pair(count, use_affine):
    i = 1
    total = 0
    while count > 0:
        if use_affine:
            total += affine(i)
            total += affine(i)
        i += 3
        if use_affine:
            total += affine(i)
            total += affine(i)
        else:
            total += 1
        count -= 1
    return total

cases = {
    "rare_pair_cold": (rare_pair, (24, False)),
    "rare_pair_hot": (rare_pair, (24, True)),
    "changing_pair": (changing_pair, (24,)),
    "single_control": (single_control, (24, False)),
    "global_branch": (global_branch, (24, True)),
    "pre_post_cold": (pre_post_pair, (24, False)),
    "pre_post_hot": (pre_post_pair, (24, True)),
}
for function, args in cases.values():
    for _ in range(1000):
        function(*args)

output = {}
for name, (function, args) in cases.items():
    timings = timeit.repeat(lambda f=function, a=args: f(*a), number=12000, repeat=3)
    stats = function.__inline_stats__
    output[name] = {
        "ns": min(timings) / 12000 * 1e9,
        "code_bytes": len(function.__code__.co_code),
        "locals": function.__code__.co_nlocals,
        "result": function(*args),
        "strength_values": getattr(stats, "cfg_strength_reduced_values", 0),
        "strength_uses": getattr(stats, "cfg_strength_reduced_uses", 0),
        "strength_updates": getattr(stats, "cfg_strength_reduction_updates", 0),
        "lazy_values": getattr(stats, "cfg_strength_lazy_values", 0),
        "lazy_uses": getattr(stats, "cfg_strength_lazy_uses", 0),
        "lazy_materializations": getattr(stats, "cfg_strength_lazy_materializations", 0),
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
    print("scenario             v0.16 ns   v0.17 ns  speedup   bytes        locals  global(v/u/upd) lazy(v/u/mat)")
    for name in baseline:
        old = baseline[name]
        new = current[name]
        if old["result"] != new["result"]:
            raise AssertionError((name, old["result"], new["result"]))
        speedup = old["ns"] / new["ns"]
        print(
            f"{name:20s} {old['ns']:9.2f} {new['ns']:10.2f}  {speedup:7.3f}x  "
            f"{old['code_bytes']:4d}->{new['code_bytes']:<4d}  "
            f"{old['locals']:2d}->{new['locals']:<2d}  "
            f"{new['strength_values']}/{new['strength_uses']}/{new['strength_updates']} "
            f"{new['lazy_values']}/{new['lazy_uses']}/{new['lazy_materializations']}"
        )


if __name__ == "__main__":
    main()
