# GitHub Setup

This checkout is ready to push as a repository named `cpython-extensions`.

## Fastest path

Create an **empty** GitHub repository (do not add a README, license, or `.gitignore` because they already exist here), then from this directory:

```powershell
git remote add origin https://github.com/<OWNER>/cpython-extensions.git
git push -u origin main
```

The prepared repository already contains an initial commit made with the neutral local identity `Repository Bootstrap <repository-bootstrap@localhost>` so no personal email address is invented. If you want the first commit attributed to your own Git identity, run `git commit --amend --reset-author --no-edit` before pushing.

If you use SSH:

```powershell
git remote add origin git@github.com:<OWNER>/cpython-extensions.git
git push -u origin main
```

## Recommended repository settings

After the first push:

1. Set the default branch to `main`.
2. Enable the dependency graph and Dependabot alerts/updates. Dependency graph is required for the included Dependency Review workflow.
3. Enable code scanning (the repository includes a CodeQL workflow).
4. Enable private vulnerability reporting if available.
5. Protect `main` with pull requests and require the `CI / test` jobs plus package validation before merge.
6. Disallow force-pushes/deletion on `main`.
7. Prefer signed commits/tags for releases if your workflow supports them.

## PyPI publishing

The release workflow creates GitHub release artifacts on `v*` tags. PyPI publication is **off by default**.

After configuring a PyPI Trusted Publisher for this GitHub repository:

- create/protect a GitHub environment named `pypi`;
- set repository variable `PYPI_PUBLISH_ENABLED` to `true`.

No PyPI token needs to be stored in GitHub secrets. The publishing job runs only after the same certified artifacts have been accepted by the GitHub Release job.
Only the wheel and sdist are sent to PyPI; `SHA256SUMS.txt` stays attached to the GitHub Release for integrity verification.

## First release tag

The source version is already `1.0.3`. After the repository is pushed and its CI is green:

```powershell
git tag -a v1.0.3 -m "cpython-extensions 1.0.3"
git push origin v1.0.3
```

If `v1.0.3` already exists in the destination repository, do not overwrite it; increment the package version and changelog instead.

## Project URLs in package metadata

`pyproject.toml` intentionally does not guess your GitHub account. Once the final repository URL is known, you can add:

```toml
[project.urls]
Homepage = "https://github.com/<OWNER>/cpython-extensions"
Repository = "https://github.com/<OWNER>/cpython-extensions"
Issues = "https://github.com/<OWNER>/cpython-extensions/issues"
Documentation = "https://github.com/<OWNER>/cpython-extensions/blob/main/docs/COMPREHENSIVE_GUIDE.md"
```

Optionally add a `repository-code` field with the final GitHub URL to `CITATION.cff` once the owner is known.

## Repository metadata

Use [`.github/REPOSITORY_METADATA.md`](.github/REPOSITORY_METADATA.md) for the recommended repository name, GitHub description, topics, website, labels, and public repository settings. The project license is **Mozilla Public License 2.0 (`MPL-2.0`)**.
