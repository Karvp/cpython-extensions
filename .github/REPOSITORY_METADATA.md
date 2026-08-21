# GitHub Repository Metadata

Use the following values when creating or editing the GitHub repository.

## Primary metadata

- **Repository name:** `cpython-extensions`
- **Visibility:** Public
- **Default branch:** `main`
- **License:** Mozilla Public License 2.0 (`MPL-2.0`)
- **Package / PyPI distribution:** `cpython-extensions`
- **Python import package:** `python_extensions`
- **Primary language:** Python
- **Supported interpreter:** CPython 3.13.x
- **Current release:** `1.1.0`

## Recommended GitHub description

> Production-oriented CPython 3.13 bytecode extensions for switch dispatch, function inlining, and validated goto.

Shorter alternative:

> CPython 3.13 switch, inline-function, and validated-goto bytecode extensions.

Performance-oriented alternative:

> Stress-tested CPython 3.13 language extensions for fast switch dispatch, function inlining, and verified local goto.

## Recommended topics

GitHub topics are lowercase and should be added without `#` prefixes:

`python`, `cpython`, `python313`, `bytecode`, `compiler`, `optimization`, `performance`, `switch`, `function-inlining`, `goto`, `control-flow`, `metaprogramming`, `language-extensions`, `developer-tools`, `python-library`, `setuptools`, `pypi`, `mpl-2-0`

## Canonical links

- **Repository:** `https://github.com/Karvp/cpython-extensions`
- **Issues:** `https://github.com/Karvp/cpython-extensions/issues`
- **Documentation:** `https://github.com/Karvp/cpython-extensions/blob/main/docs/COMPREHENSIVE_GUIDE.md`
- **PyPI:** `https://pypi.org/project/cpython-extensions/`

Use the PyPI project page as the GitHub repository website after the desired release is published; otherwise the repository URL is a safe default.

## Suggested social preview / one-line positioning

**CPython bytecode extensions with production-oriented verification.**

## Feature labels for GitHub issues

Suggested optional labels in addition to GitHub defaults:

- `area:switch`
- `area:inline`
- `area:goto`
- `area:bytecode`
- `area:packaging`
- `area:docs`
- `type:bug`
- `type:performance`
- `type:compatibility`
- `type:security`
- `needs:regression-test`

## Repository settings worth enabling

- Require pull requests before merging into `main`.
- Require the normal CI checks to pass.
- Require branches to be up to date before merging when practical.
- Enable Dependabot alerts and security updates.
- Enable private vulnerability reporting.
- Enable secret scanning and push protection when available for the account/repository.
- Keep tag/release publishing restricted to trusted maintainers.
- Use the `pypi` environment with required reviewers when enabling PyPI Trusted Publishing.
