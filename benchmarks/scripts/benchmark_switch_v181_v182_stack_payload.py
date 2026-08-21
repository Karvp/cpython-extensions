from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import timeit
from pathlib import Path

OLD_ROOT = os.environ.get("PYSWITCH_BASE_0181", "")
NEW_ROOT = str((Path(__file__).resolve().parents[2] / "src").resolve())


def compile_fn(enable_switch, switch, case, source, *, mode="portable", typed=False):
    ns = {"switch": switch, "case": case}
    exec(compile(source, "<pyswitch-v182-bench>", "exec"), ns)
    fn = enable_switch(
        mode=mode,
        source=source,
        case_key_mode="typed" if typed else "python",
    )(ns["f"])
    ns["f"] = fn
    return fn


def make_direct(enable_switch, switch, case, n=64, *, mode="portable", typed=False):
    lines = ["def f(x):", "    with switch(x):"]
    for i in range(n):
        lines += [f"        if case({i}):", f"            return {1000+i}"]
    lines += ["        if case():", "            return -1"]
    return compile_fn(enable_switch, switch, case, "\n".join(lines)+"\n", mode=mode, typed=typed)


def make_expr(enable_switch, switch, case, n=64):
    lines = ["def f(x):", "    with switch(x):"]
    for i in range(n):
        lines += [f"        if case({i}):", f"            return x + {1000+i}"]
    lines += ["        if case():", "            return x + 5000"]
    return compile_fn(enable_switch, switch, case, "\n".join(lines)+"\n")


def make_multi_expr(enable_switch, switch, case, n=64):
    lines = ["def f(x):", "    with switch(x):"]
    for i in range(n):
        mul = i % 7 + 2
        offset = 1000 + i * 3
        bias = i % 5 + 1
        lines += [f"        if case({i}):", f"            return x * {mul} + {offset} - {bias}"]
    lines += ["        if case():", "            return x * 11 + 9000 - 7"]
    return compile_fn(enable_switch, switch, case, "\n".join(lines)+"\n")


def make_statement(enable_switch, switch, case, n=64):
    lines = ["def f(x):", "    with switch(x):"]
    for i in range(n):
        lines += [
            f"        if case({i}):",
            f"            y = x + {1000+i}",
            "            z = y * 2",
            "            return z + 3",
        ]
    lines += [
        "        if case():",
        "            y = x + 5000",
        "            z = y * 2",
        "            return z + 3",
    ]
    return compile_fn(enable_switch, switch, case, "\n".join(lines)+"\n")


def make_balanced(enable_switch, switch, case, n=256):
    lines = ["def f(x, flag):", "    with switch(x):"]
    for i in range(n):
        lines += [
            f"        if case({i}, when=flag):",
            f"            return {1000+i}",
            f"        elif case({i}):",
            f"            return {-1000-i}",
        ]
    lines += ["        if case():", "            return -1"]
    return compile_fn(enable_switch, switch, case, "\n".join(lines)+"\n")


def reference_dict64(x):
    return _REF_TABLE.get(x, -1)


_REF_TABLE = {i: 1000+i for i in range(64)}


def make_if64():
    lines = ["def f(x):"]
    for i in range(64):
        lines.append(("    if" if i == 0 else "    elif") + f" x == {i}: return {1000+i}")
    lines.append("    return -1")
    ns = {}
    exec("\n".join(lines)+"\n", ns)
    return ns["f"]


def make_match64():
    lines = ["def f(x):", "    match x:"]
    for i in range(64):
        lines += [f"        case {i}:", f"            return {1000+i}"]
    lines += ["        case _:", "            return -1"]
    ns = {}
    exec("\n".join(lines)+"\n", ns)
    return ns["f"]


def measure(fn, args, target=260_000):
    def run():
        total = 0
        for a in args:
            value = fn(*a)
            if isinstance(value, int):
                total += value
        return total
    loops = max(1, target // len(args))
    values = timeit.repeat(run, number=loops, repeat=3)
    return min(values) * 1e9 / (loops * len(args))


def child(root):
    sys.path.insert(0, root)
    from python_extensions import enable_switch, switch, case

    common = [(0,), (15,), (31,), (63,), (90,)] * 8
    typed_args = [(0,), (3,), (7,), (15,), (30,)] * 8
    funcs = {
        "portable_direct64": (make_direct(enable_switch, switch, case), common),
        "portable_expr64": (make_expr(enable_switch, switch, case), common),
        "portable_multi_expr64": (make_multi_expr(enable_switch, switch, case), common),
        "portable_statement64": (make_statement(enable_switch, switch, case), common),
        "portable_typed16": (make_direct(enable_switch, switch, case, 16, typed=True), typed_args),
        "portable_balanced256": (
            make_balanced(enable_switch, switch, case),
            [(0, True), (63, False), (127, True), (255, False), (300, True)] * 8,
        ),
        "live_fast64": (make_direct(enable_switch, switch, case, 64, mode="fast"), common),
        "live_fast_typed16": (
            make_direct(enable_switch, switch, case, 16, mode="fast", typed=True), typed_args
        ),
    }
    output = {}
    for name, (fn, args) in funcs.items():
        for _ in range(5000):
            fn(*args[_ % len(args)])
        output[name] = {
            "ns": measure(fn, args),
            "backend": getattr(fn, "__pyswitch_backend__", "?"),
            "stack_plans": getattr(fn, "__pyswitch_stack_payload_plan_count__", 0),
        }
    print(json.dumps(output))


def refs_child():
    args = [(0,), (15,), (31,), (63,), (90,)] * 8
    refs = {
        "dict_get64": reference_dict64,
        "if_elif64": make_if64(),
        "match64": make_match64(),
    }
    print(json.dumps({name: measure(fn, args) for name, fn in refs.items()}))


def parent(processes):
    if not OLD_ROOT:
        raise SystemExit("set PYSWITCH_BASE_0181 to the 0.18.1 source directory before running this historical comparison")
    runs = {"0.18.1": [], "0.18.2-candidate": []}
    for label, root in (("0.18.1", OLD_ROOT), ("0.18.2-candidate", NEW_ROOT)):
        for _ in range(processes):
            raw = subprocess.check_output(
                [sys.executable, __file__, "--child", root],
                text=True,
                cwd="/tmp",
            )
            runs[label].append(json.loads(raw))
    ref_runs = []
    for _ in range(processes):
        raw = subprocess.check_output(
            [sys.executable, __file__, "--refs"], text=True, cwd="/tmp"
        )
        ref_runs.append(json.loads(raw))

    report = {
        "python": sys.version.split()[0],
        "processes": processes,
        "scenarios": {},
        "references_ns": {},
    }
    for name in runs["0.18.1"][0]:
        old = [run[name]["ns"] for run in runs["0.18.1"]]
        new = [run[name]["ns"] for run in runs["0.18.2-candidate"]]
        old_med = statistics.median(old)
        new_med = statistics.median(new)
        report["scenarios"][name] = {
            "v181_ns": old_med,
            "v182_ns": new_med,
            "speedup": old_med / new_med,
            "backend": runs["0.18.2-candidate"][0][name]["backend"],
            "stack_plans": runs["0.18.2-candidate"][0][name]["stack_plans"],
            "v181_samples_ns": old,
            "v182_samples_ns": new,
        }
    for name in ref_runs[0]:
        report["references_ns"][name] = {
            "median": statistics.median(run[name] for run in ref_runs),
            "samples": [run[name] for run in ref_runs],
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--refs", action="store_true")
    parser.add_argument("--processes", type=int, default=5)
    args = parser.parse_args()
    if args.child:
        child(args.child)
    elif args.refs:
        refs_child()
    else:
        parent(args.processes)
