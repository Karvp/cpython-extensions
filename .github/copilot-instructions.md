# Repository instructions

- Target CPython 3.13 only unless a compatibility expansion is explicitly being certified.
- Preserve Python observable semantics for the selected mode; do not add test-specific or benchmark-specific shortcuts.
- Bytecode transformations must fail closed when structural assumptions cannot be proven.
- Keep production defaults conservative: portable switch, strict goto, and the documented inline binding/policy defaults.
- Add focused regression coverage for every semantic fix and generated/adversarial coverage for generalized bytecode changes.
- Run `python -m pytest`, `python -m compileall -q src tests`, and `python tools/check_repo.py` before proposing a change.
- Treat exception tables, stack depth, source locations, argument evaluation order, descriptors, rebinding, generators/async suspension, and concurrency as observable behavior where relevant.
- `src/python_extensions/_version.py` is the single version source.
