# Releasing

The release process is designed around a clean tagged commit, deterministic artifacts, explicit metadata validation, and a separate OIDC-only publishing job.

## 1. Prepare the release

1. Update `src/python_extensions/_version.py`.
2. Add the release section to `CHANGELOG.md` and `docs/RELEASE_NOTES.md`.
3. Update release-sensitive documentation: `README.md`, `SECURITY.md`, `CITATION.cff`, compatibility/setup guidance, benchmark documentation/evidence, and repository metadata where applicable.
4. Update the release certification and audit records.
5. Run the full unit suite and the relevant full-scale stress/differential harnesses.
6. Run repository hygiene and compile checks.

Recommended local gates:

```bash
python tools/check_repo.py
python -m pytest
python -m compileall -q src tests tools benchmarks/scripts
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -X dev -W error -m pytest
```

The isolated dev-mode form avoids unrelated globally installed pytest plugins affecting release certification before this project's tests are collected.

## 2. Validate release metadata

Install the declared release tooling **without installing the local project**:

```bash
python tools/install_dependencies.py \
  --include-build-system \
  --include-runtime \
  --upgrade build test
python tools/check_metadata.py
```

Do not run `pip install ".[build,test]"` before the clean-tree preflight. Installing the local project creates build metadata such as `build/` and `src/*.egg-info`, which correctly makes the checkout dirty.

## 3. Build reproducibly

`tools/build_release.py` requires a fixed Unix epoch. Use the release commit timestamp:

```bash
EPOCH=$(git show -s --format=%ct HEAD)
python tools/build_release.py --out-dir dist --epoch "$EPOCH"
```

PowerShell:

```powershell
$epoch = git show -s --format=%ct HEAD
python tools/build_release.py --out-dir dist --epoch $epoch
```

The command writes a wheel, sdist, and `SHA256SUMS.txt`.

For release certification, build twice from the same clean commit and epoch and compare the resulting wheel, sdist, and checksum manifest byte-for-byte.
Because 1.2.0 contains a native extension, also keep the exact-sdist rebuild check below: it catches compiler metadata that is stable within one checkout but embeds the source directory. The extension build suppresses POSIX debug-path metadata for this reason.

## 4. Validate exact artifacts

Validate only Python distributions with Twine; the checksum manifest is not package metadata:

```bash
python -m twine check dist/*.whl dist/*.tar.gz
```

Then:

1. verify `SHA256SUMS.txt`;
2. install and smoke-test the exact wheel;
3. extract the exact sdist and run the full suite from that source;
4. rebuild the wheel from the exact sdist using the same epoch and compare it with the shipped wheel.

The GitHub release workflow performs these checks automatically.

## 5. Tag the release

The only stable/publishable tag is the exact package version:

```bash
git tag -s v1.2.0 -m "cpython-extensions 1.2.0"
git push origin v1.2.0
```

If signing is not configured, use an unsigned annotated tag:

```bash
git tag -a v1.2.0 -m "cpython-extensions 1.2.0"
git push origin v1.2.0
```

Do not move or overwrite a stable tag after publication. If a published release needs another change, increment the package version.

### Preview/rehearsal tags

A preview suffix can exercise the build and GitHub prerelease path without changing the package version:

```bash
git tag -a v1.2.0-beta -m "cpython-extensions 1.2.0 release rehearsal"
git push origin v1.2.0-beta
```

Allowed preview labels are `alpha`, `beta`, `rc`, `preview`, and `test`, optionally followed by a numeric suffix. Preview tags certify the same package version but can never enter the PyPI publishing job.

## 6. GitHub Release behavior

A stable tag creates a normal GitHub Release. A preview tag creates a GitHub prerelease. Both use the already-certified artifacts and attach the wheel, sdist, and checksum manifest.

Release creation is API-driven through `actions/github-script`. Re-runs are idempotent:

- matching assets are retained;
- missing assets are uploaded;
- stale preview assets may be repaired;
- a stable asset whose bytes differ from the newly certified artifact causes a hard failure instead of being overwritten.

## 7. PyPI Trusted Publishing

Stable tags enter the protected PyPI job after artifact certification and GitHub Release success. Preview tags are excluded by the validated release channel.

Before the first stable publication from a repository:

1. configure `Karvp/cpython-extensions` as the Trusted Publisher for the `cpython-extensions` project on PyPI;
2. configure the GitHub environment named `pypi`;
3. add the desired required reviewers/deployment-protection rules;
4. if the tag pusher is the only reviewer, add another reviewer or disable **Prevent self-review**.

The PyPI job requests `id-token: write` and uses OIDC. It does not require a stored PyPI API token.

Do not put an environment-scoped enable variable in the job-level `if:` expression. GitHub evaluates job eligibility before entering the environment, so those variables are unavailable there. The stable-channel check decides eligibility; the protected environment supplies the human/administrative gate.

Before invoking the PyPA publishing action, the workflow stages only `*.whl` and `*.tar.gz` into `pypi-dist/`. `SHA256SUMS.txt` remains a GitHub Release integrity artifact and must not be sent to PyPI.

## 8. Failure recovery

The tag workflow verifies checksums after each artifact handoff and is designed to be safe to re-run.

If a run fails:

- **before GitHub Release creation:** fix the release commit, increment/recreate only an unpublished preview tag as appropriate, and rerun certification;
- **during preview release creation:** the API helper may repair missing/stale preview assets on rerun;
- **after a stable GitHub Release exists:** do not replace differing stable assets; investigate the mismatch and publish a new package version when necessary;
- **during PyPI upload:** determine whether PyPI accepted any file before retrying. PyPI files are immutable; if any artifact for that version was accepted, do not attempt to replace it.

The GitHub Release job explicitly requests `actions: read` and `contents: write`. The PyPI job requests `actions: read`, `contents: read`, and `id-token: write`. Keep these permissions explicit and scoped to the jobs that need them.
