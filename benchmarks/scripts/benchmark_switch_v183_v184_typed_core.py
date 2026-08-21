from __future__ import annotations
import argparse,json,statistics,subprocess,sys,timeit,os
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1];BASE=os.environ.get('PYSWITCH_BASE_0183','');NEW=str((ROOT/'src').resolve())
def cf(e,s,c,src):
 ns={'switch':s,'case':c};exec(compile(src,'<core>','exec'),ns);return e(mode='portable',case_key_mode='typed',source=src)(ns['f'])
def src(kind,n=16):
 L=['def f(x):' if kind!='bal' else 'def f(x, flag):','    with switch(x):']
 for i in range(n):
  if kind=='lit':B=[f'return {1000+i}']
  elif kind=='expr':B=[f'return x + {1000+i}']
  elif kind=='stmt':B=[f'y = x + {1000+i}','y *= 2','return y']
  elif kind=='bal':
   if i==0:
    L += [f'        if case(0, when=flag):','            return 1000','        if case(0):','            return 2000'];continue
   B=[f'return {1000+i}']
  L += [f'        if case({i}):']+[f'            {z}' for z in B]
 L += ['        if case():']
 if kind=='expr':L += ['            return x + -1']
 elif kind=='stmt':L += ['            y = x + -1','            y *= 2','            return y']
 else:L += ['            return -1']
 return '\n'.join(L)+'\n'
def meas(f,args,target=900_000):
 def run():
  z=0
  for a in args:z+=f(*a)
  return z
 q=max(1,target//len(args));return min(timeit.repeat(run,number=q,repeat=3))*1e9/(q*len(args))
def child(root):
 sys.path.insert(0,root);from python_extensions import enable_switch,switch,case
 vals=[(0,),(8,),(15,),(20,),(True,),(1.0,)]*8; bal=[(0,True),(0,False),(8,False),(15,True),(20,False),(True,True),(1.0,False)]*8
 out={}
 for k in ['lit','expr','stmt','bal']:
  f=cf(enable_switch,switch,case,src(k));out[k]={'ns':meas(f,bal if k=='bal' else vals),'p':getattr(f,'__pyswitch_typed_partition_plan_count__',0),'stack':getattr(f,'__pyswitch_stack_payload_plan_count__',0)}
 print(json.dumps(out))
def parent(p):
 if not BASE: raise SystemExit("set PYSWITCH_BASE_0183 to the 0.18.3 source directory before running this historical comparison")
 rows={l:[json.loads(subprocess.check_output([sys.executable,__file__,'--child',r],text=True)) for _ in range(p)] for l,r in [('old',BASE),('new',NEW)]}
 o={}
 for k in rows['old'][0]:
  a=statistics.median(x[k]['ns'] for x in rows['old']);b=statistics.median(x[k]['ns'] for x in rows['new']);o[k]={'old_ns':a,'new_ns':b,'speedup':a/b,'old_samples':[x[k]['ns'] for x in rows['old']],'new_samples':[x[k]['ns'] for x in rows['new']]}
 print(json.dumps({'python':sys.version.split()[0],'processes':p,'scenarios':o},indent=2))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--child');ap.add_argument('--processes',type=int,default=7);a=ap.parse_args();child(a.child) if a.child else parent(a.processes)
