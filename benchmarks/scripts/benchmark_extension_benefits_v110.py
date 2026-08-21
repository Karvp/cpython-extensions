from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import sys
import timeit
from pathlib import Path

from python_extensions import enable_goto, inline_calls, inline_function


@inline_function(register_only=True)
def affine_helper(x: int, scale: int = 4) -> int:
    return x * scale + 3


def ordinary_affine(x: int) -> int:
    return affine_helper(x)


@inline_calls(policy="always", binding="frozen", shared_regions=False)
def inlined_affine(x: int) -> int:
    return affine_helper(x)


def explicit_state_machine(n: int) -> int:
    total = 0
    state = 0
    while True:
        if state == 0:
            if n <= 0:
                return total
            total += n
            state = 1
        elif state == 1:
            n -= 1
            state = 2
        else:
            total ^= n & 7
            state = 0


@enable_goto(mode="strict")
def goto_state_machine(n: int) -> int:
    total = 0
    label .check
    if n <= 0:
        goto .done
    total += n
    goto .decrement
    label .decrement
    n -= 1
    goto .mix
    label .mix
    total ^= n & 7
    goto .check
    label .done
    return total


def structured_reference(n: int) -> int:
    total = 0
    while n > 0:
        total += n
        n -= 1
        total ^= n & 7
    return total


def _measure(call, *, number: int, repeat: int, warmup: int) -> dict[str, float]:
    expected = call()
    for _ in range(warmup):
        if call() != expected:
            raise AssertionError("benchmark result changed during warmup")
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        samples = timeit.repeat(call, number=number, repeat=repeat)
    finally:
        if was_enabled:
            gc.enable()
    per_call = [sample / number for sample in samples]
    return {
        "median_ns": statistics.median(per_call) * 1e9,
        "best_ns": min(per_call) * 1e9,
        "stdev_ns": statistics.pstdev(per_call) * 1e9,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    for n in range(96):
        expected = structured_reference(n)
        if explicit_state_machine(n) != expected or goto_state_machine(n) != expected:
            raise AssertionError(f"state-machine semantic mismatch at n={n}")
    for x in range(-64, 65):
        if ordinary_affine(x) != inlined_affine(x):
            raise AssertionError(f"inline semantic mismatch at x={x}")

    if args.quick:
        number, repeat, warmup = 20_000, 5, 100
    else:
        number, repeat, warmup = 250_000, 9, 1_000

    inline_base = _measure(lambda: ordinary_affine(17), number=number, repeat=repeat, warmup=warmup)
    inline_ext = _measure(lambda: inlined_affine(17), number=number, repeat=repeat, warmup=warmup)

    # A goto is useful when the source really is an explicit state machine.
    # Also time the ideal structured formulation so readers can see how close
    # the lowered goto gets to code that can be naturally expressed as a loop.
    fsm_number = max(1_000, number // 20)
    fsm_base = _measure(lambda: explicit_state_machine(32), number=fsm_number, repeat=repeat, warmup=warmup)
    fsm_goto = _measure(lambda: goto_state_machine(32), number=fsm_number, repeat=repeat, warmup=warmup)
    fsm_structured = _measure(lambda: structured_reference(32), number=fsm_number, repeat=repeat, warmup=warmup)

    payload = {
        "schema": 1,
        "benchmark": "inline and goto intended-workload benefits",
        "package_version": __import__("python_extensions").__version__,
        "python": sys.version,
        "platform": platform.platform(),
        "method": {
            "timer": "timeit.repeat",
            "aggregation": "median per function call",
            "repeat": repeat,
            "warmup": warmup,
        },
        "inline": {
            "scenario": "small frozen helper call",
            "baseline": inline_base,
            "extension": inline_ext,
            "speedup_median": inline_base["median_ns"] / inline_ext["median_ns"],
        },
        "goto": {
            "scenario": "three-state explicit state machine, n=32",
            "baseline_explicit_state": fsm_base,
            "extension": fsm_goto,
            "structured_reference": fsm_structured,
            "speedup_vs_explicit_state": fsm_base["median_ns"] / fsm_goto["median_ns"],
            "ratio_vs_structured_reference": fsm_goto["median_ns"] / fsm_structured["median_ns"],
        },
    }

    print(f"Python: {sys.version.split()[0]} | {platform.platform()}")
    print(
        "inline small helper: "
        f"{inline_base['median_ns']:.2f} ns -> {inline_ext['median_ns']:.2f} ns "
        f"({payload['inline']['speedup_median']:.2f}x)"
    )
    print(
        "goto explicit FSM: "
        f"{fsm_base['median_ns']:.2f} ns -> {fsm_goto['median_ns']:.2f} ns "
        f"({payload['goto']['speedup_vs_explicit_state']:.2f}x); "
        f"structured reference {fsm_structured['median_ns']:.2f} ns"
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
