from __future__ import annotations
import asyncio, concurrent.futures, gc, importlib.util, json, statistics, time, sys, os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('bw', ROOT/'benchmarks/scripts/benchmark_live_workloads_v122_worker.py')
bw=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(bw)
from python_extensions import enable_switch, switch, case


def compile_source(source, mode, *, engine=None, kmode='python'):
    ns={'switch':switch,'case':case,'MASK':bw.MASK,'ROUTES':64}
    exec(compile(source,f'<server-{mode}>','exec'),ns)
    kw={'mode':mode,'source':source,'case_key_mode':kmode}
    if engine: kw['live_engine']=engine
    return enable_switch(**kw)(ns['workload'])

def time_threaded(fn, seq, batch_rounds, calls_per_thread, workers, repeats=4):
    def local_worker():
        out=0
        for _ in range(calls_per_thread): out ^= fn(seq,batch_rounds)
        return out
    vals=[]; checksum=None
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # force clone creation / warmup on all workers
        list(pool.map(lambda _: fn(seq,1), range(workers)))
        for _ in range(repeats):
            t0=time.perf_counter_ns(); futs=[pool.submit(local_worker) for _ in range(workers)]; got=[f.result() for f in futs]; vals.append(time.perf_counter_ns()-t0)
            checksum=got
    dispatches=len(seq)*batch_rounds*calls_per_thread*workers
    ns=[v/dispatches for v in vals]
    return {'median_ns':statistics.median(ns),'samples_ns':ns,'checksum':checksum}

def thread_matrix():
    keys=bw.keys_for('http_server',64); source,km=bw.build_source('http_server',keys)
    seq=bw.make_sequence('http_server',keys,'skewed',256,7)
    fns={'portable':compile_source(source,'portable',kmode=km), 'thread_local':compile_source(source,'thread_local',engine='native',kmode=km), 'isolated':compile_source(source,'isolated',engine='native',kmode=km)}
    # correctness
    ref=fns['portable'](seq,2)
    assert all(f(seq,2)==ref for f in fns.values())
    rows=[]
    for workers in (1,4):
        for batch_dispatches in (256,4096,65536):
            rounds=max(1,(batch_dispatches+len(seq)-1)//len(seq)); calls=max(1, min(64, 262144//(len(seq)*rounds)))
            row={'workers':workers,'batch_dispatches':len(seq)*rounds,'calls_per_thread':calls}
            for name,f in fns.items(): row[name]=time_threaded(f,seq,rounds,calls,workers)
            row['thread_local_vs_portable']=row['portable']['median_ns']/row['thread_local']['median_ns']
            row['isolated_vs_portable']=row['portable']['median_ns']/row['isolated']['median_ns']
            rows.append(row)
            print('thread',workers,'batch',row['batch_dispatches'],{k:round(row[k]['median_ns'],1) for k in fns},flush=True)
    return rows

async def async_measure(fn,seq,rounds,calls,repeats=5):
    vals=[]; result=None
    for _ in range(2): await fn(seq,rounds)
    for _ in range(repeats):
        t0=time.perf_counter_ns(); out=0
        for _ in range(calls): out ^= await fn(seq,rounds)
        vals.append(time.perf_counter_ns()-t0); result=out
    dispatches=len(seq)*rounds*calls
    ns=[v/dispatches for v in vals]
    return {'median_ns':statistics.median(ns),'samples_ns':ns,'checksum':result}

async def async_matrix():
    keys=bw.keys_for('http_server',64); source,km=bw.build_source('http_server',keys); source=source.replace('def workload(sequence, rounds):','async def workload(sequence, rounds):',1)
    portable=compile_source(source,'portable',kmode=km); isolated=compile_source(source,'isolated',engine='native',kmode=km)
    seq=bw.make_sequence('http_server',keys,'skewed',256,13)
    assert await portable(seq,2)==await isolated(seq,2)
    rows=[]
    for batch in (256,4096,65536):
        rounds=max(1,(batch+len(seq)-1)//len(seq)); calls=max(1,min(64,262144//(len(seq)*rounds)))
        p=await async_measure(portable,seq,rounds,calls); n=await async_measure(isolated,seq,rounds,calls)
        row={'batch_dispatches':len(seq)*rounds,'calls':calls,'portable':p,'isolated_native':n,'speedup':p['median_ns']/n['median_ns'],'isolated_mode':getattr(isolated,'__pyswitch_mode__',None),'isolated_backend':getattr(isolated,'__pyswitch_backend__',None)}; rows.append(row)
        print('async batch',row['batch_dispatches'],'p',round(p['median_ns'],1),'live',round(n['median_ns'],1),'speed',round(row['speedup'],2),flush=True)
    return rows

def main():
    gc.enable(); t0=time.time(); out={'threaded':thread_matrix(),'async':asyncio.run(async_matrix()),'elapsed_s':time.time()-t0,'python':sys.version}
    path=ROOT/'benchmarks/results/BENCHMARK_LIVE_SERVER_MODES_V122.json'; path.write_text(json.dumps(out,indent=2,default=str),encoding='utf-8'); print('RESULT',path)
if __name__=='__main__':main()
