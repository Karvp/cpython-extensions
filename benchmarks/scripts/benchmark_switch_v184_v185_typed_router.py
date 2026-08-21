"""Reproducible CPython 3.13 benchmark for pyswitch 0.18.4 -> 0.18.5.

The current source tree is benchmarked against a caller-supplied 0.18.4
``src`` directory.  Each sample runs in a fresh child interpreter and parent
execution alternates baseline/candidate order to reduce frequency/order bias.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def _compile_switch(switch, case, enable_switch, source: str, *, extra=None, key_mode="typed"):
    ns = {"switch": switch, "case": case}
    if extra:
        ns.update(extra)
    exec(compile(source, "<pyswitch-v185-bench>", "exec"), ns)
    name = next(
        line.split("(", 1)[0].split()[1]
        for line in source.splitlines()
        if line.startswith("def ")
    )
    return enable_switch(
        mode="portable", case_key_mode=key_mode, source=source
    )(ns[name])


def _child(src: str, loops: int) -> None:
    sys.path.insert(0, src)
    from python_extensions import case, enable_switch, switch

    def cs(source: str, *, extra=None, key_mode="typed"):
        return _compile_switch(
            switch, case, enable_switch, source, extra=extra, key_mode=key_mode
        )

    single = cs('''def f(x):
    with switch(x):
        if case(1): return 10
        if case(2): return 20
        if case(3): return 30
        if case(): return -1
''')
    mix2 = cs('''def f(x):
    with switch(x):
        if case(1): return 10
        if case(2): return 20
        if case(1.5): return 15
        if case(2.5): return 25
        if case(): return -1
''')
    mix4 = cs('''def f(x):
    with switch(x):
        if case(1): return 10
        if case(2): return 20
        if case(1.5): return 15
        if case(2.5): return 25
        if case(True): return 30
        if case(False): return 31
        if case("a"): return 40
        if case("b"): return 41
        if case(): return -1
''')
    mix8 = cs('''def f(x):
    with switch(x):
        if case(1): return 10
        if case(1.5): return 20
        if case(True): return 30
        if case("a"): return 40
        if case(b"a"): return 50
        if case((1,)): return 60
        if case(None): return 70
        if case(1j): return 80
        if case(): return -1
''')
    expr = cs('''def f(x):
    with switch(x):
        if case(1): return len(str(x)) + 10
        if case(1.5): return len(str(x)) + 11
        if case(True): return len(str(x)) + 12
        if case("a"): return len(str(x)) + 13
        if case(): return len(str(x)) - 1
''')
    stmt = cs('''def f(x):
    with switch(x):
        if case(1):
            y = len(str(x)) + 10
            y *= 2
            return y
        if case(1.5):
            y = len(str(x)) + 11
            y *= 2
            return y
        if case(True):
            y = len(str(x)) + 12
            y *= 2
            return y
        if case():
            y = len(str(x)) - 1
            y *= 2
            return y
''')

    def yes():
        return True

    balanced = cs('''def f(x):
    with switch(x):
        if case(1, when=yes()): return 10
        if case(1): return 11
        if case(1.5): return 20
        if case(True): return 30
        if case("a"): return 40
        if case(): return -1
''', extra={"yes": yes})
    python_direct = cs('''def f(x):
    with switch(x):
        if case(1): return 10
        if case(2): return 20
        if case(3): return 30
        if case(4): return 40
        if case(): return -1
''', key_mode="python")

    direct_table = {1: 10, 2: 20, 3: 30, 4: 40}
    direct_get = direct_table.get

    def dict_reference(x):
        return direct_get(x, -1)

    cases = {
        "dict_ref": (dict_reference, [1, 2, 3, 4, 5]),
        "python_direct": (python_direct, [1, 2, 3, 4, 5]),
        "typed_single_hit": (single, [1, 2, 3]),
        "typed_single_miss": (single, [1.0, True, "x", b"x"]),
        "typed_mix2": (mix2, [1, 1.5, 2, 2.5]),
        "typed_mix2_miss": (mix2, [b"x", (1,), None, 3j]),
        "typed_mix4": (mix4, [1, 1.5, True, "a", 2, 2.5, False, "b"]),
        "typed_mix4_miss": (mix4, [b"x", (1,), None, 3j]),
        "typed_mix8": (mix8, [1, 1.5, True, "a", b"a", (1,), None, 1j]),
        "typed_mix8_miss": (mix8, [range(2), frozenset(), 3j, b"x"]),
        "typed_expr": (expr, [1, 1.5, True, "a", 2]),
        "typed_stmt": (stmt, [1, 1.5, True, 2]),
        "typed_balanced": (balanced, [1, 1.5, True, "a", 2]),
    }

    def bench(fn, values, count: int) -> float:
        size = len(values)
        result = None
        start = time.perf_counter_ns()
        for i in range(count):
            result = fn(values[i % size])
        elapsed = time.perf_counter_ns() - start
        if result is object():  # pragma: no cover - keeps result live
            raise AssertionError
        return elapsed / count

    output = {}
    for name, (fn, values) in cases.items():
        bench(fn, values, min(12_000, loops))
        output[name] = bench(fn, values, loops)
    print(json.dumps(output, sort_keys=True))


def _parent(baseline_src: str, runs: int, loops: int, output: str | None) -> None:
    candidate_src = str(Path(__file__).resolve().parents[2] / "src")
    sources = {"0.18.4": baseline_src, "0.18.5": candidate_src}
    rows = {key: [] for key in sources}
    script = str(Path(__file__).resolve())

    for index in range(runs):
        order = ("0.18.4", "0.18.5") if index % 2 == 0 else ("0.18.5", "0.18.4")
        for label in order:
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            proc = subprocess.run(
                [
                    sys.executable,
                    script,
                    "--child",
                    "--src",
                    sources[label],
                    "--loops",
                    str(loops),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            rows[label].append(json.loads(proc.stdout))

    scenario_names = rows["0.18.4"][0].keys()
    scenarios = {}
    for name in scenario_names:
        baseline = statistics.median(row[name] for row in rows["0.18.4"])
        candidate = statistics.median(row[name] for row in rows["0.18.5"])
        scenarios[name] = {
            "v0.18.4_ns": baseline,
            "v0.18.5_ns": candidate,
            "speedup": baseline / candidate,
        }

    result = {
        "python": sys.version,
        "runs": runs,
        "loops_per_scenario_per_process": loops,
        "baseline_src": str(Path(baseline_src).resolve()),
        "candidate_src": candidate_src,
        "scenarios": scenarios,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-src")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--loops", type=int, default=100_000)
    parser.add_argument("--output")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--src")
    args = parser.parse_args()
    if args.child:
        if not args.src:
            parser.error("--child requires --src")
        _child(args.src, args.loops)
        return
    if not args.baseline_src:
        parser.error("--baseline-src is required")
    _parent(args.baseline_src, args.runs, args.loops, args.output)


if __name__ == "__main__":
    main()
