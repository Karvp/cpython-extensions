from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from python_extensions import case, enable_goto, enable_switch, fallthrough, inline_calls, inline_function, switch
import inspect

_switch_options = {"mode": "portable"}
if "compact_routes" in inspect.signature(enable_switch).parameters:
    _switch_options["compact_routes"] = True


@enable_switch(**_switch_options)
def _bench_shared(value, out):
    out.clear()
    with switch(value):
        if case(1):
            out.append("one")
            fallthrough()
        if case(2):
            out.append("two")
            fallthrough()
        if case():
            try:
                out.append("tail")
            finally:
                out.append("done")
    return len(out)


@enable_switch(mode="portable")
def _bench_shared_default(value, out):
    out.clear()
    with switch(value):
        if case(1):
            out.append("one")
            fallthrough()
        if case(2):
            out.append("two")
            fallthrough()
        if case():
            try:
                out.append("tail")
            finally:
                out.append("done")
    return len(out)


@inline_function(register_only=True)
def _bench_nested(a=3, b=4, c=5):
    return -(a + b + c), ~(a + b), not ((a + c) == b)


@inline_calls(policy="speed")
def _bench_folded():
    return _bench_nested()


@enable_goto
def _bench_goto_loop(value):
    total = 0
    label .again
    if value <= 0:
        goto .done
    total += value
    value -= 1
    goto .again
    label .done
    return total


def bench(fn, *args, loops=500_000):
    for _ in range(30_000):
        fn(*args)
    start = time.perf_counter_ns()
    for _ in range(loops):
        fn(*args)
    return (time.perf_counter_ns() - start) / loops


def child():
    out=[]
    results = {
        "switch_hit_ns": bench(_bench_shared, 1, out),
        "switch_mid_ns": bench(_bench_shared, 2, out),
        "switch_miss_ns": bench(_bench_shared, 9, out),
        "switch_code_bytes": len(_bench_shared.__code__.co_code),
        "switch_default_hit_ns": bench(_bench_shared_default, 1, out),
        "switch_default_miss_ns": bench(_bench_shared_default, 9, out),
        "switch_default_code_bytes": len(_bench_shared_default.__code__.co_code),
        "inline_fixed_ns": bench(_bench_folded),
        "inline_code_bytes": len(_bench_folded.__code__.co_code),
        "inline_unary_folds": _bench_folded.__inline_stats__.constant_unary_ops_folded,
        "goto_loop_ns": bench(_bench_goto_loop, 5, loops=300_000),
        "goto_code_bytes": len(_bench_goto_loop.__code__.co_code),
    }
    print(json.dumps(results, sort_keys=True))


def parent(baseline: str, candidate: str, runs: int):
    here = str(Path(__file__).resolve())
    series = {"baseline": [], "candidate": []}
    for i in range(runs):
        order = (("baseline", baseline), ("candidate", candidate)) if i % 2 == 0 else (("candidate", candidate), ("baseline", baseline))
        for name, path in order:
            env = dict(__import__("os").environ)
            env["PYTHONPATH"] = str(Path(path) / "src")
            cp = subprocess.run([sys.executable, here, "--child"], env=env, text=True, capture_output=True, check=True)
            series[name].append(json.loads(cp.stdout.strip().splitlines()[-1]))
    keys = series["baseline"][0].keys()
    out = {"runs": runs, "baseline": {}, "candidate": {}, "speedups": {}}
    for key in keys:
        b = statistics.median(item[key] for item in series["baseline"])
        c = statistics.median(item[key] for item in series["candidate"])
        out["baseline"][key] = b
        out["candidate"][key] = c
        if key.endswith("_ns") or key.endswith("_bytes"):
            out["speedups"][key] = b / c if c else None
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--candidate")
    ap.add_argument("--runs", type=int, default=13)
    ns=ap.parse_args()
    if ns.child:
        child()
    else:
        parent(ns.baseline, ns.candidate, ns.runs)
