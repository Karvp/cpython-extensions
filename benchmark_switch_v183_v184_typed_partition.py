from __future__ import annotations
import argparse,json,statistics,subprocess,sys,timeit,os
from pathlib import Path
HERE = Path(__file__).resolve().parent
BASE = os.environ.get('PYSWITCH_BASE_0183', '')
NEW = str((HERE / 'src').resolve())

def compile_fn(e,s,c,src,typed=True):
 ns={'switch':s,'case':c};exec(compile(src,'<typed-partition-bench>','exec'),ns)
 return e(mode='portable',case_key_mode='typed' if typed else 'python',source=src)(ns['f'])

def direct(e,s,c,n=16,typed=True,keykind='int'):
 lines=['def f(x):','    with switch(x):']
 for i in range(n):
  k=i if keykind=='int' else f'k{i}'
  lines += [f'        if case({k!r}):',f'            return {1000+i}']
 lines += ['        if case():','            return -1']
 return compile_fn(e,s,c,'\n'.join(lines)+'\n',typed)

def expr(e,s,c,n=16,typed=True):
 lines=['def f(x):','    with switch(x):']
 for i in range(n): lines += [f'        if case({i}):',f'            return x + {1000+i}']
 lines += ['        if case():','            return x + -1']
 return compile_fn(e,s,c,'\n'.join(lines)+'\n',typed)

def stmt(e,s,c,n=16,typed=True):
 lines=['def f(x):','    with switch(x):']
 for i in range(n): lines += [f'        if case({i}):',f'            y = x + {1000+i}','            y *= 2','            return y']
 lines += ['        if case():','            y = x + -1','            y *= 2','            return y']
 return compile_fn(e,s,c,'\n'.join(lines)+'\n',typed)

def balanced(e,s,c,n=16,typed=True):
 lines=['def f(x, flag):','    with switch(x):']
 for i in range(n):
  if i==0:
   lines += [f'        if case({i}, when=flag):',f'            return {1000+i}',f'        if case({i}):',f'            return {2000+i}']
  else: lines += [f'        if case({i}):',f'            return {1000+i}']
 lines += ['        if case():','            return -1']
 return compile_fn(e,s,c,'\n'.join(lines)+'\n',typed)

def mixed(e,s,c):
 src='''def f(x):
    with switch(x):
        if case(1): return 1001
        if case(1.0): return 1002
        if case(True): return 1003
        if case("1"): return 1004
        if case(b"1"): return 1005
        if case(): return -1
'''
 return compile_fn(e,s,c,src,True)

def measure(fn,args,target=180_000):
 def run():
  z=0
  for a in args:
   y=fn(*a)
   if isinstance(y,(int,float)):z+=y
  return z
 loops=max(1,target//len(args));return min(timeit.repeat(run,number=loops,repeat=3))*1e9/(loops*len(args))

def child(root):
 sys.path.insert(0,root);from python_extensions import enable_switch,switch,case
 typed_vals=[(0,),(8,),(15,),(20,),(True,),(1.0,)]*8
 miss_vals=[(20,),(True,),(1.0,),('x',),([],)]*8
 hit_vals=[(0,),(8,),(15,)]*16
 str_vals=[('k0',),('k8',),('k15',),('miss',),(1,)]*8
 bal_vals=[(0,True),(0,False),(8,False),(15,True),(20,False),(True,True),(1.0,False)]*8
 mix_vals=[(1,),(1.0,),(True,),('1',),(b'1',),(False,),(2,)]*8
 specs={
  'typed_direct16_mix':(direct(enable_switch,switch,case,16,True),typed_vals),
  'typed_direct16_hit':(direct(enable_switch,switch,case,16,True),hit_vals),
  'typed_direct16_miss':(direct(enable_switch,switch,case,16,True),miss_vals),
  'typed_direct64_mix':(direct(enable_switch,switch,case,64,True),[(0,),(31,),(63,),(80,),(True,),(1.0,)]*8),
  'typed_str16':(direct(enable_switch,switch,case,16,True,'str'),str_vals),
  'typed_expr16':(expr(enable_switch,switch,case,16,True),typed_vals),
  'typed_stmt16':(stmt(enable_switch,switch,case,16,True),typed_vals),
  'typed_balanced16':(balanced(enable_switch,switch,case,16,True),bal_vals),
  'typed_mixed5':(mixed(enable_switch,switch,case),mix_vals),
  'python_direct64':(direct(enable_switch,switch,case,64,False),[(0,),(31,),(63,),(80,)]*8),
  'python_expr64':(expr(enable_switch,switch,case,64,False),[(0,),(31,),(63,),(80,)]*8),
 }
 out={}
 for name,(fn,args) in specs.items():
  out[name]={'ns':measure(fn,args),'backend':fn.__pyswitch_backend__,'partitions':getattr(fn,'__pyswitch_typed_partition_plan_count__',0),'stack':getattr(fn,'__pyswitch_stack_payload_plan_count__',0)}
 if root==NEW:
  tt={(int,i):1000+i for i in range(16)};tg=tt.get
  def tuple_ref(x):return tg((type(x),x),-1)
  raw={i:1000+i for i in range(16)};rg=raw.get
  def partition_ref(x):
   if type(x) is int:return rg(x,-1)
   try:hash(x)
   except TypeError:
    if type(x).__hash__ is None:return -1
    raise
   return -1
  out['refs']={'tuple_ns':measure(tuple_ref,typed_vals),'partition_ns':measure(partition_ref,typed_vals)}
 print(json.dumps(out))

def parent(p):
 if not BASE: raise SystemExit("set PYSWITCH_BASE_0183 to the 0.18.3 source directory before running this historical comparison")
 rows={}
 for label,root in [('0.18.3',BASE),('candidate',NEW)]:
  rows[label]=[json.loads(subprocess.check_output([sys.executable,__file__,'--child',root],text=True)) for _ in range(p)]
 out={'python':sys.version.split()[0],'processes':p,'scenarios':{}}
 for name in rows['0.18.3'][0]:
  if name=='refs':continue
  a=statistics.median(r[name]['ns'] for r in rows['0.18.3']);b=statistics.median(r[name]['ns'] for r in rows['candidate'])
  out['scenarios'][name]={'old_ns':a,'new_ns':b,'speedup':a/b,'partition':rows['candidate'][0][name]['partitions'],'stack':rows['candidate'][0][name]['stack']}
 out['references']={k:statistics.median(r['refs'][k] for r in rows['candidate']) for k in rows['candidate'][0]['refs']}
 print(json.dumps(out,indent=2))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--child');ap.add_argument('--processes',type=int,default=5);a=ap.parse_args();child(a.child) if a.child else parent(a.processes)
