from __future__ import annotations

import argparse, gc, json, math, os, random, statistics, sys, time
from typing import Any

from python_extensions import case, enable_switch, switch

MASK = (1 << 64) - 1


def keys_for(kind: str, routes: int):
    if kind in {'vm_dense','state_machine','parser_int','direct_control'}:
        return list(range(routes))
    if kind == 'protocol_sparse':
        return [17 + i * 1009 for i in range(routes)]
    if kind in {'http_server','rpc_typed','server_heavy'}:
        return [f'/api/v2/r{i}' if kind != 'rpc_typed' else f'method.{i}' for i in range(routes)]
    if kind == 'event_mixed':
        out=[]
        for i in range(routes):
            out.append(i if i % 2 == 0 else f'evt:{i}')
        return out
    raise ValueError(kind)


def build_source(kind: str, keys: list[Any]) -> tuple[str,str]:
    typed = kind in {'rpc_typed','event_mixed'}
    kmode = 'typed' if typed else 'python'
    lines = [
        'def workload(sequence, rounds):',
        '    acc = 0x123456789ABCDEF',
        '    state = 0x9E3779B97F4A7C15',
        '    cursor = 7',
        '    for _ in range(rounds):',
    ]
    if kind == 'state_machine':
        lines += ['        for stimulus in sequence:', '            with switch(state % ROUTES):']
    else:
        lines += ['        for item in sequence:', '            selector, payload = item', '            with switch(selector):']
    for i,k in enumerate(keys):
        lines.append(f'                if case({k!r}):')
        c=(i*17+11)&0xffff
        if kind == 'vm_dense':
            op=i & 7
            if op==0: lines += [f'                    acc = (acc + payload + {c}) & MASK', '                    state ^= acc & 0xffff']
            elif op==1: lines += [f'                    state = (state + payload * {i%5+1} + {c}) & MASK', '                    acc ^= state >> 7']
            elif op==2: lines += [f'                    acc = ((acc << 1) ^ payload ^ {c}) & MASK', '                    cursor += 1']
            elif op==3: lines += [f'                    state = ((state >> 1) + acc + {c}) & MASK', '                    cursor ^= payload & 31']
            elif op==4: lines += [f'                    acc = (acc - payload - {c}) & MASK', '                    state ^= cursor']
            elif op==5: lines += [f'                    state = (state ^ (payload + {c})) & MASK', '                    acc += state & 255']
            elif op==6: lines += [f'                    acc = (acc + (state & 1023) * {i%3+1}) & MASK', '                    cursor += payload & 3']
            else: lines += [f'                    state = (state + (acc ^ {c})) & MASK', '                    acc ^= cursor']
        elif kind == 'protocol_sparse':
            lines += [f'                    header = (payload ^ {c}) & 0xffff', '                    acc = (acc + header + cursor) & MASK', '                    state ^= (header << 3) & MASK', '                    cursor = (cursor + 1) & 0xffff']
        elif kind == 'state_machine':
            # Use stimulus and state to mimic DFA / workflow transition work.
            lines += [f'                    acc = (acc + stimulus + {c}) & MASK', f'                    state = (state + stimulus + {i%11+1}) % ROUTES', '                    cursor ^= state']
        elif kind == 'parser_int':
            lines += [f'                    acc = (acc + payload + {c}) & MASK', '                    cursor += 1', '                    if payload & 1:', '                        state ^= acc & 0xff', '                    else:', '                        state = (state + cursor) & MASK']
        elif kind == 'http_server':
            lines += [f'                    status = 200 + ({i} & 3)', f'                    acc = (acc + payload * {i%7+1} + status + {c}) & MASK', '                    cursor += 1', '                    state ^= (acc >> 9) & 0xffff']
        elif kind == 'rpc_typed':
            lines += [f'                    acc = (acc ^ (payload + {c})) & MASK', f'                    state = (state + acc + {i%13+1}) & MASK', '                    cursor += payload & 7']
        elif kind == 'server_heavy':
            lines += [f'                    x = (payload + {c}) & 0xffff', '                    x = ((x * 33) ^ (x >> 3)) & 0xffff', '                    y = (x + cursor) & 0xffff', '                    acc = (acc + x * y + (state & 255)) & MASK', '                    state = ((state << 3) ^ acc ^ y) & MASK', '                    cursor = (cursor + y + 1) & 0xffff']
        elif kind == 'event_mixed':
            lines += [f'                    acc = (acc + payload + {c}) & MASK', '                    state ^= (acc + cursor) & 0xffff', '                    cursor += 1']
        elif kind == 'direct_control':
            # Minimal body: control showing the best case for low-overhead portable plans.
            lines += [f'                    acc += {i+1}']
        else: raise AssertionError(kind)
    lines.append('                if case():')
    lines += ['                    acc ^= payload if \"payload\" in locals() else stimulus', '                    cursor += 1']
    lines += ['    return (acc ^ state ^ cursor) & MASK']
    return '\n'.join(lines)+'\n', kmode


def make_sequence(kind: str, keys: list[Any], pattern: str, length: int, seed: int):
    rng=random.Random(seed)
    if kind=='state_machine':
        return tuple(rng.randrange(1,17) for _ in range(length))
    if pattern=='sequential':
        sels=[keys[i%len(keys)] for i in range(length)]
    elif pattern=='alternating':
        sels=[keys[0] if i%2==0 else keys[-1] for i in range(length)]
    elif pattern=='skewed':
        hot=keys[:min(4,len(keys))]
        sels=[]
        for _ in range(length):
            if rng.random()<0.9: sels.append(hot[rng.randrange(len(hot))])
            else: sels.append(keys[rng.randrange(len(keys))])
    else:
        sels=[keys[rng.randrange(len(keys))] for _ in range(length)]
    return tuple((s, rng.randrange(1,256)) for s in sels)


def compile_one(source, mode, engine, kmode, routes):
    ns={'switch':switch,'case':case,'MASK':MASK,'ROUTES':routes}
    exec(compile(source, f'<v122-work-{mode}-{engine}>','exec'),ns)
    kw={'mode':mode,'source':source,'case_key_mode':kmode}
    if mode=='fast': kw['live_engine']=engine
    t0=time.perf_counter_ns(); fn=enable_switch(**kw)(ns['workload']); comp=(time.perf_counter_ns()-t0)/1e6
    return fn,comp


def telemetry(fn):
    attrs=['__pyswitch_backend__','__pyswitch_live_engine__','__pyswitch_case_count__','__pyswitch_switch_count__','__pyswitch_balanced_plan_count__','__pyswitch_direct_plan_count__','__pyswitch_template_plan_count__','__pyswitch_statement_template_plan_count__','__pyswitch_binary_route_plan_count__']
    d={a:getattr(fn,a,None) for a in attrs}
    info=getattr(fn,'__pyswitch_native_dispatch_info__',())
    if info: d['native_info']=info
    return d


def measure(fn, seq, rounds, repeats, keep_gc=False):
    # Warm adaptive interpreter and stabilize the live gate.
    for _ in range(4): fn(seq, max(1,min(rounds,3)))
    vals=[]; result=None
    gc.collect()
    if not keep_gc:
        gc.disable()
    try:
        for _ in range(repeats):
            t0=time.perf_counter_ns(); result=fn(seq,rounds); vals.append(time.perf_counter_ns()-t0)
    finally:
        if not keep_gc:
            gc.enable()
    dispatches=len(seq)*rounds
    ns=[v/dispatches for v in vals]
    return {'median_ns_per_dispatch':statistics.median(ns),'min_ns_per_dispatch':min(ns),'max_ns_per_dispatch':max(ns),'samples_ns_per_dispatch':ns,'result':result}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--kind',required=True); ap.add_argument('--routes',type=int,required=True); ap.add_argument('--pattern',default='random'); ap.add_argument('--seed',type=int,default=1); ap.add_argument('--target',type=int,default=800_000); ap.add_argument('--repeats',type=int,default=5); ap.add_argument('--length',type=int,default=1024); ap.add_argument('--keep-gc',action='store_true'); a=ap.parse_args()
    # Pin to one allowed CPU when possible. Each subprocess is independent.
    keys=keys_for(a.kind,a.routes); source,kmode=build_source(a.kind,keys); seq=make_sequence(a.kind,keys,a.pattern,a.length,a.seed)
    rounds=max(1, math.ceil(a.target/len(seq)))
    out={'kind':a.kind,'routes':a.routes,'pattern':a.pattern,'seed':a.seed,'sequence_len':len(seq),'rounds':rounds,'dispatches_per_sample':len(seq)*rounds,'python':sys.version,'backends':{}}
    fns={}
    for label,mode,engine in [('portable','portable',None),('ctypes','fast','ctypes'),('native','fast','native')]:
        f,c=compile_one(source,mode,engine,kmode,a.routes); fns[label]=f; out['backends'][label]={'compile_ms':c,'telemetry':telemetry(f)}
    # correctness before timing
    small_rounds=min(rounds,3)
    expected=fns['portable'](seq,small_rounds)
    for label,f in fns.items():
        got=f(seq,small_rounds)
        if got!=expected: raise AssertionError((label,got,expected))
    # rotate order by seed to reduce systematic thermal/order bias
    order=['portable','ctypes','native']; shift=a.seed%3; order=order[shift:]+order[:shift]
    for label in order: out['backends'][label]['timing']=measure(fns[label],seq,rounds,a.repeats,a.keep_gc)
    print(json.dumps(out,default=str))
if __name__=='__main__': main()
