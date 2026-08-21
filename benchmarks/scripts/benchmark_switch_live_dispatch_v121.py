"""Authoritative real-backend portable-vs-fast switch hot-loop benchmark.

Each configuration is measured in multiple isolated CPython processes.  Every
worker compiles ``mode='portable'`` and ``mode='fast'`` from the exact same
source, validates results against an independent reference, asserts the actual
compiler backends, warms both functions, alternates timing order, and reports
nanoseconds per *internal switch dispatch*.

Example certification run::

    python benchmarks/scripts/benchmark_switch_live_dispatch_v121.py \
      --routes 16,64,256,1024,2048,4096 --sites 1 \
      --workloads dispatch,whole --processes 5 \
      --json benchmarks/results/BENCHMARK_SWITCH_LIVE_DISPATCH_V121.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORKER = HERE / "benchmark_switch_live_dispatch_v121_worker.py"


def _csv_ints(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not result or any(item <= 0 for item in result):
        raise argparse.ArgumentTypeError("expected comma-separated positive integers")
    return result


def _csv_choices(value: str, choices: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(part.strip() for part in value.split(",") if part.strip())
    if not result or any(item not in choices for item in result):
        raise argparse.ArgumentTypeError(f"expected comma-separated subset of {choices!r}")
    return result


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not current else src + os.pathsep + current
    return env


def run_worker(
    *,
    routes: int,
    sites: int,
    workload: str,
    patterns: tuple[str, ...],
    target_dispatches: int,
    warmup_dispatches: int,
    repeats: int,
    seed: int,
    order: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(WORKER),
        "--routes", str(routes),
        "--sites", str(sites),
        "--workload", workload,
        "--patterns", ",".join(patterns),
        "--target-dispatches", str(target_dispatches),
        "--warmup-dispatches", str(warmup_dispatches),
        "--repeats", str(repeats),
        "--seed", str(seed),
        "--order", str(order),
    ]
    output = subprocess.check_output(command, env=_worker_env(), text=True)
    return json.loads(output)


def aggregate(raw: list[dict[str, Any]], patterns: tuple[str, ...]) -> dict[str, Any]:
    first = raw[0]
    for item in raw[1:]:
        if item["source_sha256"] != first["source_sha256"]:
            raise AssertionError("isolated workers did not compile identical source")
        if item["telemetry"] != first["telemetry"]:
            raise AssertionError("backend telemetry changed across isolated workers")
        if item["correctness"] != first["correctness"]:
            raise AssertionError("correctness evidence changed across isolated workers")

    timings: dict[str, Any] = {}
    for pattern in patterns:
        portable = [item["timings"][pattern]["portable_ns_per_dispatch"] for item in raw]
        fast = [item["timings"][pattern]["fast_ns_per_dispatch"] for item in raw]
        portable_median = statistics.median(portable)
        fast_median = statistics.median(fast)
        timings[pattern] = {
            "portable_median_ns_per_dispatch": portable_median,
            "fast_median_ns_per_dispatch": fast_median,
            "portable_over_fast": portable_median / fast_median,
            "saved_ns_per_dispatch": portable_median - fast_median,
            "portable_process_medians_ns": portable,
            "fast_process_medians_ns": fast,
            "dispatch_count_per_timed_call": first["timings"][pattern]["dispatch_count_per_timed_call"],
            "sequence_length": first["timings"][pattern]["sequence_length"],
        }

    return {
        "routes": first["routes"],
        "sites": first["sites"],
        "workload": first["workload"],
        "source_sha256": first["source_sha256"],
        "source_lines": first["source_lines"],
        "compile_ms_median": {
            "portable": statistics.median(item["compile_ms"]["portable"] for item in raw),
            "fast": statistics.median(item["compile_ms"]["fast"] for item in raw),
        },
        "code_bytes": first["code_bytes"],
        "telemetry": first["telemetry"],
        "correctness": first["correctness"],
        "timings": timings,
        "raw_processes": raw,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure real CPython 3.13 live-inline switch dispatch against the general portable balanced backend."
    )
    parser.add_argument("--routes", type=_csv_ints, default=(16, 64, 256, 1024, 2048, 4096))
    parser.add_argument("--sites", type=_csv_ints, default=(1,))
    parser.add_argument(
        "--workloads",
        type=lambda value: _csv_choices(value, ("dispatch", "whole")),
        default=("dispatch", "whole"),
    )
    parser.add_argument(
        "--patterns",
        type=lambda value: _csv_choices(value, ("sequential", "alternating", "random", "skewed")),
        default=("sequential", "alternating", "random", "skewed"),
    )
    parser.add_argument("--processes", type=int, default=5)
    parser.add_argument("--target-dispatches", type=int, default=250_000)
    parser.add_argument("--warmup-dispatches", type=int, default=25_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0x518121)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if args.quick:
        args.processes = 3
        args.target_dispatches = min(args.target_dispatches, 50_000)
        args.warmup_dispatches = min(args.warmup_dispatches, 5_000)
        args.repeats = min(args.repeats, 3)
    if args.processes < 3:
        parser.error("--processes must be >= 3 for isolated-process median evidence")
    if args.target_dispatches <= 0 or args.warmup_dispatches < 0 or args.repeats < 1:
        parser.error("invalid dispatch/repeat counts")

    rows: list[dict[str, Any]] = []
    for sites in args.sites:
        for routes in args.routes:
            for workload in args.workloads:
                raw = []
                for process_index in range(args.processes):
                    raw.append(
                        run_worker(
                            routes=routes,
                            sites=sites,
                            workload=workload,
                            patterns=args.patterns,
                            target_dispatches=args.target_dispatches,
                            warmup_dispatches=args.warmup_dispatches,
                            repeats=args.repeats,
                            seed=args.seed,
                            order=process_index,
                        )
                    )
                row = aggregate(raw, args.patterns)
                rows.append(row)
                random_result = row["timings"].get("random") or next(iter(row["timings"].values()))
                print(
                    f"routes={routes:4d} sites={sites:2d} workload={workload:8s} "
                    f"portable={random_result['portable_median_ns_per_dispatch']:8.2f} ns "
                    f"fast={random_result['fast_median_ns_per_dispatch']:8.2f} ns "
                    f"speedup={random_result['portable_over_fast']:5.2f}x",
                    file=sys.stderr,
                )

    payload = {
        "schema": 1,
        "benchmark": "real CPython 3.13 live-inline vs portable-balanced switch hot-loop dispatch",
        "package_version": __import__("python_extensions").__version__,
        "python": sys.version,
        "platform": platform.platform(),
        "method": {
            "identical_source_per_backend": True,
            "one_outer_python_call_per_timed_sample": True,
            "real_fast_backend_required": "cpython313-live-inline-v18",
            "real_portable_backend_required": "portable-balanced-v18",
            "portable_direct_or_template_plans_allowed": False,
            "independent_reference_correctness_check": True,
            "isolated_processes_per_configuration": args.processes,
            "within_process_repeats": args.repeats,
            "timing_order": "alternated portable/fast",
            "target_internal_dispatches_per_timed_call": args.target_dispatches,
            "warmup_internal_dispatches": args.warmup_dispatches,
            "traffic_patterns": list(args.patterns),
            "workloads": {
                "dispatch": "minimal control-heavy route body; dispatch-dominant, general balanced portable path",
                "whole": "heterogeneous arithmetic/control-flow route bodies; whole-workload comparison",
            },
        },
        "rows": rows,
    }

    encoded = json.dumps(payload, indent=2, sort_keys=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
