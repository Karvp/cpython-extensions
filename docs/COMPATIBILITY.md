# Compatibility

Current supported release: **1.2.0**.

## Supported runtime

| Area | Supported boundary |
|---|---|
| Implementation | CPython |
| Python | `>=3.13,<3.14` |
| Primary certification interpreter | CPython 3.13.5 |
| Portable transforms | Windows, Linux, macOS on CPython 3.13 |
| Optional native live accelerator | CPython 3.13 platform extension; official wheels should contain it |
| Free-threaded CPython | portable paths only; live mode not certified |

Most transformations are implemented in Python but deliberately depend on CPython 3.13 bytecode and exception-table behavior. “Python source” does **not** imply interpreter portability.

## Not currently certified

The project does not currently claim production certification for:

- live/self-modifying switch modes on free-threaded CPython 3.13 (`3.13t`);
- CPython 3.14 or newer;
- CPython 3.12 or older;
- PyPy, GraalPy, or other Python implementations;
- live switch in environments that prohibit or rewrite its required runtime code layout;
- direct use of `python_extensions._livegate` outside the public switch API/runtime self-tests.

Unsupported runtimes may fail explicitly rather than attempting an unsafe transformation.

## Native live accelerator

`python_extensions._livegate` is an optional C extension declared by `setup.py`. It uses the regular CPython C API rather than the Limited API/abi3 because the feature is intentionally CPython-3.13-specific.

`live_engine="auto"` requires the Python-side live-layout self-test and then uses the native engine only if the native engine's own gate-write self-test succeeds. Otherwise it falls back to ctypes. `live_engine="native"` requires the accelerator and raises if it is unavailable.

Source installations can therefore retain portable/ctypes functionality when a compiler is unavailable. Release-wheel validation on the build platform additionally asserts that the produced wheel contains `_livegate`.

On a free-threaded build, `switch.py` checks `Py_GIL_DISABLED` through `sysconfig` **before** importing `_livegate`; this avoids silently importing an extension that does not declare no-GIL support. Live modes are rejected and portable mode remains available.

## Runtime dependency boundary

The package requires:

```text
bytecode >=0.17,<0.18
```

The upper bound is intentional. `bytecode` changes can alter instruction, stack, CFG, and exception-region abstractions used by the transformation engine. Version 0.18.1 is not admitted by the current contract: its compatibility run failed the high-fast-local copy-propagation regression because transformed stack-size calculation no longer matched the expected code object.

Do not widen this range until the full transformation suite, verifier, installed-artifact tests, specialization stress, and relevant live/native harnesses pass on the candidate dependency.

## Build and release tooling

Release validation declares `twine>=5,<7`; Twine 6.2.0 is the certified line. CI also exercises a Twine 7 candidate job so a future major-version widening is based on an actual build/metadata-validation run rather than a dependency-range edit alone.

Twine is validation tooling, not the upload credential path. PyPI publication uses GitHub OIDC Trusted Publishing. `tools/install_dependencies.py` reads declared release dependencies from `pyproject.toml` without installing the local project first, preserving the clean tagged tree required by release preflight.

The 1.2.0 build is no longer universally `py3-none-any`: wheels that contain `_livegate` are CPython/platform-specific. The source distribution includes `_livegate.c` and can fall back to a no-native installation when the optional extension cannot be compiled.

## Source availability requirements

Switch and goto decorators recognize marker syntax from retrievable function source. Functions defined in contexts where `inspect` cannot recover source—some REPL, `stdin`, notebook, generated, or dynamically executed definitions—may require the documented explicit-source path or may be untransformable.

Put production-decorated functions in normal source files whenever practical.

## Specialization boundary

`partial`, `specialize`, and `hotpath` rely on the `bytecode` package and CPython 3.13 instruction forms. Generator/async-generator specialization is currently rejected. Coroutine specialization uses wrapper-oriented fallbacks where in-frame dispatch is not appropriate.

`hotpath` may use `sys.monitoring`; a monitoring tool-slot conflict causes fallback rather than stealing another tool's slot.

## Dynamic mutation and inline binding

Inlining exposes two explicit binding contracts:

- `binding="frozen"` snapshots the eligible target state used during transformation and is appropriate for intentionally stable hot helpers;
- `binding="guarded"` validates supported target/code/default/descriptor state and falls back to the ordinary call when that state becomes stale.

Guarded binding is semantic hardening, not a synchronization primitive for arbitrary concurrent mutation.

## Compatibility expansion policy

Supporting another CPython minor version is a port, not a metadata-only change. A new interpreter line requires review of opcode forms, adaptive/specialized instruction behavior, call conventions, jumps, `EXTENDED_ARG`, exception tables, stack effects, code-object construction/layout, source/bytecode mapping, C-API availability, free-threaded behavior, verifier logic, and all transformation/native stress suites before `requires-python` is widened.
