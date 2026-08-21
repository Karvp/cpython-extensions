# Contributing

Thank you for contributing to `cpython-extensions`.

## Engineering principles

Changes should be general-purpose, CPython-3.13-aware, and semantics-preserving for the mode being modified. Do not introduce test-specific pattern matching, benchmark-only shortcuts, silent semantic weakening, or unverified bytecode rewrites.

The preferred order is:

1. Reproduce the behavior with a focused regression.
2. Identify the shared parser/compiler/runtime invariant that is wrong or incomplete.
3. Implement the smallest general fix.
4. Verify transformed code and ordinary Python observables.
5. Add adversarial/generated coverage when the change affects control flow, stack layout, registries, concurrency, or mutation semantics.
6. Run the relevant focused suite, then the full suite.

## Setup

Use CPython 3.13:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

## Required local checks

```bash
python -m pytest
python -m compileall -q src tests
python -m coverage run --branch -m pytest
python -m coverage report
python tools/check_repo.py
```

For changes to transformation code, also run CPython development mode:

```bash
python -X dev -W error -m pytest
```

On POSIX, allocator-debug runs are useful for lifecycle-sensitive changes:

```bash
PYTHONMALLOC=debug python -X dev -m pytest
```

## Stress tests

Normal pull requests should stay reasonably fast. The repository includes a separate stress workflow for full harnesses. Run focused stress locally when changing the corresponding subsystem:

```bash
python tests/harness_guarded_binding_v102.py --scale 0.05
python tests/stress/harness_release_deep.py --quick
```

Use full scale before claiming production certification.

## Tests and semantics

For bytecode transformations, test more than return values. Relevant observables can include:

- exception type/message and protected-region behavior;
- argument evaluation order and exactly-once lookup;
- aliasing and mutation;
- recursion, closures, defaults, descriptors, and rebinding;
- generator/coroutine/async-generator suspension;
- tracing/source locations when the backend promises caller-frame execution;
- thread-safe decoration/registry lifecycle;
- generated code verification and stack depth;
- behavior under different `PYTHONHASHSEED` values.

## Pull requests

Keep PRs scoped and explain:

- the semantic/performance problem;
- the invariant used by the fix;
- which modes/backends are affected;
- new regression/stress coverage;
- any performance or compatibility tradeoff.

Do not commit build directories, virtual environments, caches, `.egg-info`, wheels, sdists, credentials, or local benchmark noise unless the result is intentionally part of the historical evidence set.

## Commit style

Use short imperative subjects, for example:

- `Fix guarded inline default invalidation`
- `Verify strict goto extended jumps`
- `Add typed switch collision regression`

## Release-affecting changes

Update `CHANGELOG.md`, user-facing documentation, and tests. Version changes must go through `src/python_extensions/_version.py`. See `docs/RELEASING.md`.
