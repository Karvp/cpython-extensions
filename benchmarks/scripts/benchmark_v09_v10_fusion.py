from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, textwrap

PROGRAM = r'''
import json, statistics, timeit, dis
from python_extensions.inline import inline_calls, inline_function
VERSION_KIND = {kind!r}
@inline_function(register_only=True)
def inc(x): return x + 1
@inline_function(register_only=True)
def double(x): return x * 2
@inline_function(register_only=True)
def flag(): return True
@inline_function(register_only=True)
def choose(enabled, x):
    if enabled: return x + 1
    return x - 1

def decorate(mode, fn):
    if VERSION_KIND == "current":
        return inline_calls(fusion_strategy=mode)(fn)
    return inline_calls()(fn)

def dyn(x):
    a = inc(x)
    b = double(a)
    return b - 3

def const(x):
    enabled = flag()
    return choose(enabled, x)

safe = decorate("safe", dyn)
aggressive = decorate("aggressive", dyn) if VERSION_KIND == "current" else safe
constant = decorate("safe", const)

def measure(fn):
    for _ in range(100000): fn(10)
    vals=[]
    for _ in range(7):
        vals.append(timeit.timeit('fn(10)', globals={{'fn':fn}}, number=2000000)/2000000*1e9)
    return statistics.median(vals)
print(json.dumps({{
  'safe_ns': measure(safe), 'safe_code':len(safe.__code__.co_code), 'safe_locals':len(safe.__code__.co_varnames),
  'aggr_ns': measure(aggressive), 'aggr_code':len(aggressive.__code__.co_code), 'aggr_locals':len(aggressive.__code__.co_varnames),
  'const_ns': measure(constant), 'const_code':len(constant.__code__.co_code), 'const_locals':len(constant.__code__.co_varnames),
}}))
'''

def run(src, kind):
    env=os.environ.copy(); env['PYTHONPATH']=src
    p=subprocess.run([sys.executable,'-c',PROGRAM.format(kind=kind)],env=env,text=True,capture_output=True,check=True)
    return json.loads(p.stdout.strip().splitlines()[-1])

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--baseline-src',required=True); ap.add_argument('--current-src',required=True); ns=ap.parse_args()
    base=run(ns.baseline_src,'baseline'); cur=run(ns.current_src,'current')
    print(sys.version)
    print('baseline',base); print('current',cur)
    for key in ('safe','aggr','const'):
        b=base[f'{key}_ns']; c=cur[f'{key}_ns']
        print(f'{key}: {b:.2f} -> {c:.2f} ns  speedup {b/c:.3f}x  code {base[f"{key}_code"]}->{cur[f"{key}_code"]}  locals {base[f"{key}_locals"]}->{cur[f"{key}_locals"]}')
if __name__=='__main__': main()
