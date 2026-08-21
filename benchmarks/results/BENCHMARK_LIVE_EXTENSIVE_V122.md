# CPython Extensions v1.22 — Extensive Live Self-Modifying Dispatch Benchmark

## Scope

- Runtime: CPython 3.13.5, GCC 14.2.0, Linux x86-64 container.
- Compared: `portable`, `fast + ctypes`, and renovated `fast + native-fused-v1`.
- Broad matrix: 30 workload/size/traffic configurations × 3 fresh processes × 3 timing samples × 3 backends.
- Broad matrix timed dispatches: **202,798,080** (warmups and correctness calls excluded).
- Additional tests: 2,048-route VM scaling; 5–10 million-dispatch long-loop samples with GC enabled; threaded/isolated server modes; coroutine server batches; one-route-call-per-request server routing; code-size and compile-cost inspection.
- Correctness gate: **450/450 pytest passed**; full live compatibility harness **1,239,100 calls passed** under `-X dev`.

The broad CPU-dispatch matrix disables cyclic GC during timed samples to isolate dispatch/body cost. Production-style long-loop and server-mode experiments keep GC enabled. Fresh-process medians are used because the host showed occasional scheduler outliers.

## Executive result

| Workload family | Native vs portable | Result |
|---|---:|---|
| Dense VM/opcode | 1.82× geometric mean (1.70–2.06×) | strong win |
| Parser/token int | 1.62× geometric mean (1.41–1.85×) | strong win |
| State machine | 1.18× geometric mean (1.10–1.26×) | moderate win |
| Mixed typed event bus | 1.11× geometric mean (1.08–1.14×) | moderate win |
| HTTP string router | 1.01× geometric mean (1.00–1.03×) | rough tie |
| Typed RPC strings | 0.98× geometric mean (0.90–1.05×) | rough tie |
| Sparse integer protocol | 0.98× geometric mean (0.96–1.04×) | rough tie |
| Heavy server body | 0.94× geometric mean (0.93–0.95×) | portable wins |
| Minimal/direct control | 0.88× geometric mean (0.80–0.97×) | portable wins |

## VM / interpreter dispatch

| Routes / traffic | Portable ns | ctypes ns | Native ns | Native vs portable | Native vs ctypes |
|---|---:|---:|---:|---:|---:|
| 64 / random | 250.4 | 209.6 | 147.4 | **1.70×** | 1.42× |
| 256 / random | 302.9 | 226.4 | 178.3 | **1.70×** | 1.27× |
| 256 / skewed | 258.4 | 191.8 | 145.0 | **1.78×** | 1.32× |
| 1024 / random | 361.2 | 245.4 | 193.0 | **1.87×** | 1.27× |
| 1024 / skewed | 330.6 | 212.9 | 160.5 | **2.06×** | 1.33× |
| 2,048 / random | 393.3 | 212.6 | 159.2 | **2.47×** | 1.34× |
| 2,048 / skewed | 319.8 | 220.4 | 148.0 | **2.16×** | 1.49× |

The dense native lane scales as intended. The best measured route-scale point is 2,048 random routes at roughly **2.47× portable** and **1.34× ctypes**.

## Other task families

| Task | Routes / traffic | Portable ns | Native ns | Native vs portable |
|---|---|---:|---:|---:|
| parser_int | 256 / skewed | 289.0 | 156.5 | 1.85× |
| state_machine | 128 / random | 152.9 | 121.1 | 1.26× |
| protocol_sparse | 256 / random | 237.1 | 242.2 | 0.98× |
| http_server | 64 / skewed | 238.1 | 239.1 | 1.00× |
| http_server | 256 / random | 247.5 | 241.8 | 1.02× |
| rpc_typed | 64 / random | 203.1 | 193.8 | 1.05× |
| rpc_typed | 256 / random | 194.0 | 215.3 | 0.90× |
| event_mixed | 128 / skewed | 192.0 | 178.1 | 1.08× |
| server_heavy | 64 / skewed | 351.1 | 377.3 | 0.93× |
| direct_control | 256 / random | 49.7 | 62.2 | 0.80× |

### Why HTTP/RPC/sparse protocol differs

Portable compilation recognized those route bodies as `portable-statement-template-v18`; the whole route table collapses to a compact table-driven template. Native live must retain the inline case bodies and mutate a gate, so its architectural advantage disappears.

| Representative case | Portable code | Native code | Native / portable |
|---|---:|---:|---:|
| vm_dense 1024 | 59,892 B | 74,602 B | 1.2× |
| protocol_sparse 256 | 346 B | 29,882 B | 86.4× |
| http_server 256 | 360 B | 26,298 B | 73.0× |
| rpc_typed 64 | 388 B | 6,212 B | 16.0× |
| state_machine 128 | 346 B | 11,724 B | 33.9× |
| direct_control 256 | 268 B | 9,674 B | 36.1× |

This is the largest optimization signal from the benchmark: **do not force live mode when portable can statement-template the switch**. For HTTP 256 routes the portable function is only 360 bytes of bytecode versus 26,298 bytes for native live.

## Long in-frame loops (GC enabled)

These are 5–10 million internal dispatches per timing sample. Process-isolated medians are shown; some configurations exhibited host scheduler outliers, so the broad matrix remains the primary estimate where noted.

| Task | Dispatches/sample | Portable ns | Native ns | Speedup |
|---|---:|---:|---:|---:|
| vm_dense 1024 skewed | 10,000,384 | 329.4 | 169.0 | 1.95× |
| http_server 64 skewed | 10,000,384 | 228.9 | 231.2 | 0.99× |
| protocol_sparse 256 random | 5,000,192 | 219.1 | 220.2 | 0.99× |
| parser_int 256 skewed | 5,000,192 | 254.9 | 142.7 | 1.79× |
| state_machine 128 random | 5,000,192 | 144.9 | 125.8 | 1.15× |
| event_mixed 128 skewed | 5,000,192 | 426.7 | 202.8 | 2.10× |
| server_heavy 64 skewed | 5,000,192 | 382.7 | 401.4 | 0.95× |

The stable long-loop signals match the broad matrix: VM/parser retain large gains, HTTP/sparse protocol remain approximately tied with portable, and heavy server bodies favor portable. Event/state-machine long samples were noisier than the broad process matrix and should not override the broader medians.

## Server execution modes

### One route function call per request (64 string routes)

| Backend | ns/request | Relative to portable |
|---|---:|---:|
| portable | 135.9 | 1.00× |
| fast native (single-active-call) | 141.0 | 0.96× |
| thread-local native (safe) | 246.2 | 0.55× |

For classic one-call-per-request routing, **portable is preferred**. Thread-local live cloning/wrapper machinery has no opportunity to amortize.

### Threaded batched server routing (3-process medians)

| Threads | Dispatches/call | Portable ns | thread_local ns | isolated ns |
|---:|---:|---:|---:|---:|
| 1 | 256 | 255.4 | 234.3 | 214.8 |
| 1 | 4,096 | 227.4 | 234.2 | 216.1 |
| 1 | 65,536 | 227.3 | 218.4 | 234.9 |
| 4 | 256 | 234.1 | 230.4 | 223.7 |
| 4 | 4,096 | 238.4 | 259.0 | 226.6 |
| 4 | 65,536 | 233.7 | 240.7 | 227.5 |

These differences are mostly within ±10%; the safe live wrappers are not a compelling HTTP-string win. They are valuable for semantics/isolation, not throughput in this route shape.

### Coroutine server batches

| Dispatches/coroutine call | Portable ns | isolated-native ns | Live speedup |
|---:|---:|---:|---:|
| 256 | 211.0 | 252.4 | 0.84× |
| 4,096 | 215.4 | 214.6 | 1.00× |
| 65,536 | 212.5 | 205.6 | 1.03× |

Per-call clone cost is visible at 256 dispatches (live ~16% slower), amortized by ~4K dispatches, and only slightly favorable at 65K dispatches. This is not a reason to use live mode for ordinary async request handlers.

## Compile-time economics

For hot VM loops the native compile premium is small enough to amortize quickly; for portable-template-friendly route tables it is not.

| Case | Portable compile | Native compile | Runtime saving | Approx. break-even |
|---|---:|---:|---:|---:|
| vm_dense 256 random | 300.6 ms | 312.7 ms | 124.6 ns | 97,125 dispatches |
| vm_dense 1024 skewed | 1178.6 ms | 1238.6 ms | 170.1 ns | 353,222 dispatches |
| state_machine 128 random | 80.9 ms | 171.4 ms | 31.8 ns | 2,847,638 dispatches |
| http_server 256 random | 129.1 ms | 469.0 ms | 5.7 ns | 59,376,567 dispatches |
| rpc_typed 64 random | 31.5 ms | 95.3 ms | 9.3 ns | 6,872,722 dispatches |

## Recommended policy from the evidence

1. **Dense integer VM/opcode/interpreter loops:** native live is the preferred backend when execution safety permits it.
2. **Integer parser/token dispatch:** native live is strongly attractive.
3. **State machines:** moderate benefit; use when the function is genuinely hot and long-lived.
4. **Mixed typed event dispatch:** small broad-matrix benefit; benchmark the concrete application.
5. **HTTP/RPC string routing:** prefer portable, especially when statement-template lowering is available.
6. **Sparse integer protocol routing:** current native hash lane beats ctypes but not the portable statement-template route; this is a direct optimization target.
7. **Heavy route bodies:** portable generally wins overall because dispatch becomes a small fraction of work.
8. **One-call-per-request and small async batches:** portable should be the default.
9. **Auto-selection renovation:** before opting into live by route count alone, query portable plan eligibility; a statement/direct/template plan should veto live unless explicit user forcing requests it.

## Qualification

- `pytest`: **450 passed**.
- Live compatibility full harness: **1,239,100 calls passed** under CPython dev mode.
- All benchmark workers compare backend outputs before timing.
- No source-runtime optimization was changed during this benchmark pass; only benchmark scripts/results were added.
