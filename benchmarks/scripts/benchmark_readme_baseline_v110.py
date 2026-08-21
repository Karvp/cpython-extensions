from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import timeit
from pathlib import Path

from python_extensions import case, enable_goto, enable_switch, inline_calls, inline_function, switch


# --- Switch: ordinary Python control flow vs extension-backed switch ---
def baseline_route(op: str) -> int:
    if op == "load":
        return 1
    elif op == "store":
        return 2
    elif op == "add":
        return 3
    elif op == "sub":
        return 4
    elif op == "mul":
        return 5
    elif op == "div":
        return 6
    elif op == "jump":
        return 7
    elif op == "halt":
        return 8
    return 0


@enable_switch(mode="auto")
def extension_route(op: str) -> int:
    with switch(op):
        if case("load"):
            return 1
        if case("store"):
            return 2
        if case("add"):
            return 3
        if case("sub"):
            return 4
        if case("mul"):
            return 5
        if case("div"):
            return 6
        if case("jump"):
            return 7
        if case("halt"):
            return 8
        if case():
            return 0


# --- Inline: ordinary helper call vs inlined helper ---
@inline_function(register_only=True)
def affine(x: int, scale: int = 4) -> int:
    return x * scale + 3


def baseline_affine(x: int) -> int:
    return affine(x)


@inline_calls(policy="always", binding="frozen", shared_regions=False)
def extension_affine(x: int) -> int:
    return affine(x)


# --- Goto: structured while loop vs validated local jump ---
def baseline_countdown(n: int) -> int:
    total = 0
    while n > 0:
        total += n
        n -= 1
    return total


@enable_goto(mode="strict")
def extension_countdown(n: int) -> int:
    total = 0
    label .loop
    if n <= 0:
        goto .done
    total += n
    n -= 1
    goto .loop
    label .done
    return total


OPS = ("load", "store", "add", "sub", "mul", "div", "jump", "halt", "miss")


def _switch_batch(fn, rounds: int = 32) -> int:
    acc = 0
    for _ in range(rounds):
        for op in OPS:
            acc += fn(op)
    return acc


def _inline_batch(fn, rounds: int = 256) -> int:
    acc = 0
    for i in range(rounds):
        acc += fn(i & 31)
    return acc


def _goto_batch(fn, rounds: int = 64) -> int:
    acc = 0
    for i in range(rounds):
        acc += fn((i & 7) + 1)
    return acc


def measure(fn, *, number: int, repeat: int, warmup: int) -> dict[str, float]:
    for _ in range(warmup):
        fn()
    samples = timeit.repeat(fn, number=number, repeat=repeat)
    per_call = [s / number for s in samples]
    return {
        "median_seconds": statistics.median(per_call),
        "best_seconds": min(per_call),
        "stdev_seconds": statistics.pstdev(per_call),
    }


def benchmark_pair(name: str, baseline, extension, *, number: int, repeat: int, warmup: int) -> dict:
    expected = baseline()
    actual = extension()
    if actual != expected:
        raise AssertionError(f"{name}: extension result {actual!r} != baseline {expected!r}")
    b = measure(baseline, number=number, repeat=repeat, warmup=warmup)
    e = measure(extension, number=number, repeat=repeat, warmup=warmup)
    return {
        "name": name,
        "baseline": b,
        "extension": e,
        "speedup_median": b["median_seconds"] / e["median_seconds"],
        "speedup_best": b["best_seconds"] / e["best_seconds"],
        "result": expected,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if args.quick:
        number, repeat, warmup = 1_000, 5, 50
    else:
        number, repeat, warmup = 20_000, 9, 500

    scenarios = [
        ("8-way string routing", lambda: _switch_batch(baseline_route), lambda: _switch_batch(extension_route)),
        ("small affine helper", lambda: _inline_batch(baseline_affine), lambda: _inline_batch(extension_affine)),
        ("countdown state loop", lambda: _goto_batch(baseline_countdown), lambda: _goto_batch(extension_countdown)),
    ]

    results = [
        benchmark_pair(name, base, ext, number=number, repeat=repeat, warmup=warmup)
        for name, base, ext in scenarios
    ]
    payload = {
        "schema": 1,
        "benchmark": "README baseline vs cpython-extensions",
        "package_version": __import__("python_extensions").__version__,
        "python": sys.version,
        "platform": platform.platform(),
        "method": {
            "timer": "timeit.repeat",
            "aggregation": "median of per-batch timings",
            "number": number,
            "repeat": repeat,
            "warmup_batches": warmup,
        },
        "scenarios": results,
    }

    print(f"Python: {sys.version.split()[0]} | {platform.platform()}")
    print(f"{'scenario':24s} {'baseline us':>12s} {'extension us':>13s} {'speedup':>9s}")
    for r in results:
        bu = r['baseline']['median_seconds'] * 1e6
        eu = r['extension']['median_seconds'] * 1e6
        print(f"{r['name']:24s} {bu:12.3f} {eu:13.3f} {r['speedup_median']:8.3f}x")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
