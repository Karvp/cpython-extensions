# Compatibility

Current supported release: **1.1.0**.

## Supported runtime

| Area | Supported boundary |
|---|---|
| Implementation | CPython |
| Python | `>=3.13,<3.14` |
| Primary certification interpreter | CPython 3.13.5 |
| Portable package platforms | Windows, Linux, macOS |
| Distribution type | Pure Python, CPython-bytecode-specific |

CI runs the required regression suite on CPython 3.13 GitHub-hosted runners across Windows, Linux, and macOS. The package is pure Python, but its transformations deliberately depend on CPython 3.13 bytecode and exception-table behavior; “pure Python” does **not** imply implementation portability.

## Not currently certified

The project does not currently claim independent production certification for:

- free-threaded CPython 3.13 (`3.13t`);
- CPython 3.14 or newer;
- CPython 3.12 or older;
- PyPy, GraalPy, or other Python implementations;
- live/self-modifying switch modes in environments that prohibit or instrument their required runtime behavior.

Unsupported runtimes may fail explicitly rather than attempting an unsafe transformation.

## Runtime dependency boundary

The package requires:

```text
bytecode >=0.17,<0.18
```

The upper bound is intentional. `bytecode` changes can alter instruction, stack, CFG, and exception-region abstractions used by the transformation engine. Version 0.18.1 is not admitted by the 1.1.0 contract: its compatibility run reached 369/370 tests and failed the high-fast-local copy-propagation regression because the transformed stack-size calculation no longer matched the expected code object.

Do not widen this range until the full transformation suite, generated-code verifier, artifact tests, and relevant stress harnesses pass on the candidate dependency.

## Build and release tooling

Release validation currently declares `twine>=5,<7`; Twine 6.2.0 is the certified line. CI also runs a dedicated Twine 7.0.0 candidate job so a future major-version widening is based on an actual build/metadata-validation run rather than a dependency-range edit alone.

Twine is validation tooling, not the upload credential path. PyPI publication uses GitHub OIDC Trusted Publishing. `tools/install_dependencies.py` reads declared release dependencies from `pyproject.toml` without installing the local project first, preserving the clean tagged tree required by release preflight.

## Source availability requirements

Switch and goto decorators recognize marker syntax from retrievable function source. Functions defined in contexts where `inspect` cannot recover source—some REPL, `stdin`, notebook, generated, or dynamically executed definitions—may require the documented explicit-source path or may be untransformable.

Put production-decorated functions in normal source files whenever practical. See the comprehensive guide for generated/notebook examples.

## Dynamic mutation and inline binding

Inlining exposes two explicit binding contracts:

- `binding="frozen"` snapshots the eligible target state used during transformation and is appropriate for intentionally stable hot helpers;
- `binding="guarded"` validates supported target/code/default/descriptor state and falls back to the ordinary call when that state becomes stale.

Guarded binding is semantic hardening, not a synchronization primitive for arbitrary concurrent mutation. See [`COMPREHENSIVE_GUIDE.md`](COMPREHENSIVE_GUIDE.md) for the exact guard and deoptimization behavior.

## Compatibility expansion policy

Supporting another CPython minor version is a port, not a metadata-only change. The 1.1.0 milestone intentionally keeps the CPython 3.13 boundary while strengthening evidence inside that boundary. A new interpreter line requires review of opcode forms, call conventions, jumps, exception tables, stack effects, code-object construction, source/bytecode mapping, verifier logic, and all transformation-specific stress suites before `requires-python` is widened.
