from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, tempfile, textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATE = HERE / "src"
BASELINE = Path(os.environ["PE_V200_SRC"]).resolve() if "PE_V200_SRC" in os.environ else None

CHILD = r'''
import json, sys, timeit
sys.path.insert(0, sys.argv[1])
from python_extensions import case, enable_goto, enable_switch, fallthrough, inline_calls, inline_function, switch


def closure_factory():
    @inline_function(register_only=True)
    def calc(x):
        return (x + 1) * (x + 2) + (x + 3)
    target = calc
    @inline_calls(policy="always")
    def caller(x):
        return target(x)
    return caller


def tiny_factory():
    @inline_function(register_only=True)
    def add1(x):
        return x + 1
    target = add1
    @inline_calls(policy="speed")
    def caller(x):
        return target(x)
    return caller

@enable_switch(mode="portable")
def switch_default(v, out):
    with switch(v):
        if case(1):
            out.append("one")
            fallthrough()
        if case():
            try:
                out.append("a"); out.append("b"); out.append("c")
            finally:
                out.append("done")
    return len(out)

@enable_goto
def goto_loop(v):
    total = 0
    label .again
    if v <= 0:
        goto .done
    total += v
    v -= 1
    goto .again
    label .done
    return total

medium = closure_factory(); tiny = tiny_factory()
assert medium(5) == 50 and tiny(5) == 6 and goto_loop(6) == 21

def sw():
    out=[]; return switch_default(1,out)

def bench(expr, n):
    return timeit.timeit(expr, globals=globals(), number=n) * 1e9 / n

print(json.dumps({
    "medium_guard_ns": bench("medium(5)", 400000),
    "tiny_speed_ns": bench("tiny(5)", 500000),
    "switch_default_ns": bench("sw()", 250000),
    "goto_ns": bench("goto_loop(6)", 250000),
    "medium_size": len(medium.__code__.co_code),
    "tiny_size": len(tiny.__code__.co_code),
    "medium_inlined": medium.__inline_stats__.calls_inlined,
    "tiny_inlined": tiny.__inline_stats__.calls_inlined,
}))
'''


def child(path: Path) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(CHILD); name=f.name
    try:
        out=subprocess.check_output([sys.executable, name, str(path)], text=True)
        return json.loads(out)
    finally:
        Path(name).unlink(missing_ok=True)


def auto_size() -> dict:
    source = textwrap.dedent('''
        from python_extensions import case, enable_switch, fallthrough, switch
        @enable_switch(mode="portable", compact_routes="auto")
        def f(v, out):
            with switch(v):
                if case(1):
                    out.append("one")
                    fallthrough()
                if case():
                    try:
                        out.append("a")
                        out.append("b")
                        out.append("c")
                    finally:
                        out.append("done")
            return tuple(out)
        print(len(f.__code__.co_code), f.__pyswitch_shared_continuation_plan_count__)
    ''')
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(source); name=f.name
    try:
        env=dict(os.environ); env["PYTHONPATH"]=str(CANDIDATE)
        out=subprocess.check_output([sys.executable, name], text=True, env=env).split()
        return {"code_bytes": int(out[0]), "plans": int(out[1])}
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    if BASELINE is None:
        raise SystemExit("set PE_V200_SRC to the 0.20.0 source directory before running this historical comparison")
    ap=argparse.ArgumentParser(); ap.add_argument("--processes", type=int, default=7); args=ap.parse_args()
    rows={"baseline":[],"candidate":[]}
    for i in range(args.processes):
        order=(("baseline",BASELINE),("candidate",CANDIDATE)) if i%2==0 else (("candidate",CANDIDATE),("baseline",BASELINE))
        for label,path in order: rows[label].append(child(path))
    medians={label:{k:statistics.median(r[k] for r in data) for k in data[0]} for label,data in rows.items()}
    ratios={k:medians["baseline"][k]/medians["candidate"][k] for k in ("medium_guard_ns","tiny_speed_ns","switch_default_ns","goto_ns")}
    result={"processes":args.processes,"baseline_src":str(BASELINE),"candidate_src":str(CANDIDATE),"medians":medians,"baseline_over_candidate_speedup":ratios,"auto_compact":auto_size(),"samples":rows}
    print(json.dumps(result,indent=2))

if __name__ == "__main__": main()
