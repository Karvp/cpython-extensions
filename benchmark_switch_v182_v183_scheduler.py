from __future__ import annotations
import argparse,json,os,statistics,subprocess,sys,timeit
from pathlib import Path

HERE=Path(__file__).resolve().parent
BASE=os.environ.get("PYSWITCH_BASE_0182", "")
CAND=str((HERE/"src").resolve())

def compile_fn(e,s,c,src,*,typed=False,extra=None):
 ns={'switch':s,'case':c}; ns.update(extra or {}); exec(compile(src,'<v183-bench>','exec'),ns)
 return e(mode='portable',case_key_mode='typed' if typed else 'python',source=src)(ns['f'])

def cases(expr,default,n=64,params='x',extra=None,typed=False):
 def make(e,s,c):
  lines=[f'def f({params}):','    with switch(x):']
  for i in range(n): lines += [f'        if case({i}):',f'            return {expr(i)}']
  lines += ['        if case():',f'            return {default}']
  return compile_fn(e,s,c,'\n'.join(lines)+'\n',typed=typed,extra=extra)
 return make

def balanced(e,s,c,n=256):
 lines=['def f(x, flag):','    with switch(x):']
 for i in range(n):
  lines += [f'        if case({i}, when=flag):',f'            return {1000+i}',f'        elif case({i}):',f'            return {-1000-i}']
 lines += ['        if case():','            return -1']
 return compile_fn(e,s,c,'\n'.join(lines)+'\n')

def direct(e,s,c,n=64,typed=False):
 return cases(lambda i:str(1000+i),'-1',n=n,typed=typed)(e,s,c)

def measure(fn,args,target=120_000):
 def run():
  total=0
  for a in args:
   v=fn(*a)
   if isinstance(v,int): total += v
   elif isinstance(v,(tuple,list)): total += len(v)
   elif isinstance(v,str): total += len(v)
  return total
 loops=max(1,target//len(args))
 for i in range(2000): fn(*args[i%len(args)])
 values=timeit.repeat(run,number=loops,repeat=2)
 return min(values)*1e9/(loops*len(args))

def child(root):
 sys.path.insert(0,root)
 from python_extensions import enable_switch,switch,case
 def probe1(a): return a*3+1
 def probe2(a,b): return a*5+b*7
 common=[(0,),(15,),(31,),(63,),(90,)]*12
 typed_args=[(0,),(3,),(7,),(15,),(30,),(True,),(1.0,)]*10
 scenarios={
  'direct64':(direct(enable_switch,switch,case),common),
  'right_add64':(cases(lambda i:f'x + {1000+i}','x + 5000')(enable_switch,switch,case),common),
  'left_add64':(cases(lambda i:f'{1000+i} + x','5000 + x')(enable_switch,switch,case),common),
  'multi_expr64':(cases(lambda i:f'x * {i%7+2} + {1000+i*3} - {i%5+1}','x * 11 + 9000 - 7')(enable_switch,switch,case),common),
  'statement64':(None,None),
  'tuple_left64':(cases(lambda i:f'({1000+i}, x)','(5000, x)')(enable_switch,switch,case),common),
  'tuple_right64':(cases(lambda i:f'(x, {1000+i})','(x, 5000)')(enable_switch,switch,case),common),
  'call1_64':(cases(lambda i:f'probe1({1000+i})','probe1(5000)',extra={'probe1':probe1})(enable_switch,switch,case),common),
  'call2_64':(cases(lambda i:f'probe2(x, {1000+i})','probe2(x, 5000)',extra={'probe2':probe2})(enable_switch,switch,case),common),
  'identical64':(cases(lambda i:'x + 10','x + 10')(enable_switch,switch,case),common),
  'typed16':(direct(enable_switch,switch,case,n=16,typed=True),typed_args),
  'balanced256':(balanced(enable_switch,switch,case),[(0,True),(63,False),(127,True),(255,False),(300,True)]*12),
 }
 # Build statement separately.
 lines=['def f(x):','    with switch(x):']
 for i in range(64): lines += [f'        if case({i}):',f'            y=x+{1000+i}','            z=y*2','            return z+3']
 lines += ['        if case():','            y=x+5000','            z=y*2','            return z+3']
 st=compile_fn(enable_switch,switch,case,'\n'.join(lines)+'\n')
 scenarios['statement64']=(st,common)
 out={}
 for name,(fn,args) in scenarios.items():
  out[name]={'ns':measure(fn,args),'bytes':len(fn.__code__.co_code),'stack':fn.__code__.co_stacksize,'backend':getattr(fn,'__pyswitch_backend__','?')}
 print(json.dumps(out))

def refs():
 table={i:1000+i for i in range(64)}
 def d(x): return table.get(x,-1)
 lines=['def f(x):']+[('    if' if i==0 else '    elif')+f' x == {i}: return {1000+i}' for i in range(64)]+['    return -1']
 ns={};exec('\n'.join(lines)+'\n',ns);iff=ns['f']
 lines=['def f(x):','    match x:']
 for i in range(64):lines += [f'        case {i}:',f'            return {1000+i}']
 lines += ['        case _:','            return -1']; ns={};exec('\n'.join(lines)+'\n',ns);mat=ns['f']
 args=[(0,),(15,),(31,),(63,),(90,)]*12
 print(json.dumps({k:measure(v,args) for k,v in {'dict_get64':d,'if_elif64':iff,'match64':mat}.items()}))

def parent(n):
 if not BASE: raise SystemExit("set PYSWITCH_BASE_0182 to the 0.18.2 source directory before running this historical comparison")
 runs={'0.18.2':[],'candidate':[]}
 for label,root in [('0.18.2',BASE),('candidate',CAND)]:
  for _ in range(n):runs[label].append(json.loads(subprocess.check_output([sys.executable,__file__,'--child',root],text=True,cwd='/tmp')))
 ref_runs=[json.loads(subprocess.check_output([sys.executable,__file__,'--refs'],text=True,cwd='/tmp')) for _ in range(n)]
 report={'python':sys.version.split()[0],'processes':n,'scenarios':{},'references_ns':{}}
 for sc in runs['0.18.2'][0]:
  old=[r[sc]['ns'] for r in runs['0.18.2']];new=[r[sc]['ns'] for r in runs['candidate']]
  om=statistics.median(old);nm=statistics.median(new)
  report['scenarios'][sc]={'v182_ns':om,'candidate_ns':nm,'speedup':om/nm,'v182_samples_ns':old,'candidate_samples_ns':new,'v182_bytes':runs['0.18.2'][0][sc]['bytes'],'candidate_bytes':runs['candidate'][0][sc]['bytes'],'backend':runs['candidate'][0][sc]['backend']}
 for name in ref_runs[0]:
  vals=[r[name] for r in ref_runs];report['references_ns'][name]={'median':statistics.median(vals),'samples':vals}
 print(json.dumps(report,indent=2))

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--child');ap.add_argument('--refs',action='store_true');ap.add_argument('--processes',type=int,default=5);a=ap.parse_args()
 if a.child:child(a.child)
 elif a.refs:refs()
 else:parent(a.processes)
