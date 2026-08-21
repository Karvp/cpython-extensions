## Summary

<!-- What problem does this solve? Keep the scope explicit. -->

## Semantics / compatibility

<!-- Which switch/inline/goto modes are affected? What observable behavior and compatibility boundary must remain unchanged? -->

## Validation

- [ ] Focused regression added or updated
- [ ] `python -m pytest`
- [ ] `python -m compileall -q src tests tools benchmarks/scripts`
- [ ] `python tools/check_repo.py`
- [ ] Relevant dev-mode / differential / stress harness run for transformation changes
- [ ] Exact artifact or packaging checks run when release tooling changes
- [ ] Documentation and changelog updated for user-visible behavior

## Performance

<!-- If performance changes, include the benchmark driver and arguments, baseline, Python/platform details, raw or committed evidence, and code-size/memory tradeoffs. Explain why the optimization is general-purpose rather than fixture-specific. Do not use a machine-specific timing threshold as a correctness gate. -->

## Release impact

<!-- If this changes the public release state, confirm version, release notes, security/compatibility docs, benchmark evidence, certification/audit records, and tag instructions are synchronized. -->
