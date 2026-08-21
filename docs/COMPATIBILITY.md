# Compatibility

## Supported runtime

The current production line supports:

- **Implementation:** CPython
- **Python:** `>=3.13,<3.14`
- **Primary certification interpreter:** CPython 3.13.5
- **Platforms:** Windows, Linux, macOS for the portable pure-Python package

CI runs the required regression suite on current CPython 3.13 GitHub-hosted runners for all three major desktop/server operating systems.

## Not currently certified

The following are not claimed as independently production-certified:

- CPython 3.13 free-threaded (`3.13t`);
- CPython 3.14+;
- PyPy, GraalPy, or other Python implementations;
- live/self-modifying switch modes under environments that prohibit or instrument the required runtime behavior.

Some imports may fail explicitly outside the supported runtime rather than attempting an unsafe transformation.

## Dependency boundary

Runtime dependency:

```text
bytecode >=0.17,<0.18
```

The upper bound is intentional. Bytecode library changes can alter instruction/CFG abstractions used by the transformation engine and must be certified before the range is widened.

## Source availability

Switch and goto decorators depend on retrievable function source for marker recognition. Functions created in contexts where `inspect` cannot recover source (for example, some interactive/`stdin` definitions) may not be transformable. Put production-decorated functions in normal source files.

## Dynamic mutation

Inlining has two explicit contracts:

- `binding="frozen"` is snapshot-oriented and assumes the target remains stable.
- `binding="guarded"` is the choice when target binding/code/default state may change after decoration.

See `COMPREHENSIVE_GUIDE.md` for the exact guarded coverage and deoptimization behavior.
