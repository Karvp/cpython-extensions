# Benchmarks

Current benchmark evidence release: **1.1.0**.

This directory contains reproducible performance drivers and versioned result artifacts used to support public performance claims. Correctness, differential, verifier, and stress harnesses remain under `tests/`; benchmark results are evidence, not correctness gates.

## Layout

- `scripts/` — executable benchmark drivers and worker processes.
- `results/` — committed JSON/text evidence retained for release qualification and historical comparison.

Treat recorded result files as immutable release evidence. New releases should write new versioned result files instead of overwriting earlier measurements.

## 1.1.0 benchmark suite

The 1.1.0 suite separates three questions that are easy to conflate:

1. **How does switch dispatch scale as route count grows?**
2. **Does eligible frozen inlining remove measurable call overhead?**
3. **Can strict goto efficiently encode a source that is genuinely an explicit state machine?**

### Switch scaling

`benchmark_switch_scaling_v110.py` generates semantically equivalent routers implemented as:

- an `if/elif` chain;
- a `match` statement;
- a minimal bound `dict.get` lookup;
- `@enable_switch(mode="portable")`.

The default matrix covers **2, 4, 8, 16, 32, 64, 128, 256, 512, and 1,024 routes** for integer and string keys. Every successful route is validated before timing and exercised uniformly in forward and reverse order. Miss behavior is validated but excluded from the timed hit sequence so the linear baselines represent average successful-hit depth rather than a deliberately worst-case miss. Large generated routers reduce the target batch count proportionally, but each timed sample retains at least 50,000 successful dispatches.

Reproduce the committed 1.1.0 matrix:

```bash
python benchmarks/scripts/benchmark_switch_scaling_v110.py \
  --target-dispatches 100000 --repeat 7 --warmup-batches 50 \
  --json benchmarks/results/BENCHMARK_SWITCH_SCALING_V110.json
```

For a CI-friendly smoke run:

```bash
python benchmarks/scripts/benchmark_switch_scaling_v110.py --quick
```

The driver also accepts `--sizes`, `--key-kinds`, `--target-dispatches`, `--repeat`, and `--warmup-batches`, for example:

```bash
python benchmarks/scripts/benchmark_switch_scaling_v110.py \
  --sizes 8,32,128,512,1024 --key-kinds int --quick
```

The committed artifact is [`results/BENCHMARK_SWITCH_SCALING_V110.json`](results/BENCHMARK_SWITCH_SCALING_V110.json).

### Inline and goto intended-workload benchmark

`benchmark_extension_benefits_v110.py` compares:

- an ordinary small helper call with the same helper after frozen inlining;
- an explicit three-state dispatch loop with strict goto;
- a naturally structured reference for the same state-machine work.

Run it with:

```bash
python benchmarks/scripts/benchmark_extension_benefits_v110.py \
  --json benchmarks/results/BENCHMARK_EXTENSION_BENEFITS_V110.json
```

The committed artifact is [`results/BENCHMARK_EXTENSION_BENEFITS_V110.json`](results/BENCHMARK_EXTENSION_BENEFITS_V110.json).

### Broad three-feature baseline

`benchmark_readme_baseline_v110.py` retains a compact switch/inline/goto comparison for continuity with the earlier README benchmark format:

```bash
python benchmarks/scripts/benchmark_readme_baseline_v110.py \
  --json benchmarks/results/BENCHMARK_README_BASELINE_V110.json
```

Historical 1.0.4 benchmark files remain under `results/` and are not rewritten.

## Interpreting results

Timing values depend on CPU, operating system, CPython patch/build, adaptive-specialization state, key type, hit distribution, route-body shape, cache state, and system load. Compare results produced by the same driver and methodology; do not compare isolated nanosecond values from unrelated machines as if they were equivalent.

The switch benchmark deliberately exposes its crossover. Two-route native branching can be faster; the portable table-backed specialization becomes increasingly advantageous as route count grows. The structural regression under `tests/` protects the mechanism behind the result: route data grows with case count, but the specialized executable dispatch bytecode remains bounded instead of degenerating into a linear chain.

For performance changes, record:

- exact Python version/build and platform;
- benchmark driver and arguments;
- warmup, repeat, and sample policy;
- raw or committed result artifact;
- correctness/differential checks run before timing;
- code-size, memory, or specialization tradeoffs relevant to interpretation.

## Contribution guidance

Do not add fixture-specific shortcuts, benchmark-only semantics, or special cases whose sole purpose is to improve committed numbers. Optimizations must be general-purpose and preserve the documented semantic contract. Pair performance work with focused correctness and, when appropriate, structural regressions under `tests/`.
