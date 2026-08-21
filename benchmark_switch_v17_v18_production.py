from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, timeit
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD_ROOT = os.environ.get("PYSWITCH_BASE_017", "")
NEW_ROOT = str((HERE / "src").resolve())


def build_switch(enable_switch, switch, case, n: int, kind: str, typed: bool = False):
    lines = ["def f(x):", "    with switch(x):"]
    for i in range(n):
        lines.append(f"        if case({i}):")
        k = i + 1000
        if kind == "literal":
            lines.append(f"            return {k}")
        elif kind in {"template_full", "template_partial"}:
            lines.append(f"            return x + {k}")
        elif kind == "statement":
            lines += [f"            y = x + {k}", "            y *= 2", "            return y"]
        elif kind == "heterogeneous":
            if i % 3 == 0:
                lines.append(f"            return x + {k}")
            elif i % 3 == 1:
                lines += [f"            y = x * {k}", "            return y"]
            else:
                lines += ["            if x & 1:", f"                return {k}", f"            return {-k}"]
        else:
            raise ValueError(kind)
    lines += ["        if case():"]
    if kind == "template_full":
        lines.append("            return x + -1")
    else:
        lines.append("            return -1")
    source = "\n".join(lines) + "\n"
    ns = {"switch": switch, "case": case}
    exec(compile(source, f"<bench-{n}-{kind}>", "exec"), ns)
    return enable_switch(mode="portable", source=source,
                         case_key_mode="typed" if typed else "python")(ns["f"])


def build_if(n: int, kind: str):
    lines = ["def f(x):"]
    for i in range(n):
        lines.append(("    if" if i == 0 else "    elif") + f" x == {i}:")
        k = i + 1000
        if kind == "literal": lines.append(f"        return {k}")
        elif kind in {"template_full", "template_partial"}: lines.append(f"        return x + {k}")
        elif kind == "statement": lines += [f"        y=x+{k}", "        y*=2", "        return y"]
        elif kind == "heterogeneous":
            if i % 3 == 0: lines.append(f"        return x+{k}")
            elif i % 3 == 1: lines += [f"        y=x*{k}", "        return y"]
            else: lines += ["        if x & 1:", f"            return {k}", f"        return {-k}"]
    lines.append("    return x + -1" if kind == "template_full" else "    return -1")
    ns = {}; exec("\n".join(lines)+"\n", ns); return ns["f"]


def build_match(n: int, kind: str):
    lines = ["def f(x):", "    match x:"]
    for i in range(n):
        lines.append(f"        case {i}:")
        k=i+1000
        if kind == "literal": lines.append(f"            return {k}")
        elif kind in {"template_full", "template_partial"}: lines.append(f"            return x+{k}")
        elif kind == "statement": lines += [f"            y=x+{k}", "            y*=2", "            return y"]
        elif kind == "heterogeneous":
            if i%3==0: lines.append(f"            return x+{k}")
            elif i%3==1: lines += [f"            y=x*{k}", "            return y"]
            else: lines += ["            if x & 1:", f"                return {k}", f"            return {-k}"]
    lines += ["        case _:", "            return x + -1" if kind == "template_full" else "            return -1"]
    ns={}; exec("\n".join(lines)+"\n",ns); return ns["f"]


def measure(fn, values, target_calls=100_000):
    def run():
        total = 0
        for x in values:
            result = fn(x)
            if isinstance(result, int): total += result
        return total
    loops = max(1, target_calls // len(values))
    samples = timeit.repeat(run, number=loops, repeat=2)
    return min(samples) * 1e9 / (loops * len(values))


def child(root: str):
    sys.path.insert(0, root)
    from python_extensions import enable_switch, switch, case
    scenarios = [
        ("literal64", 64, "literal", False, [0, 31, 63, 80]),
        ("typed16", 16, "literal", True, [0, 8, 15, 20, True, 1.0]),
        ("template_full64", 64, "template_full", False, [0, 31, 63, 80]),
        ("template_partial64", 64, "template_partial", False, [0, 31, 63, 80]),
        ("statement64", 64, "statement", False, [0, 31, 63, 80]),
        ("heterogeneous64", 64, "heterogeneous", False, [0, 31, 63, 80]),
    ]
    out = {}
    for name,n,kind,typed,values in scenarios:
        fn=build_switch(enable_switch,switch,case,n,kind,typed)
        out[name] = {"ns": measure(fn, values*8), "backend": getattr(fn,"__pyswitch_backend__","?")}
    if root == NEW_ROOT:
        for name,n,kind,typed,values in scenarios:
            if typed: continue
            iff=build_if(n,kind); mat=build_match(n,kind)
            out[name]["if_ns"] = measure(iff, values*8)
            out[name]["match_ns"] = measure(mat, values*8)
        table={i:i+1000 for i in range(64)}
        def direct_dict(x, table=table): return table.get(x,-1)
        out["literal64"]["dict_ns"] = measure(direct_dict,[0,31,63,80]*8)
        typed_table={(int,i):i+1000 for i in range(16)}
        def typed_dict(x, table=typed_table): return table.get((type(x),x),-1)
        out["typed16"]["dict_ns"] = measure(typed_dict,[0,8,15,20,True,1.0]*8)
    print(json.dumps(out))


def parent(processes: int):
    if not OLD_ROOT:
        raise SystemExit("set PYSWITCH_BASE_017 to the 0.17 source directory before running this historical comparison")
    rows={}
    for label,root in (("0.17",OLD_ROOT),("0.18",NEW_ROOT)):
        runs=[]
        for _ in range(processes):
            raw=subprocess.check_output([sys.executable,__file__,"--child",root],text=True)
            runs.append(json.loads(raw))
        rows[label]=runs
    names=list(rows["0.18"][0])
    report={"python":sys.version.split()[0],"processes":processes,"scenarios":{}}
    for name in names:
        old=[r[name]["ns"] for r in rows["0.17"]]
        new=[r[name]["ns"] for r in rows["0.18"]]
        entry={
            "old_ns":statistics.median(old),
            "new_ns":statistics.median(new),
            "speedup":statistics.median(old)/statistics.median(new),
            "old_backend":rows["0.17"][0][name]["backend"],
            "new_backend":rows["0.18"][0][name]["backend"],
        }
        for baseline in ("dict_ns","if_ns","match_ns"):
            vals=[r[name][baseline] for r in rows["0.18"] if baseline in r[name]]
            if vals: entry[baseline]=statistics.median(vals)
        report["scenarios"][name]=entry
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--child"); ap.add_argument("--processes",type=int,default=5)
    args=ap.parse_args()
    child(args.child) if args.child else parent(args.processes)
