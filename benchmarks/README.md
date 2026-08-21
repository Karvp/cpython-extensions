# Benchmarks

This directory contains performance measurement tooling and the historical benchmark evidence retained by the project.

## Layout

- `scripts/` — executable benchmark drivers and worker processes.
- `results/` — committed benchmark output captured for historical release comparisons (`.json` and `.txt`).

Regression and correctness stress harnesses remain under `tests/`; they are CI/certification inputs rather than benchmark drivers.

## Running benchmarks

Run benchmark drivers from the repository root so relative examples and source-tree defaults remain predictable. For example:

```bash
python benchmarks/scripts/benchmark_inline_optimizer.py
python benchmarks/scripts/benchmark_shared_regions.py
```

Version-comparison drivers may require a baseline checkout/source path or an environment variable. Use `--help` on the individual driver for its exact inputs.


## 1.0.4 plain-Python baseline

`benchmark_readme_baseline_v104.py` compares three extension-backed examples against idiomatic code that does not use the library: an 8-way `if/elif` router, an ordinary helper call, and a structured `while` loop. Its committed result is `results/BENCHMARK_README_BASELINE_V104.json`; the README summarizes those numbers without omitting slower cases.

Run it with:

```bash
python benchmarks/scripts/benchmark_readme_baseline_v104.py --json benchmarks/results/BENCHMARK_README_BASELINE_V104.json
```

## Result artifacts

Files under `results/` are historical evidence and should normally be treated as immutable. New benchmark runs should write a new, clearly versioned result file instead of overwriting evidence from an older release.

Timing values are machine-, OS-, build-, specialization-, and load-dependent. Prefer semantic/correctness gates for release safety; benchmark numbers are evidence for performance trends, not correctness guarantees.

## Contribution guidance

Benchmark changes must remain general-purpose. Do not add fixture-specific shortcuts or alter runtime semantics solely to improve benchmark output. When a benchmark motivates an optimization, pair the change with focused correctness regression coverage under `tests/`.
