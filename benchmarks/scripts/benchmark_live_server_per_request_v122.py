from __future__ import annotations
import argparse,gc,json,random,statistics,time,os,sys
from python_extensions import enable_switch,switch,case

def source(n=64):
 l=['def route(selector,payload):','    with switch(selector):']
 for i in range(n):
  l += [f"        if case('/api/v2/r{i}'):",f'            return ((payload * {i%7+1}) + {i+200}) & 0xffffffff']
 l += ['        if case():','            return 404']
 return '\n'.join(l)+'\n'
def compile(src,mode,engine=None):
 ns={'switch':switch,'case':case};exec(compile_builtin(src,'<server-per-request>','exec'),ns)
 kw={'mode':mode,'source':src};
 if engine:kw['live_engine']=engine
 return enable_switch(**kw)(ns['route'])
compile_builtin=__builtins__['compile'] if isinstance(__builtins__,dict) else __builtins__.compile

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--seed',type=int,default=1);a=ap.parse_args(); src=source()
 fns={'portable':compile(src,'portable'),'fast_native':compile(src,'fast','native'),'thread_local_native':compile(src,'thread_local','native')}
 rng=random.Random(a.seed); data=[(f'/api/v2/r{rng.randrange(64)}',rng.randrange(1,1000)) for _ in range(4096)]
 assert [f(*data[0]) for f in fns.values()].count(fns['portable'](*data[0]))==len(fns)
 out={'seed':a.seed,'telemetry':{k:{'backend':getattr(f,'__pyswitch_backend__',None),'mode':getattr(f,'__pyswitch_mode__',None),'engine':getattr(f,'__pyswitch_live_engine__',None)} for k,f in fns.items()},'timing':{}}
 for name,f in fns.items():
  for _ in range(3):
   for x in data[:128]:f(*x)
  vals=[]
  for _ in range(5):
   t=time.perf_counter_ns(); s=0
   for _ in range(50):
    for x in data:s^=f(*x)
   vals.append(time.perf_counter_ns()-t)
  calls=50*len(data); ns=[v/calls for v in vals];out['timing'][name]={'median_ns_per_request':statistics.median(ns),'samples':ns,'checksum':s}
 print(json.dumps(out))
if __name__=='__main__':main()
