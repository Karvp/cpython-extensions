from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, timeit
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD_ROOT = os.environ.get("PYSWITCH_BASE_0180", "")
NEW_ROOT = str((HERE / "src").resolve())


def compile_fn(enable_switch, switch, case, source, name="f", **kwargs):
    ns={"switch":switch,"case":case}
    exec(compile(source,"<pyswitch-hardening-bench>","exec"),ns)
    return enable_switch(mode="portable", source=source, **kwargs)(ns[name])


def make_direct(enable_switch,switch,case,n=64):
    lines=["def f(x):","    with switch(x):"]
    for i in range(n): lines += [f"        if case({i}):",f"            return {i+1000}"]
    lines += ["        if case():","            return -1"]
    return compile_fn(enable_switch,switch,case,"\n".join(lines)+"\n")


def make_direct_assign(enable_switch,switch,case,n=64):
    lines=["def f(x):","    y = -9","    with switch(x):"]
    for i in range(n): lines += [f"        if case({i}):",f"            y = {i+1000}"]
    lines += ["        if case():","            y = -1","    return y"]
    return compile_fn(enable_switch,switch,case,"\n".join(lines)+"\n")


def make_template_assign(enable_switch,switch,case,n=64):
    lines=["def f(x):","    y = -9","    with switch(x):"]
    for i in range(n): lines += [f"        if case({i}):",f"            y = x + {i+1000}"]
    lines += ["        if case():","            y = -1","    return y"]
    return compile_fn(enable_switch,switch,case,"\n".join(lines)+"\n")


def make_statement_assign(enable_switch,switch,case,n=64):
    lines=["def f(x):","    y = -9","    z = -9","    with switch(x):"]
    for i in range(n): lines += [f"        if case({i}):",f"            y = x + {i+1000}","            z = y * 3"]
    lines += ["        if case():","            y = -1","            z = -3","    return y + z"]
    return compile_fn(enable_switch,switch,case,"\n".join(lines)+"\n")


def make_balanced(enable_switch,switch,case,n=256):
    lines=["def f(x, flag):","    with switch(x):"]
    for i in range(n):
        lines += [f"        if case({i}, when=flag):",f"            return {i+1000}",f"        elif case({i}):",f"            return {-i-1000}"]
    lines += ["        if case():","            return -1"]
    return compile_fn(enable_switch,switch,case,"\n".join(lines)+"\n")


def measure(fn,args,target=180_000):
    def run():
        s=0
        for a in args:
            r=fn(*a)
            if isinstance(r,int): s += r
        return s
    loops=max(1,target//len(args))
    vals=timeit.repeat(run,number=loops,repeat=3)
    return min(vals)*1e9/(loops*len(args))


def child(root):
    sys.path.insert(0,root)
    from python_extensions import enable_switch,switch,case
    funcs={
        "direct64":(make_direct(enable_switch,switch,case),[(0,),(31,),(63,),(90,)]*8),
        "direct_assign64":(make_direct_assign(enable_switch,switch,case),[(0,),(31,),(63,),(90,)]*8),
        "template_assign64":(make_template_assign(enable_switch,switch,case),[(0,),(31,),(63,),(90,)]*8),
        "statement_assign64":(make_statement_assign(enable_switch,switch,case),[(0,),(31,),(63,),(90,)]*8),
        "balanced256":(make_balanced(enable_switch,switch,case),[(0,True),(127,False),(255,True),(300,False)]*8),
    }
    out={}
    for name,(fn,args) in funcs.items():
        out[name]={"ns":measure(fn,args),"backend":getattr(fn,"__pyswitch_backend__","?")}
    print(json.dumps(out))


def parent(processes):
    if not OLD_ROOT:
        raise SystemExit("set PYSWITCH_BASE_0180 to the 0.18.0 source directory before running this historical comparison")
    runs={"0.18.0":[],"hardened":[]}
    for label,root in (("0.18.0",OLD_ROOT),("hardened",NEW_ROOT)):
        for _ in range(processes):
            raw=subprocess.check_output([sys.executable,__file__,"--child",root],text=True)
            runs[label].append(json.loads(raw))
    report={"python":sys.version.split()[0],"processes":processes,"scenarios":{}}
    for name in runs["0.18.0"][0]:
        old=[r[name]["ns"] for r in runs["0.18.0"]]
        new=[r[name]["ns"] for r in runs["hardened"]]
        report["scenarios"][name]={
            "v18_ns":statistics.median(old),"hardened_ns":statistics.median(new),
            "hardened_vs_v18":statistics.median(old)/statistics.median(new),
            "backend":runs["hardened"][0][name]["backend"],
            "v18_samples_ns":old,"hardened_samples_ns":new,
        }
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--child"); ap.add_argument("--processes",type=int,default=5); a=ap.parse_args()
    child(a.child) if a.child else parent(a.processes)
