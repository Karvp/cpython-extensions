"""Alternating-process 0.18.5 -> 0.19.0 benchmark for all three extensions.

Usage after extracting both trees::

    python3.13 benchmarks/benchmark_all_extensions_v19.py \
        --baseline /path/to/python_extensions-0.18.5/src

The candidate defaults to this checkout's ``src`` directory.  The script emits
JSON containing every child-process result, medians, and baseline/candidate
speed ratios.  Ratio > 1 means the candidate is faster.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKER = HERE / "benchmark_all_extensions_v19_worker.py"


def _run(root: Path, loops: int) -> dict[str, Any]:
    env = os.environ.copy()
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(root) if not old else str(root) + os.pathsep + old
    output = subprocess.check_output(
        [sys.executable, str(WORKER), "--loops", str(loops)],
        env=env,
        text=True,
    )
    return json.loads(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        type=Path,
        default=(Path(os.environ["PYTHON_EXTENSIONS_BASELINE"]) if "PYTHON_EXTENSIONS_BASELINE" in os.environ else None),
        help="baseline src directory (or set PYTHON_EXTENSIONS_BASELINE)",
    )
    parser.add_argument("--candidate", type=Path, default=ROOT / "src")
    parser.add_argument("--processes", type=int, default=7)
    parser.add_argument("--loops", type=int, default=300_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.baseline is None:
        parser.error("--baseline or PYTHON_EXTENSIONS_BASELINE is required")
    if args.processes < 3:
        parser.error("--processes must be >= 3")

    roots = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    raw: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    for rep in range(args.processes):
        order = ("baseline", "candidate") if rep % 2 == 0 else ("candidate", "baseline")
        for name in order:
            raw[name].append(_run(roots[name], args.loops))

    metric_names = sorted(raw["baseline"][0]["metrics"])
    medians: dict[str, dict[str, float]] = {}
    for metric in metric_names:
        baseline = statistics.median(item["metrics"][metric] for item in raw["baseline"])
        candidate = statistics.median(item["metrics"][metric] for item in raw["candidate"])
        medians[metric] = {
            "baseline_ns": baseline,
            "candidate_ns": candidate,
            "speedup": baseline / candidate,
        }

    result = {
        "python": sys.version,
        "processes_per_side": args.processes,
        "loops_per_metric_per_process": args.loops,
        "roots": {name: str(path) for name, path in roots.items()},
        "medians": medians,
        "candidate_code_bytes": raw["candidate"][0]["code_bytes"],
        "baseline_code_bytes": raw["baseline"][0]["code_bytes"],
        "candidate_telemetry": raw["candidate"][0]["telemetry"],
        "baseline_telemetry": raw["baseline"][0]["telemetry"],
        "raw": raw,
    }
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
