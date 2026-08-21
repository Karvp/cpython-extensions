"""Worker for the real-backend live-vs-portable switch hot-loop benchmark.

The generated portable and fast functions are compiled from the exact same
source.  The timed loop lives inside the transformed function so one Python
function call covers many switch dispatches.  Portable compilation is required
to select the general balanced backend; fast compilation is required to select
the CPython 3.13 live-inline backend.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import random
import statistics
import time
from typing import Any, Callable

from python_extensions import case, enable_switch, switch

MASK = 0xFFFFFFFF
PATTERNS = ("sequential", "alternating", "random", "skewed")
WORKLOADS = ("dispatch", "whole")


def _case_constant(route: int, site: int) -> int:
    # Keep values compact enough for tagged/small-int arithmetic while making
    # each site observably distinct.
    return route + site + 1


def build_source(routes: int, sites: int, workload: str) -> str:
    if routes < 2:
        raise ValueError("routes must be >= 2")
    if sites < 1:
        raise ValueError("sites must be >= 1")
    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload: {workload!r}")

    lines = [
        "def hotloop(sequence, rounds):",
        "    acc = 0x13579BDF",
        "    state = 0x2468ACE0",
        "    for _ in range(rounds):",
        "        for value in sequence:",
    ]

    for site in range(sites):
        lines.append("            with switch(value):")
        for route in range(routes):
            constant = _case_constant(route, site)
            lines.append(f"                if case({route}):")
            if workload == "dispatch":
                # The identical nested control statement deliberately makes the
                # routes ineligible for direct/expression/statement templates.
                # It is almost always false and therefore keeps route-body work
                # small while forcing the general balanced portable compiler.
                lines.extend(
                    [
                        "                    if state < 0:",
                        "                        acc ^= 1",
                        f"                    acc += {constant}",
                    ]
                )
            else:
                kind = route & 3
                if kind == 0:
                    lines.extend(
                        [
                            "                    if state & 1:",
                            f"                        acc = (acc + {constant}) & {MASK}",
                            "                    else:",
                            f"                        acc = (acc - {constant}) & {MASK}",
                        ]
                    )
                elif kind == 1:
                    lines.extend(
                        [
                            f"                    state = (state ^ {constant}) & {MASK}",
                            f"                    acc = (acc + (state & 255)) & {MASK}",
                        ]
                    )
                elif kind == 2:
                    lines.extend(
                        [
                            f"                    acc = ((acc << 1) ^ {constant}) & {MASK}",
                            "                    if acc & 8:",
                            f"                        state = (state + {constant}) & {MASK}",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "                    if acc & 2:",
                            f"                        state = (state + {constant}) & {MASK}",
                            "                    else:",
                            f"                        state = (state - {constant}) & {MASK}",
                            "                    acc ^= state & 1023",
                        ]
                    )
        lines.append("                if case():")
        if workload == "dispatch":
            lines.extend(
                [
                    "                    if state < 0:",
                    "                        acc ^= 1",
                    f"                    acc -= {site + 1}",
                ]
            )
        else:
            lines.extend(
                [
                    f"                    acc = (acc - {site + 1}) & {MASK}",
                    f"                    state = (state ^ {site + 1}) & {MASK}",
                ]
            )

    lines.append("    return (acc ^ state) & 0xFFFFFFFFFFFFFFFF")
    return "\n".join(lines) + "\n"


def compile_backend(source: str, mode: str) -> tuple[Callable[[tuple[int, ...], int], int], float]:
    namespace = {"switch": switch, "case": case}
    filename = f"<switch-live-dispatch-v121-{mode}>"
    exec(compile(source, filename, "exec"), namespace)
    started = time.perf_counter_ns()
    fn = enable_switch(mode=mode, source=source)(namespace["hotloop"])
    compile_ns = time.perf_counter_ns() - started
    return fn, compile_ns / 1e6


def verify_backend_contract(fn: Callable[..., Any], mode: str, routes: int, sites: int) -> dict[str, Any]:
    backend = getattr(fn, "__pyswitch_backend__", None)
    case_count = getattr(fn, "__pyswitch_case_count__", None)
    expected_cases = routes * sites
    if case_count != expected_cases:
        raise AssertionError(f"{mode}: case_count={case_count}, expected {expected_cases}")

    if mode == "portable":
        if backend != "portable-balanced-v18":
            raise AssertionError(f"portable benchmark escaped balanced backend: {backend!r}")
        telemetry = {
            "backend": backend,
            "case_count": case_count,
            "switch_count": getattr(fn, "__pyswitch_switch_count__", None),
            "balanced_plan_count": getattr(fn, "__pyswitch_balanced_plan_count__", None),
            "direct_plan_count": getattr(fn, "__pyswitch_direct_plan_count__", None),
            "template_plan_count": getattr(fn, "__pyswitch_template_plan_count__", None),
            "statement_template_plan_count": getattr(fn, "__pyswitch_statement_template_plan_count__", None),
            "binary_route_plan_count": getattr(fn, "__pyswitch_binary_route_plan_count__", None),
        }
        if telemetry["switch_count"] != sites or telemetry["balanced_plan_count"] != sites:
            raise AssertionError(f"portable benchmark plan mismatch: {telemetry!r}")
        if any(
            telemetry[name]
            for name in (
                "direct_plan_count",
                "template_plan_count",
                "statement_template_plan_count",
                "binary_route_plan_count",
            )
        ):
            raise AssertionError(f"portable benchmark used an unintended specialization: {telemetry!r}")
        return telemetry

    if mode == "fast":
        if backend != "cpython313-live-inline-v18":
            raise AssertionError(f"fast benchmark escaped live-inline backend: {backend!r}")
        offsets = tuple(getattr(fn, "__pyswitch_gate_offsets__", ()))
        units = tuple(getattr(fn, "__pyswitch_gate_units__", ()))
        if len(offsets) != sites or len(units) != sites:
            raise AssertionError(
                f"fast benchmark gate count mismatch: offsets={len(offsets)}, units={len(units)}, sites={sites}"
            )
        return {
            "backend": backend,
            "case_count": case_count,
            "gate_count": len(offsets),
            "gate_offsets": offsets,
            "gate_units": units,
        }

    raise ValueError(mode)


def make_sequence(routes: int, pattern: str, seed: int) -> tuple[int, ...]:
    if pattern == "sequential":
        return tuple(range(routes))
    length = min(4096, max(512, routes * 2))
    if pattern == "alternating":
        return tuple(0 if i % 2 == 0 else routes - 1 for i in range(length))
    rng = random.Random(seed ^ (routes * 0x9E3779B1))
    if pattern == "random":
        return tuple(rng.randrange(routes) for _ in range(length))
    if pattern == "skewed":
        sequence = [0] * length
        minority = max(1, length // 10)
        positions = rng.sample(range(length), minority)
        for pos in positions:
            sequence[pos] = 1 + rng.randrange(routes - 1)
        return tuple(sequence)
    raise ValueError(pattern)


def reference(sequence: tuple[int, ...], rounds: int, routes: int, sites: int, workload: str) -> int:
    acc = 0x13579BDF
    state = 0x2468ACE0
    for _ in range(rounds):
        for value in sequence:
            for site in range(sites):
                if 0 <= value < routes:
                    constant = _case_constant(value, site)
                    if workload == "dispatch":
                        if state < 0:
                            acc ^= 1
                        acc += constant
                    else:
                        kind = value & 3
                        if kind == 0:
                            if state & 1:
                                acc = (acc + constant) & MASK
                            else:
                                acc = (acc - constant) & MASK
                        elif kind == 1:
                            state = (state ^ constant) & MASK
                            acc = (acc + (state & 255)) & MASK
                        elif kind == 2:
                            acc = ((acc << 1) ^ constant) & MASK
                            if acc & 8:
                                state = (state + constant) & MASK
                        else:
                            if acc & 2:
                                state = (state + constant) & MASK
                            else:
                                state = (state - constant) & MASK
                            acc ^= state & 1023
                elif workload == "dispatch":
                    if state < 0:
                        acc ^= 1
                    acc -= site + 1
                else:
                    acc = (acc - (site + 1)) & MASK
                    state = (state ^ (site + 1)) & MASK
    return (acc ^ state) & 0xFFFFFFFFFFFFFFFF


def _rounds_for(target_dispatches: int, sequence_len: int, sites: int) -> int:
    return max(1, math.ceil(target_dispatches / (sequence_len * sites)))


def _time_once(fn: Callable[[tuple[int, ...], int], int], sequence: tuple[int, ...], rounds: int) -> tuple[float, int]:
    started = time.perf_counter_ns()
    value = fn(sequence, rounds)
    elapsed = time.perf_counter_ns() - started
    dispatches = len(sequence) * rounds
    return float(elapsed), value


def measure_pair(
    portable: Callable[[tuple[int, ...], int], int],
    fast: Callable[[tuple[int, ...], int], int],
    sequence: tuple[int, ...],
    *,
    sites: int,
    target_dispatches: int,
    warmup_dispatches: int,
    repeats: int,
    portable_first: bool,
) -> dict[str, Any]:
    rounds = _rounds_for(target_dispatches, len(sequence), sites)
    warm_rounds = _rounds_for(warmup_dispatches, len(sequence), sites)
    dispatch_count = len(sequence) * rounds * sites

    warm_expected = portable(sequence, warm_rounds)
    if fast(sequence, warm_rounds) != warm_expected:
        raise AssertionError("portable/fast divergence during warmup")

    samples: dict[str, list[float]] = {"portable": [], "fast": []}
    expected: int | None = None
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for index in range(repeats):
            order = ("portable", "fast") if ((index % 2 == 0) == portable_first) else ("fast", "portable")
            for name in order:
                fn = portable if name == "portable" else fast
                elapsed_ns, value = _time_once(fn, sequence, rounds)
                if expected is None:
                    expected = value
                elif value != expected:
                    raise AssertionError("benchmark result changed while timing")
                samples[name].append(elapsed_ns / dispatch_count)
    finally:
        if was_enabled:
            gc.enable()

    portable_ns = statistics.median(samples["portable"])
    fast_ns = statistics.median(samples["fast"])
    return {
        "dispatch_count_per_timed_call": dispatch_count,
        "rounds": rounds,
        "sequence_length": len(sequence),
        "portable_ns_per_dispatch": portable_ns,
        "fast_ns_per_dispatch": fast_ns,
        "portable_over_fast": portable_ns / fast_ns,
        "saved_ns_per_dispatch": portable_ns - fast_ns,
        "portable_samples_ns": samples["portable"],
        "fast_samples_ns": samples["fast"],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = build_source(args.routes, args.sites, args.workload)
    portable, portable_compile_ms = compile_backend(source, "portable")
    fast, fast_compile_ms = compile_backend(source, "fast")
    portable_telemetry = verify_backend_contract(portable, "portable", args.routes, args.sites)
    fast_telemetry = verify_backend_contract(fast, "fast", args.routes, args.sites)

    # Independent correctness check includes hits and misses and is deliberately
    # kept outside the timed region.
    correctness_sequence = tuple(
        [0, args.routes - 1, -1, args.routes]
        + [i % args.routes for i in range(min(args.routes, 31))]
    )
    expected = reference(correctness_sequence, 2, args.routes, args.sites, args.workload)
    portable_value = portable(correctness_sequence, 2)
    fast_value = fast(correctness_sequence, 2)
    if portable_value != expected or fast_value != expected:
        raise AssertionError(
            f"correctness failure: reference={expected}, portable={portable_value}, fast={fast_value}"
        )

    patterns = args.patterns.split(",") if args.patterns else list(PATTERNS)
    timings: dict[str, Any] = {}
    for index, pattern in enumerate(patterns):
        sequence = make_sequence(args.routes, pattern, args.seed)
        timings[pattern] = measure_pair(
            portable,
            fast,
            sequence,
            sites=args.sites,
            target_dispatches=args.target_dispatches,
            warmup_dispatches=args.warmup_dispatches,
            repeats=args.repeats,
            portable_first=((args.order + index) % 2 == 0),
        )

    return {
        "routes": args.routes,
        "sites": args.sites,
        "workload": args.workload,
        "source_sha256": __import__("hashlib").sha256(source.encode()).hexdigest(),
        "source_lines": source.count("\n"),
        "compile_ms": {"portable": portable_compile_ms, "fast": fast_compile_ms},
        "code_bytes": {"portable": len(portable.__code__.co_code), "fast": len(fast.__code__.co_code)},
        "telemetry": {"portable": portable_telemetry, "fast": fast_telemetry},
        "correctness": {"reference": expected, "portable": portable_value, "fast": fast_value},
        "timings": timings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", type=int, required=True)
    parser.add_argument("--sites", type=int, required=True)
    parser.add_argument("--workload", choices=WORKLOADS, required=True)
    parser.add_argument("--patterns", default=",".join(PATTERNS))
    parser.add_argument("--target-dispatches", type=int, default=250_000)
    parser.add_argument("--warmup-dispatches", type=int, default=25_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0x518121)
    parser.add_argument("--order", type=int, default=0)
    args = parser.parse_args()
    if args.routes < 2 or args.sites < 1:
        parser.error("routes must be >= 2 and sites must be >= 1")
    if args.target_dispatches <= 0 or args.warmup_dispatches < 0 or args.repeats < 1:
        parser.error("invalid dispatch/repeat counts")
    patterns = [part for part in args.patterns.split(",") if part]
    if not patterns or any(pattern not in PATTERNS for pattern in patterns):
        parser.error(f"patterns must be a comma-separated subset of {PATTERNS!r}")
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
