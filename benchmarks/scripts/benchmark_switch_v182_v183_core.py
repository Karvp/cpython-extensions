from __future__ import annotations
import argparse,json,os,statistics,subprocess,sys,timeit
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1];B=os.environ.get('PYSWITCH_BASE_0182','');C=str((ROOT/'src').resolve())
def comp(e,s,c,src):
 ns={'switch':s,'case':c};exec(compile(src,'<b>','exec'),ns);return e(mode='portable',source=src)(ns['f'])
def make(e,s,c,kind):
 L=['def f(x):','    with switch(x):']
 for i in range(64):
  if kind=='direct':body=f'return {1000+i}'
  elif kind=='right':body=f'return x + {1000+i}'
  elif kind=='left':body=f'return {1000+i} + x'
  elif kind=='multi':body=f'return x*{i%7+2}+{1000+i*3}-{i%5+1}'
  elif kind=='stmt':
   L += [f'        if case({i}):',f'            y=x+{1000+i}','            z=y*2','            return z+3'];continue
  L += [f'        if case({i}):','            '+body]
 if kind=='direct':default='return -1'
 elif kind=='right':default='return x + 5000'
 elif kind=='left':default='return 5000 + x'
 elif kind=='multi':default='return x*11+9000-7'
 else:
  L += ['        if case():','            y=x+5000','            z=y*2','            return z+3'];return comp(e,s,c,'\n'.join(L)+'\n')
 L += ['        if case():','            '+default]
 return comp(e,s,c,'\n'.join(L)+'\n')
def meas(fn):
 args=[0,15,31,63,90]*20
 def run():
  z=0
  for x in args:z+=fn(x)
  return z
 for _ in range(5000):run()
 n=3500; vals=timeit.repeat(run,number=n,repeat=3);return min(vals)*1e9/(n*len(args))
def child(root):
 sys.path.insert(0,root);from python_extensions import enable_switch,switch,case
 print(json.dumps({k:meas(make(enable_switch,switch,case,k)) for k in ['direct','right','left','multi','stmt']}))
def parent(n):
 if not B: raise SystemExit("set PYSWITCH_BASE_0182 to the 0.18.2 source directory before running this historical comparison")
 R={x:[] for x in ['base','cand']}
 for lab,root in [('base',B),('cand',C)]:
  for _ in range(n):R[lab].append(json.loads(subprocess.check_output([sys.executable,__file__,'--child',root],text=True,cwd='/tmp')))
 out={}
 for k in R['base'][0]:
  a=[r[k] for r in R['base']];b=[r[k] for r in R['cand']];am=statistics.median(a);bm=statistics.median(b);out[k]={'base':am,'cand':bm,'speedup':am/bm,'base_samples':a,'cand_samples':b}
 print(json.dumps(out,indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--child');p.add_argument('-n',type=int,default=7);a=p.parse_args();child(a.child) if a.child else parent(a.n)
