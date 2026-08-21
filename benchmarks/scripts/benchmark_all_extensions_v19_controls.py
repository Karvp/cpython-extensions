"""Interleaved unchanged-control benchmark for python_extensions 0.19.0."""
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
ROOT = HERE.parents[1]
WORKER = HERE / "benchmark_all_extensions_v19_controls_worker.py"


def _run(root: Path, loops: int) -> dict[str, Any]:
    env = os.environ.copy()
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(root) if not old else str(root) + os.pathsep + old
    return json.loads(subprocess.check_output(
        [sys.executable, str(WORKER), "--loops", str(loops)], env=env, text=True
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=ROOT / "src")
    parser.add_argument("--processes", type=int, default=13)
    parser.add_argument("--loops", type=int, default=1_000_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    roots = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    raw: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    for rep in range(args.processes):
        order = ("baseline", "candidate") if rep % 2 == 0 else ("candidate", "baseline")
        for name in order:
            raw[name].append(_run(roots[name], args.loops))

    names = sorted(raw["baseline"][0]["metrics"])
    medians = {}
    for name in names:
        baseline = statistics.median(x["metrics"][name] for x in raw["baseline"])
        candidate = statistics.median(x["metrics"][name] for x in raw["candidate"])
        medians[name] = {
            "baseline_ns": baseline,
            "candidate_ns": candidate,
            "speedup": baseline / candidate,
        }
    baseline_hashes = raw["baseline"][0]["co_code_sha256"]
    candidate_hashes = raw["candidate"][0]["co_code_sha256"]
    result = {
        "python": sys.version,
        "processes_per_side": args.processes,
        "loops_per_metric_per_process": args.loops,
        "roots": {k: str(v) for k, v in roots.items()},
        "medians": medians,
        "baseline_co_code_sha256": baseline_hashes,
        "candidate_co_code_sha256": candidate_hashes,
        "co_code_identical": {
            name: baseline_hashes[name] == candidate_hashes[name] for name in names
        },
        "raw": raw,
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
