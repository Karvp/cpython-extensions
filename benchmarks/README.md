# Benchmarks

Current benchmark evidence release: **1.2.0**.

This directory contains reproducible performance drivers and versioned result artifacts used to support public performance claims. Correctness, differential, verifier, and stress harnesses remain under `tests/`; benchmark results are evidence, not correctness gates.

Internal script/result suffixes such as `v110`, `v121`, and `v122` are **engineering evidence identifiers**, not package versions. They remain stable so historical evidence can be reproduced without renaming artifacts after a release decision.

## Layout

- `scripts/` — executable benchmark drivers and isolated worker processes.
- `results/` — committed JSON/CSV/Markdown/text evidence retained for qualification and historical comparison.

Treat recorded result files as immutable evidence. New qualification passes should write new versioned result files instead of overwriting earlier measurements.

## Benchmark policy

Every performance claim should satisfy the following rules:

1. validate transformed and reference results before timing;
2. record the exact Python build/platform;
3. warm the generated functions before measurement;
4. use multiple samples and, for sensitive live measurements, multiple fresh interpreter processes;
5. record the actual compiler/backend plan so a benchmark cannot silently become a different optimization;
6. include negative/control workloads where the architecture is expected to lose or tie;
7. preserve raw machine-readable results alongside summaries;
8. distinguish dispatch-core cost from whole-workload cost;
9. do not generalize a direct-value/table result to heterogeneous live dispatch, or vice versa;
10. do not keep a source optimization solely because it improves one microbenchmark while regressing the broader certified matrix.

## 1.2.0 extensive live workload matrix

The primary new evidence is:

- `scripts/benchmark_live_workloads_v122.py`
- `scripts/benchmark_live_server_modes_v122.py`
- `scripts/benchmark_live_server_per_request_v122.py`
- `results/BENCHMARK_LIVE_EXTENSIVE_V122.md`
- `results/BENCHMARK_LIVE_EXTENSIVE_V122.json`
- `results/BENCHMARK_LIVE_EXTENSIVE_V122.csv`

The consolidated matrix compares real transformed:

```text
portable
fast + ctypes live engine
fast + native fused live engine
```

across dense VM/interpreter dispatch, integer parser/token routing, state machines, mixed event dispatch, sparse protocol IDs, HTTP/RPC string routing, heavy server bodies, direct/minimal controls, threaded safe modes, coroutine isolation, and one-router-call-per-request server behavior.

The broad matrix contains **30 workload/size/traffic configurations**, three fresh processes per configuration, and **202,798,080 timed dispatches** excluding warmups and correctness checks. Additional 5–10 million-dispatch sustained-loop runs directly test the extensive in-frame looping workload that motivates live self-modification.

### Representative results

On the committed CPython 3.13.5 Linux x86-64 host:

| Workload | Routes / pattern | Portable | ctypes live | native live | Native vs portable |
|---|---|---:|---:|---:|---:|
| VM dense | 64 / random | 250.4 ns | 209.6 ns | **147.4 ns** | **1.70×** |
| VM dense | 1,024 / skewed | 330.6 | 212.9 | **160.5** | **2.06×** |
| VM dense | 2,048 / random | 393.3 | 212.6 | **159.2** | **2.47×** |
| Parser integer | 256 / skewed | 289.0 | 203.2 | **156.5** | **1.85×** |
| State machine | 128 / random | 152.9 | 169.9 | **121.1** | **1.26×** |
| HTTP string | 64 / random | 230.7 | 247.5 | 230.0 | ~1.00× |
| Sparse protocol | 256 / random | **237.1** | 295.7 | 242.2 | 0.98× |
| Heavy server | 256 / random | **406.3** | 443.3 | 427.6 | 0.95× |
| Direct/minimal | 256 / random | **49.7** | 83.2 | 62.2 | 0.80× |

A 1,024-route VM sample with **10,000,384 internal dispatches per timed sample** records approximately 329.4 ns portable, 222.5 ns ctypes, and 169.0 ns native. A comparable 10-million-dispatch 64-route HTTP loop remains effectively tied at about 228.9 ns portable versus 231.2 ns native.

The evidence therefore supports **workload classification**, not a universal live-speedup claim. See [`../docs/LIVE_SWITCH.md`](../docs/LIVE_SWITCH.md).

### Server controls

The per-request benchmark explicitly measures the architecture's weak case: one router function call per request. At 64 string routes the committed sample is approximately:

```text
portable                  135.9 ns/request
shared fast native        141.0 ns/request
thread_local native       246.2 ns/request
```

This is why ordinary HTTP/RPC routing should normally remain portable even when the server itself is busy.

## Real live-vs-portable corrective benchmark (`v121` evidence)

`benchmark_switch_live_dispatch_v121.py` was added after an earlier benchmark-design incident. The old comparison performed one Python call per switch and emphasized direct-value cases, which strongly favored portable table lookup and did not exercise live mode's intended repeated in-frame architecture.

The corrective driver therefore:

- compiles `mode="portable"` and `mode="fast"` from the **exact same source**;
- keeps repeated dispatches inside one outer function invocation;
- asserts that portable selected the expected general/balanced plan rather than direct/template specialization;
- asserts that fast selected the real CPython 3.13 live-inline plan;
- verifies live gate count and correctness before timing;
- exercises sequential, alternating, deterministic-random, and 90/10-skewed traffic;
- aggregates isolated-process medians.

It also exposed a real compiler bug: large/multi-site functions can require `EXTENDED_ARG` before gate marker operands. The live locator was repaired to locate and bind from the prefix start, with regression tests for >255 constants and >255 locals.

The incident and resolution are retained under `results/SWITCH_LIVE_BENCHMARK_INCIDENT_RESOLUTION_V121.md` and `results/BENCHMARK_SWITCH_LIVE_DISPATCH_V121.json`.

## Historical 1.1.0 portable scaling suite

`benchmark_switch_scaling_v110.py` answers a different question: how portable direct-value routing scales relative to linear native Python dispatch forms.

It generates equivalent routers implemented as:

- `if/elif`;
- `match`;
- a minimal bound `dict.get`;
- `@enable_switch(mode="portable")`.

The default matrix covers **2 through 1,024 integer and string routes**. The result demonstrates bounded executable dispatch shape for table-friendly portable plans while `if/elif`/`match` code grows with route count. Keep this evidence separate from live heterogeneous routing.

Reproduce:

```bash
python benchmarks/scripts/benchmark_switch_scaling_v110.py \
  --target-dispatches 100000 --repeat 7 --warmup-batches 50 \
  --json benchmarks/results/BENCHMARK_SWITCH_SCALING_V110.json
```

## Inline and goto intended-workload benchmark

`benchmark_extension_benefits_v110.py` compares:

- an ordinary small helper call with the same helper after frozen inlining;
- an explicit three-state dispatch loop with strict goto;
- a naturally structured reference for the same state-machine work.

Run:

```bash
python benchmarks/scripts/benchmark_extension_benefits_v110.py \
  --json benchmarks/results/BENCHMARK_EXTENSION_BENEFITS_V110.json
```

The structured-loop control remains important: goto is not claimed to be faster than Python source that is already naturally expressed as a loop.

## Specialization benchmark and stress evidence

`benchmark_specialization_v120.py` measures explicit/automatic specialization cases. Correctness qualification lives under `tests/`, including:

- `test_specialization_v120.py`;
- `harness_specialization_v120.py`;
- `harness_specialization_v121_adversarial.py`.

The adversarial harness focuses on semantic boundaries rather than only throughput: call binding, invalid calls, variadics, signed zero/NaN/complex constants, side-effecting objects, concurrency/promotion, bounded megamorphic profiles, recursion/closures, tracing, descriptors, async behavior, monitoring lifecycle, and weak-reference cleanup.

## Interpreting results

Timing values depend on CPU, operating system, CPython patch/build, adaptive-specialization state, key type, traffic distribution, route-body shape, cache state, and system load. Compare results produced by the same driver/methodology. Do not compare unrelated nanosecond figures as if they were equivalent.

For live work in particular, report both:

- **dispatch-only or dispatch-dominant cost**, which exposes routing architecture;
- **whole-workload cost**, which shows whether routing is important enough to matter.

Also report compile/decorator cost and generated code size when a transformation substantially changes either.

## Contribution guidance

Do not add fixture-specific shortcuts, benchmark-only semantics, or special cases whose sole purpose is to improve committed numbers. Optimizations must be general-purpose and preserve the documented semantic contract. Pair performance work with focused correctness, structural regressions, and the broadest workload matrix relevant to the changed mechanism.
