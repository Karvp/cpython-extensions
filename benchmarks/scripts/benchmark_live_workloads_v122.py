from __future__ import annotations
import json, os, statistics, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
WORKER=ROOT/'benchmarks/scripts/benchmark_live_workloads_v122_worker.py'
PY=sys.executable
MATRIX=[
 ('vm_dense',64,'random'),('vm_dense',256,'random'),('vm_dense',256,'skewed'),('vm_dense',1024,'random'),('vm_dense',1024,'skewed'),
 ('protocol_sparse',64,'random'),('protocol_sparse',256,'random'),('protocol_sparse',256,'skewed'),('protocol_sparse',1024,'random'),
 ('state_machine',32,'random'),('state_machine',128,'random'),('state_machine',512,'random'),
 ('parser_int',64,'random'),('parser_int',256,'random'),('parser_int',256,'skewed'),
 ('http_server',16,'random'),('http_server',64,'random'),('http_server',64,'skewed'),('http_server',256,'random'),('http_server',256,'skewed'),
 ('rpc_typed',16,'random'),('rpc_typed',64,'random'),('rpc_typed',64,'skewed'),('rpc_typed',256,'random'),
 ('server_heavy',64,'skewed'),('server_heavy',256,'random'),
 ('event_mixed',32,'random'),('event_mixed',128,'skewed'),
 ('direct_control',64,'random'),('direct_control',256,'random'),
]
def run_one(kind,routes,pattern,seed):
    env=os.environ.copy(); env['PYTHONPATH']=str(ROOT/'src')
    cmd=[PY,str(WORKER),'--kind',kind,'--routes',str(routes),'--pattern',pattern,'--seed',str(seed),'--target','250000','--repeats','3','--length','512']
    p=subprocess.run(cmd,cwd=ROOT,env=env,text=True,capture_output=True,check=True)
    return json.loads(p.stdout)
def main():
    started=time.time(); raw=[]; summary=[]
    for idx,(kind,routes,pattern) in enumerate(MATRIX,1):
        procs=[run_one(kind,routes,pattern,seed) for seed in (1,2,3)]
        raw.extend(procs)
        row={'kind':kind,'routes':routes,'pattern':pattern,'processes':len(procs),'dispatches_per_sample':procs[0]['dispatches_per_sample']}
        for label in ('portable','ctypes','native'):
            vals=[p['backends'][label]['timing']['median_ns_per_dispatch'] for p in procs]
            row[label+'_ns']=statistics.median(vals); row[label+'_process_medians']=vals
            row[label+'_compile_ms']=statistics.median([p['backends'][label]['compile_ms'] for p in procs])
        row['native_vs_portable']=row['portable_ns']/row['native_ns']; row['native_vs_ctypes']=row['ctypes_ns']/row['native_ns']; row['ctypes_vs_portable']=row['portable_ns']/row['ctypes_ns']
        row['portable_backend']=procs[0]['backends']['portable']['telemetry'].get('__pyswitch_backend__')
        row['native_strategy']=(procs[0]['backends']['native']['telemetry'].get('native_info') or [{}])[0].get('lookup_strategy')
        summary.append(row)
        print(f"[{idx:02d}/{len(MATRIX)}] {kind:15s} r={routes:<4d} {pattern:7s} portable={row['portable_ns']:.1f} ctypes={row['ctypes_ns']:.1f} native={row['native_ns']:.1f} speedup={row['native_vs_portable']:.2f}x",flush=True)
    out={'schema':'cpython-extensions-live-workloads-v122','python':sys.version,'elapsed_s':time.time()-started,'matrix':summary,'raw_processes':raw}
    path=ROOT/'benchmarks/results/BENCHMARK_LIVE_WORKLOADS_V122.json'; path.write_text(json.dumps(out,indent=2,default=str),encoding='utf-8')
    print('RESULT',path)
if __name__=='__main__':main()
