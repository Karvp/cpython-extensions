from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKER = ROOT / "benchmarks" / "benchmark_all_extensions_v22_worker.py"

def run_one(source_root: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root / "src")
    out = subprocess.check_output([sys.executable, str(WORKER)], env=env, text=True)
    return json.loads(out)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, type=Path)
    ap.add_argument("--candidate", type=Path, default=ROOT)
    ap.add_argument("--processes", type=int, default=7)
    args=ap.parse_args()
    rows={"baseline":[],"candidate":[]}
    for i in range(args.processes):
        order=("baseline","candidate") if i%2==0 else ("candidate","baseline")
        for name in order:
            root=args.baseline if name=="baseline" else args.candidate
            rows[name].append(run_one(root))
    keys=rows["baseline"][0]["results_ns"]
    summary={}
    for key in keys:
        b=statistics.median(r["results_ns"][key] for r in rows["baseline"])
        c=statistics.median(r["results_ns"][key] for r in rows["candidate"])
        summary[key]={"baseline_ns":b,"candidate_ns":c,"speedup":b/c}
    payload={
        "python":sys.version,
        "processes":args.processes,
        "baseline":str(args.baseline),
        "candidate":str(args.candidate),
        "summary":summary,
        "baseline_meta":rows["baseline"][0]["meta"],
        "candidate_meta":rows["candidate"][0]["meta"],
        "raw":rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

if __name__=="__main__": main()
