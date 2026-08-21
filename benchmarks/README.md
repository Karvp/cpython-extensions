# Benchmarks

This directory contains performance measurement tooling and retained pre-public development benchmark evidence.

> **Version-label note:** numeric labels embedded in benchmark filenames, JSON fields, or driver names are internal development checkpoints used to compare optimizer iterations. They were not published to GitHub or PyPI. **1.0.3 is the first public package release.**

## Layout

- `scripts/` — executable benchmark drivers and worker processes.
- `results/` — committed benchmark output captured for pre-public development comparisons (`.json` and `.txt`).

Regression and correctness stress harnesses remain under `tests/`; they are CI/certification inputs rather than benchmark drivers.

## Running benchmarks

Run benchmark drivers from the repository root so relative examples and source-tree defaults remain predictable. For example:

```bash
python benchmarks/scripts/benchmark_inline_optimizer.py
python benchmarks/scripts/benchmark_shared_regions.py
```

Version-comparison drivers may require a baseline checkout/source path or an environment variable. Use `--help` on the individual driver for its exact inputs.

## Result artifacts

Files under `results/` are historical evidence and should normally be treated as immutable. New benchmark runs should write a new, clearly versioned result file instead of overwriting evidence from an older development checkpoint.

Timing values are machine-, OS-, build-, specialization-, and load-dependent. Prefer semantic/correctness gates for release safety; benchmark numbers are evidence for performance trends, not correctness guarantees.

## Contribution guidance

Benchmark changes must remain general-purpose. Do not add fixture-specific shortcuts or alter runtime semantics solely to improve benchmark output. When a benchmark motivates an optimization, pair the change with focused correctness regression coverage under `tests/`.
