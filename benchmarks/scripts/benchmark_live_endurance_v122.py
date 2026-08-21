from __future__ import annotations
import json, os, statistics, subprocess, sys, time, math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; WORKER=ROOT/'benchmarks/scripts/benchmark_live_workloads_v122_worker.py'
CONFIGS=[
 ('vm_dense',256,'random'),('vm_dense',1024,'skewed'),('protocol_sparse',256,'random'),('state_machine',128,'random'),
 ('http_server',64,'skewed'),('rpc_typed',64,'random'),('server_heavy',64,'skewed'),('event_mixed',128,'skewed'),('direct_control',256,'random')]
def run(cfg,seed):
 kind,routes,pattern=cfg; env=os.environ.copy(); env['PYTHONPATH']=str(ROOT/'src')
 cmd=[sys.executable,str(WORKER),'--kind',kind,'--routes',str(routes),'--pattern',pattern,'--seed',str(seed),'--target','300000','--repeats','5','--length','512','--keep-gc']
 p=subprocess.run(cmd,cwd=ROOT,env=env,text=True,capture_output=True,check=True); return json.loads(p.stdout)
def main():
 rows=[]; raw=[]; t0=time.time()
 for i,cfg in enumerate(CONFIGS,1):
  ps=[run(cfg,s) for s in (1,2,3)]; raw+=ps
  kind,routes,pattern=cfg; r={'kind':kind,'routes':routes,'pattern':pattern,'processes':3,'gc_enabled':True,'dispatches_per_sample':ps[0]['dispatches_per_sample'],'repeats_per_process':5}
  for b in ('portable','ctypes','native'):
   pm=[x['backends'][b]['timing']['median_ns_per_dispatch'] for x in ps]
   r[b+'_ns']=statistics.median(pm)
   allsamples=[v for x in ps for v in x['backends'][b]['timing']['samples_ns_per_dispatch']]
   r[b+'_sample_cv_pct']=statistics.pstdev(allsamples)/statistics.mean(allsamples)*100
   r[b+'_first_last_ratio']=statistics.median([x['backends'][b]['timing']['samples_ns_per_dispatch'][-1]/x['backends'][b]['timing']['samples_ns_per_dispatch'][0] for x in ps])
  r['native_vs_portable']=r['portable_ns']/r['native_ns']; r['native_vs_ctypes']=r['ctypes_ns']/r['native_ns']; rows.append(r)
  print(f'[{i}/{len(CONFIGS)}] {kind} {routes} {pattern}: p={r["portable_ns"]:.1f} c={r["ctypes_ns"]:.1f} n={r["native_ns"]:.1f} n/p={r["native_vs_portable"]:.2f}x cv={r["native_sample_cv_pct"]:.1f}%',flush=True)
 out={'schema':'cpython-extensions-live-endurance-v122','elapsed_s':time.time()-t0,'rows':rows,'raw':raw}
 path=ROOT/'benchmarks/results/BENCHMARK_LIVE_ENDURANCE_V122.json'; path.write_text(json.dumps(out,indent=2),encoding='utf-8'); print('RESULT',path)
if __name__=='__main__':main()
