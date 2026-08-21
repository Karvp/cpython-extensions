"""Compare python_extensions 0.7 and 0.8 live-range splitting.

Example:
    python benchmark_v07_v08_live_range_split.py \
        --baseline-src ../python_extensions-0.7.0/src \
        --current-src ./src \
        --repeats 9 --number 1000000

The script benchmarks each source tree in a fresh subprocess so adaptive
specialization and module registries cannot leak between versions.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

SCENARIOS = {
    "prefix_speed": r'''
@inline_function(register_only=True)
def helper(x):
    a = x + 1
    p = a * 2 + a * 3
    b = x + 2
    y = a * 4 + b * 5 + a
    z = b * 6 + b * 7 + b * 8
    return p + y + z + b

@inline_calls(policy="speed", stack_strategy="speed", shared_regions=False)
def caller(x):
    return helper(x)
''',
    "suffix_density": r'''
@inline_function(register_only=True)
def helper(x):
    a = x + 1
    b = x + 2
    y = a * 2 + a * 3 + a * 4 + b * 5 + a
    z = b * 4 + b
    return y + z + b

@inline_calls(policy="speed", stack_strategy="density", shared_regions=False)
def caller(x):
    return helper(x)
''',
}


def _run_once(source: Path, body: str, number: int, warmup: int, cpu: int | None) -> dict[str, float | int]:
    child = f'''\nimport json\nimport os\nimport timeit\nfrom python_extensions import inline_function, inline_calls\nif {cpu!r} is not None and hasattr(os, "sched_setaffinity"):\n    os.sched_setaffinity(0, {{{cpu if cpu is not None else 0}}})\n{body}\nfor _ in range({warmup}):\n    caller(11)\nn = {number}\nprint(json.dumps({{\n    "ns": timeit.timeit("caller(11)", globals=globals(), number=n) * 1e9 / n,\n    "bytes": len(caller.__code__.co_code),\n    "locals": caller.__code__.co_nlocals,\n    "split": getattr(caller.__inline_stats__, "stack_split_values", 0),\n    "split_reads": getattr(caller.__inline_stats__, "stack_split_reads", 0),\n}}))\n'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source)
    completed = subprocess.run(
        [sys.executable, "-c", child],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _measure(source: Path, body: str, repeats: int, number: int, warmup: int, cpu: int | None):
    values = [_run_once(source, body, number, warmup, cpu) for _ in range(repeats)]
    timings = [float(value["ns"]) for value in values]
    meta = values[0]
    return {
        "median": statistics.median(timings),
        "minimum": min(timings),
        "maximum": max(timings),
        "bytes": int(meta["bytes"]),
        "locals": int(meta["locals"]),
        "split": int(meta["split"]),
        "split_reads": int(meta["split_reads"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-src", type=Path, required=True, help="0.7 source directory containing python_extensions/")
    parser.add_argument("--current-src", type=Path, default=Path(__file__).resolve().parent / "src")
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--number", type=int, default=1_000_000)
    parser.add_argument("--warmup", type=int, default=20_000)
    parser.add_argument("--cpu", type=int, default=None, help="Optional Linux CPU affinity for child processes")
    args = parser.parse_args()

    for path in (args.baseline_src, args.current_src):
        if not (path / "python_extensions").is_dir():
            parser.error(f"not a python_extensions source directory: {path}")

    versions = (("0.7.0", args.baseline_src), ("0.8.0", args.current_src))
    for scenario, body in SCENARIOS.items():
        print(f"[{scenario}]")
        for version, path in versions:
            result = _measure(path, body, args.repeats, args.number, args.warmup, args.cpu)
            print(
                version,
                f"median={result['median']:.3f} ns",
                f"range={result['minimum']:.3f}-{result['maximum']:.3f}",
                f"bytes={result['bytes']}",
                f"locals={result['locals']}",
                f"split={result['split']}",
                f"split_reads={result['split_reads']}",
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
