"""Long-running unchanged-control worker for the 0.18.5 -> 0.19.0 benchmark."""
from __future__ import annotations

import argparse
import hashlib
import json
import timeit

import benchmark_all_extensions_v19_worker as suite

CONTROLS = {
    "inline_dynamic_control_ns": suite.inline_dynamic,
    "goto_marker_free_control_ns": suite.goto_control,
    "switch_direct_control_ns": suite.switch_direct,
}


def run(loops: int) -> dict[str, object]:
    warm = max(100_000, loops // 10)
    for _ in range(warm):
        suite.inline_dynamic(7)
        suite.goto_control(7)
        suite.switch_direct(4)

    metrics = {
        "inline_dynamic_control_ns": timeit.timeit(
            "suite.inline_dynamic(7)", globals=globals(), number=loops
        ) / loops * 1e9,
        "goto_marker_free_control_ns": timeit.timeit(
            "suite.goto_control(7)", globals=globals(), number=loops
        ) / loops * 1e9,
        "switch_direct_control_ns": timeit.timeit(
            "suite.switch_direct(4)", globals=globals(), number=loops
        ) / loops * 1e9,
    }
    hashes = {
        name: hashlib.sha256(func.__code__.co_code).hexdigest()
        for name, func in CONTROLS.items()
    }
    return {"metrics": metrics, "co_code_sha256": hashes}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops", type=int, default=1_000_000)
    args = parser.parse_args()
    print(json.dumps(run(args.loops), sort_keys=True))


if __name__ == "__main__":
    main()
