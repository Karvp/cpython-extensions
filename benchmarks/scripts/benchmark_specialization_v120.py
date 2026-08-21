from __future__ import annotations

import argparse
import functools
import json
import platform
import statistics
import sys
import timeit
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from python_extensions import __version__, hotpath, partial, specialize


def make_value_router(size: int):
    lines = ["def route(value, mode):"]
    for index in range(size):
        prefix = "if" if index == 0 else "elif"
        lines.append(f"    {prefix} mode == 'm{index}':")
        lines.append(f"        return value + {index}")
    lines.extend(["    else:", "        return value - 1"])
    namespace: dict[str, object] = {}
    exec("\n".join(lines), namespace)
    return namespace["route"]


def make_type_router(size: int):
    classes = [type(f"Type{index}", (), {}) for index in range(size)]
    namespace: dict[str, object] = {cls.__name__: cls for cls in classes}
    lines = ["def route(value):"]
    for index, cls in enumerate(classes):
        prefix = "if" if index == 0 else "elif"
        lines.append(f"    {prefix} type(value) is {cls.__name__}:")
        lines.append(f"        return {index}")
    lines.extend(["    else:", "        return -1"])
    exec("\n".join(lines), namespace)
    return namespace["route"], classes


def best_ns(call, *, number: int, repeat: int) -> float:
    values = timeit.repeat(call, number=number, repeat=repeat)
    return min(values) / number * 1e9


def bench_value_router(size: int, position: str, *, number: int, repeat: int, threshold: int):
    baseline = make_value_router(size)
    if position == "first":
        index = 0
    elif position == "middle":
        index = size // 2
    else:
        index = size - 1
    mode = f"m{index}"

    std_partial = functools.partial(baseline, mode=mode)
    optimized_partial = partial(baseline, mode=mode)
    explicit = specialize(constants={"mode": mode})(baseline)
    adaptive = hotpath(
        threshold=threshold,
        max_variants=1,
        types=False,
        constants=("mode",),
    )(baseline)

    expected = baseline(7, mode)
    assert std_partial(7) == expected
    assert optimized_partial(7) == expected
    assert explicit(7, mode) == expected
    for _ in range(threshold + 2):
        assert adaptive(7, mode) == expected

    base_ns = best_ns(lambda: baseline(7, mode), number=number, repeat=repeat)
    std_partial_ns = best_ns(lambda: std_partial(7), number=number, repeat=repeat)
    partial_ns = best_ns(lambda: optimized_partial(7), number=number, repeat=repeat)
    explicit_ns = best_ns(lambda: explicit(7, mode), number=number, repeat=repeat)
    adaptive_ns = best_ns(lambda: adaptive(7, mode), number=number, repeat=repeat)

    return {
        "size": size,
        "position": position,
        "route_index": index,
        "baseline_ns": base_ns,
        "stdlib_partial_ns": std_partial_ns,
        "partial_ns": partial_ns,
        "specialize_ns": explicit_ns,
        "hotpath_ns": adaptive_ns,
        "partial_speedup": base_ns / partial_ns,
        "partial_vs_functools": std_partial_ns / partial_ns,
        "specialize_speedup": base_ns / explicit_ns,
        "hotpath_speedup": base_ns / adaptive_ns,
        "original_code_bytes": len(baseline.__code__.co_code),
        "partial_code_bytes": len(optimized_partial.__code__.co_code),
        "specialize_variant_code_bytes": (
            len(explicit.specialization_variants()[0].__code__.co_code)
            if explicit.specialization_variants()
            else None
        ),
        "hotpath_variants": adaptive.specialization_stats().variants_created,
        "hotpath_backend": adaptive.__python_extensions_dispatch_mode__,
    }


def bench_type_router(size: int, *, number: int, repeat: int, threshold: int):
    baseline, classes = make_type_router(size)
    value = classes[-1]()
    expected_type = classes[-1]
    explicit = specialize(types={"value": expected_type})(baseline)
    adaptive = hotpath(threshold=threshold, max_variants=1)(baseline)
    expected = baseline(value)
    assert explicit(value) == expected
    for _ in range(threshold + 2):
        assert adaptive(value) == expected

    base_ns = best_ns(lambda: baseline(value), number=number, repeat=repeat)
    explicit_ns = best_ns(lambda: explicit(value), number=number, repeat=repeat)
    adaptive_ns = best_ns(lambda: adaptive(value), number=number, repeat=repeat)
    return {
        "size": size,
        "baseline_ns": base_ns,
        "specialize_ns": explicit_ns,
        "hotpath_ns": adaptive_ns,
        "specialize_speedup": base_ns / explicit_ns,
        "hotpath_speedup": base_ns / adaptive_ns,
        "original_code_bytes": len(baseline.__code__.co_code),
        "variant_code_bytes": len(explicit.specialization_variants()[0].__code__.co_code),
        "hotpath_variants": adaptive.specialization_stats().variants_created,
        "hotpath_backend": adaptive.__python_extensions_dispatch_mode__,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    parser.add_argument("--number", type=int, default=100_000)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--threshold", type=int, default=16)
    parser.add_argument("--sizes", default="4,8,16,32,64,128,256")
    args = parser.parse_args()
    sizes = [int(part) for part in args.sizes.split(",") if part]
    if args.number < 1 or args.repeat < 1 or args.threshold < 1 or not sizes:
        parser.error("number, repeat, threshold, and sizes must be positive")

    value_rows = []
    for size in sizes:
        for position in ("first", "middle", "last"):
            value_rows.append(
                bench_value_router(
                    size,
                    position,
                    number=args.number,
                    repeat=args.repeat,
                    threshold=args.threshold,
                )
            )

    type_sizes = [size for size in sizes if size <= 64]
    type_rows = [
        bench_type_router(size, number=args.number, repeat=args.repeat, threshold=args.threshold)
        for size in type_sizes
    ]

    payload = {
        "benchmark": "specialization_v120",
        "package_version": __version__,
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "method": {
            "timer": "timeit.repeat minimum",
            "number": args.number,
            "repeat": args.repeat,
            "hotpath_threshold": args.threshold,
        },
        "value_dispatch": value_rows,
        "type_dispatch": type_rows,
    }

    print(f"python_extensions {__version__} specialization benchmark")
    print("value branch routing (speedup over ordinary call with fixed mode)")
    print("cases position   partial  functools  specialize  hotpath")
    for row in value_rows:
        print(
            f"{row['size']:>5} {row['position']:<8} "
            f"{row['partial_speedup']:>8.2f}x "
            f"{row['partial_vs_functools']:>8.2f}x "
            f"{row['specialize_speedup']:>10.2f}x "
            f"{row['hotpath_speedup']:>7.2f}x"
        )
    print("\nexact-type branch routing (last type)")
    print("types specialize hotpath")
    for row in type_rows:
        print(
            f"{row['size']:>5} {row['specialize_speedup']:>9.2f}x "
            f"{row['hotpath_speedup']:>7.2f}x"
        )

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
