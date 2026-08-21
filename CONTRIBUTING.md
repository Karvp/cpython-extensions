# Contributing

Thank you for contributing to `cpython-extensions`. The current release is **1.1.0**, targeting CPython 3.13.

## Engineering principles

Changes must be general-purpose, CPython-3.13-aware, and semantics-preserving for the mode being modified. Do not introduce test-specific pattern matching, benchmark-only shortcuts, silent semantic weakening, or bytecode rewrites that bypass verification.

For transformation work, prefer this sequence:

1. Reproduce the behavior with a focused regression.
2. Identify the shared parser/compiler/runtime invariant that is incorrect or incomplete.
3. Implement the smallest general fix.
4. Verify both transformed-code structure and ordinary Python observables.
5. Add adversarial or generated coverage when the change affects control flow, stack layout, registries, concurrency, mutation, or exception behavior.
6. Run the focused suite, then the complete suite and relevant stress harnesses.

## Development setup

Use CPython 3.13:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Canonical repository:

```bash
git clone https://github.com/Karvp/cpython-extensions.git
cd cpython-extensions
```

## Required local checks

Before opening a pull request, run:

```bash
python -m pytest
python -m compileall -q src tests tools benchmarks/scripts
python -m coverage run --branch -m pytest
python -m coverage report
python tools/check_repo.py
```

For transformation-code changes, also run CPython development mode:

```bash
python -X dev -W error -m pytest
```

If unrelated globally installed pytest plugins emit warnings before this project's tests are collected, repeat the isolated check with third-party plugin autoload disabled:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -X dev -W error -m pytest
```

On POSIX, allocator-debug runs are valuable for lifecycle-sensitive work:

```bash
PYTHONMALLOC=debug python -X dev -m pytest
```

## Stress and differential testing

Normal pull-request feedback should remain reasonably fast. Full-scale harnesses live separately from the ordinary unit suite. Run the focused stress harness for the subsystem you change and use full scale before making a release-certification claim.

Examples:

```bash
python tests/harness_guarded_binding_v102.py --scale 0.05
python tests/stress/harness_release_deep.py --quick
```

Performance changes should also run the corresponding public benchmark. For switch work:

```bash
python benchmarks/scripts/benchmark_switch_scaling_v110.py --quick
```

Do not optimize solely for committed benchmark inputs. Performance work must retain differential correctness coverage and, where practical, a structural regression that protects the optimization mechanism rather than a machine-specific timing threshold. Timing regressions belong in review evidence; correctness and structural invariants belong in automated gates.

## What to test for bytecode transformations

Return values are only one observable. Relevant tests may also need to cover:

- exception type/message and protected-region behavior;
- argument evaluation order and exactly-once lookup;
- aliasing, mutation, and rebinding;
- recursion, closures, defaults, descriptors, and partials;
- generators, coroutines, and async-generator suspension;
- tracing/source locations when the backend promises caller-frame execution;
- thread-safe decoration and registry lifecycle;
- stack depth, code size, and generated-code verification;
- behavior under different `PYTHONHASHSEED` values.

## Pull requests

Keep pull requests scoped. Explain:

- the semantic or performance problem;
- the invariant used by the fix;
- affected modes/backends;
- regression, stress, and benchmark coverage;
- compatibility or performance tradeoffs;
- user-facing documentation changes, when applicable.

Do not commit build directories, virtual environments, caches, `.egg-info`, wheels, sdists, credentials, or local benchmark noise unless a result file is intentionally retained as versioned release evidence.

## Commit style

Use short imperative subjects, for example:

- `Fix guarded inline default invalidation`
- `Verify strict goto extended jumps`
- `Preserve bounded large-switch dispatch`

## Release-affecting changes

A release-affecting change should update every source of public release state that applies: `CHANGELOG.md`, `README.md`, `SECURITY.md`, `CITATION.cff`, release notes, compatibility/setup guidance, benchmark documentation/evidence, certification/audit records, and relevant tests.

The version source is `src/python_extensions/_version.py`. `tools/check_repo.py` enforces release-sensitive documentation markers so stale support/version claims fail repository hygiene. See [`docs/RELEASING.md`](docs/RELEASING.md).
