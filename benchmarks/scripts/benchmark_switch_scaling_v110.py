from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import sys
import timeit
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from python_extensions import case, enable_switch, switch


DEFAULT_SIZES = (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024)


@dataclass(frozen=True)
class Family:
    keys: tuple[Any, ...]
    miss: Any
    if_elif: Callable[[Any], int]
    match: Callable[[Any], int]
    dict_get: Callable[[Any], int]
    extension: Callable[[Any], int]


def _literal(value: Any) -> str:
    return repr(value)


def _compile_function(source: str, namespace: dict[str, Any]) -> Callable[[Any], int]:
    local_namespace = dict(namespace)
    exec(compile(source, "<switch-scaling-benchmark>", "exec"), local_namespace)
    return local_namespace["route"]


def build_family(size: int, key_kind: str) -> Family:
    if size <= 0:
        raise ValueError("size must be positive")
    if key_kind == "int":
        keys: tuple[Any, ...] = tuple(range(size))
        miss: Any = -1
    elif key_kind == "str":
        width = max(3, len(str(size - 1)))
        keys = tuple(f"k{i:0{width}d}" for i in range(size))
        miss = "__missing_switch_key__"
    else:
        raise ValueError("key_kind must be 'int' or 'str'")

    if_lines = ["def route(value):"]
    for index, key in enumerate(keys):
        keyword = "if" if index == 0 else "elif"
        if_lines.append(f"    {keyword} value == {_literal(key)}: return {index + 1}")
    if_lines.append("    return -1")
    if_elif = _compile_function("\n".join(if_lines) + "\n", {})

    match_lines = ["def route(value):", "    match value:"]
    for index, key in enumerate(keys):
        match_lines.append(f"        case {_literal(key)}: return {index + 1}")
    match_lines.append("        case _: return -1")
    match_fn = _compile_function("\n".join(match_lines) + "\n", {})

    table = {key: index + 1 for index, key in enumerate(keys)}
    dict_get = _compile_function(
        "def route(value, _get=table.get):\n    return _get(value, -1)\n",
        {"table": table},
    )

    switch_lines = ["def route(value):", "    with switch(value):"]
    for index, key in enumerate(keys):
        switch_lines.append(f"        if case({_literal(key)}): return {index + 1}")
    switch_lines.append("        if case(): return -1")
    switch_source = "\n".join(switch_lines) + "\n"
    raw_switch = _compile_function(switch_source, {"switch": switch, "case": case})
    extension = enable_switch(mode="portable", source=switch_source)(raw_switch)

    # Correctness is a prerequisite, not inferred from timing.
    for index, key in enumerate(keys):
        expected = index + 1
        for name, fn in (
            ("if/elif", if_elif),
            ("match", match_fn),
            ("dict.get", dict_get),
            ("extension", extension),
        ):
            actual = fn(key)
            if actual != expected:
                raise AssertionError(
                    f"{key_kind}/{size} {name}: {key!r} -> {actual!r}, expected {expected!r}"
                )
    for name, fn in (
        ("if/elif", if_elif),
        ("match", match_fn),
        ("dict.get", dict_get),
        ("extension", extension),
    ):
        actual = fn(miss)
        if actual != -1:
            raise AssertionError(
                f"{key_kind}/{size} {name}: miss -> {actual!r}, expected -1"
            )

    return Family(keys, miss, if_elif, match_fn, dict_get, extension)


def _measure_dispatch(
    fn: Callable[[Any], int],
    sequence: tuple[Any, ...],
    *,
    target_dispatches: int,
    repeat: int,
    warmup_batches: int,
) -> dict[str, float | int]:
    def batch() -> int:
        total = 0
        for value in sequence:
            total += fn(value)
        return total

    expected = batch()
    for _ in range(warmup_batches):
        if batch() != expected:
            raise AssertionError("benchmark result changed during warmup")

    number = max(1, target_dispatches // len(sequence))
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        samples = timeit.repeat(batch, number=number, repeat=repeat)
    finally:
        if was_enabled:
            gc.enable()

    dispatch_count = number * len(sequence)
    per_dispatch = [sample / dispatch_count for sample in samples]
    return {
        "median_ns": statistics.median(per_dispatch) * 1e9,
        "best_ns": min(per_dispatch) * 1e9,
        "stdev_ns": statistics.pstdev(per_dispatch) * 1e9,
        "sample_dispatches": dispatch_count,
        "repeat": repeat,
    }


def benchmark_size(
    size: int,
    key_kind: str,
    *,
    target_dispatches: int,
    repeat: int,
    warmup_batches: int,
) -> dict[str, Any]:
    family = build_family(size, key_kind)
    # Every successful route is hit equally often in forward and reverse order.
    # Miss behavior is validated above but excluded from the timing sequence so
    # large if/elif chains are measured by average hit depth rather than a
    # deliberately worst-case miss distribution.
    sequence = family.keys + tuple(reversed(family.keys))
    methods = {
        "if_elif": family.if_elif,
        "match": family.match,
        "dict_get": family.dict_get,
        "extension": family.extension,
    }
    timing = {
        name: _measure_dispatch(
            fn,
            sequence,
            target_dispatches=target_dispatches,
            repeat=repeat,
            warmup_batches=warmup_batches,
        )
        for name, fn in methods.items()
    }
    ext_ns = float(timing["extension"]["median_ns"])
    return {
        "size": size,
        "key_kind": key_kind,
        "backend": getattr(family.extension, "__pyswitch_backend__", "unknown"),
        "case_count": getattr(family.extension, "__pyswitch_case_count__", None),
        "code_bytes": {
            name: len(fn.__code__.co_code) for name, fn in methods.items()
        },
        "timing": timing,
        "speedup_vs_if_elif": float(timing["if_elif"]["median_ns"]) / ext_ns,
        "speedup_vs_match": float(timing["match"]["median_ns"]) / ext_ns,
        "ratio_vs_dict_get": ext_ns / float(timing["dict_get"]["median_ns"]),
    }


def _parse_sizes(value: str) -> tuple[int, ...]:
    sizes = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError("sizes must be comma-separated positive integers")
    return sizes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare switch scaling against if/elif, match, and hand-written dict.get."
    )
    parser.add_argument("--sizes", type=_parse_sizes, default=DEFAULT_SIZES)
    parser.add_argument("--key-kinds", choices=("int", "str", "both"), default="both")
    parser.add_argument("--quick", action="store_true", help="use the CI-friendly timing preset")
    parser.add_argument("--target-dispatches", type=int, help="successful dispatches targeted per sample before large-size scaling")
    parser.add_argument("--repeat", type=int, help="timeit repeat count")
    parser.add_argument("--warmup-batches", type=int, help="warm-up batch count")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    preset = (40_000, 5, 25) if args.quick else (250_000, 9, 100)
    target_dispatches = preset[0] if args.target_dispatches is None else args.target_dispatches
    repeat = preset[1] if args.repeat is None else args.repeat
    warmup = args.warmup_batches if args.warmup_batches is not None else preset[2]
    if target_dispatches <= 0 or repeat <= 0 or warmup < 0:
        parser.error("target dispatches/repeat must be positive and warm-up must be non-negative")

    kinds = ("int", "str") if args.key_kinds == "both" else (args.key_kinds,)
    rows = []
    for key_kind in kinds:
        for size in args.sizes:
            # Keep very large linear baselines practical while still collecting
            # tens of thousands of dispatches per sample.
            row_target = max(50_000, target_dispatches * 256 // max(256, size))
            rows.append(
                benchmark_size(
                    size,
                    key_kind,
                    target_dispatches=row_target,
                    repeat=repeat,
                    warmup_batches=warmup,
                )
            )

    payload = {
        "schema": 1,
        "benchmark": "switch scaling vs native Python dispatch",
        "package_version": __import__("python_extensions").__version__,
        "python": sys.version,
        "platform": platform.platform(),
        "method": {
            "timer": "timeit.repeat",
            "aggregation": "median per successful dispatch",
            "hit_distribution": "uniform over every route, forward then reverse",
            "misses": "validated for correctness, excluded from timing",
            "target_dispatches_per_sample": target_dispatches,
            "repeat": repeat,
            "warmup_batches": warmup,
            "sizes": ",".join(str(size) for size in args.sizes),
        },
        "rows": rows,
    }

    print(f"Python: {sys.version.split()[0]} | {platform.platform()}")
    print(
        f"{'kind':4s} {'N':>4s} {'if ns':>9s} {'match ns':>9s} "
        f"{'dict ns':>9s} {'switch ns':>10s} {'vs if':>8s} {'vs match':>9s} {'dict ratio':>11s}"
    )
    for row in rows:
        timing = row["timing"]
        print(
            f"{row['key_kind']:4s} {row['size']:4d} "
            f"{timing['if_elif']['median_ns']:9.2f} "
            f"{timing['match']['median_ns']:9.2f} "
            f"{timing['dict_get']['median_ns']:9.2f} "
            f"{timing['extension']['median_ns']:10.2f} "
            f"{row['speedup_vs_if_elif']:7.2f}x "
            f"{row['speedup_vs_match']:8.2f}x "
            f"{row['ratio_vs_dict_get']:10.2f}x"
        )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
