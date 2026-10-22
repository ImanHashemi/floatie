# Frontend contributors

The Python server serves the checked-in `dist/` entry and bundled assets. Running
`floatie ui` requires only the existing host Python standard library; it never
installs packages, invokes Node or builds source. Workspace data stays behind the
existing token-protected Python API.

Bootstrap reads the launcher URL's `t` parameter into memory once per document.
It preserves that URL so Reload and cross-document Back can authenticate again.
The token is never rendered, copied into asset URLs, sent as a referrer, or stored
in cookies or browser storage. The existing launcher and API authentication
protocol is unchanged.

Workspace and all five Settings sections share one observation controller and
keep view state and drafts in memory across navigation. See the public
[architecture guide](../docs/guides/architecture.md) for ownership and lifecycle
boundaries. Synthetic browser checks and live VM acceptance are separate gates.

## Pinned contributor tools

Use Node **24.22.0** (`.node-version`) and its bundled npm **11.19.1**, uv
**0.13.1** (`pyproject.toml`), and Python **3.13.15** (`.python-version`). Build
and test scripts reject a different Node/npm version. uv selects the separate
contributor Python; `.python39-version` pins **3.9.23** solely for the Python 3.9
runtime compatibility subprocess, not for contributors or users.

Direct npm versions are exact in `package.json`, including React/React DOM
19.3.0, Vite 8.3.4 and TypeScript 7.0.2. `package-lock.json` and `uv.lock` pin the
complete dependency graphs with integrity hashes. pytest 9.1.1 and Playwright
1.64.0 are contributor dependencies only. npm lifecycle scripts are disabled.

Run these from the tool checkout, without a workspace or VM:

```sh
just --justfile ui/justfile install
just --justfile ui/justfile build
just --justfile ui/justfile typecheck
just --justfile ui/justfile test
just --justfile ui/justfile test-browser
git add ui/dist
just --justfile ui/justfile check-dist
```

`install` also installs the separate compatibility interpreter from
`.python39-version`. `test` compiles pure TypeScript tests into ignored
`.test-build/`, runs Node's test runner and the Python static/build/API/launcher
checks, including process-ownership regressions. Shutdown waits for confirmed
exit when Linux clears launch identity during teardown; losing identity prevents
any further signals. It uses `FLOATIE_TEST_PYTHON39` when configured, otherwise
locates the already-installed pinned interpreter; it never silently downloads
one during a test. Missing, non-executable and wrong-version interpreters fail with setup
guidance. The compatibility subprocess runs the real launcher and server under
Python 3.9, fetches the shipped entry and all referenced assets with no Node/npm
on PATH, verifies process ownership and stops only its own server.

`test-browser` runs the complete Workspace, Settings and shell contract against
the built bundle through the real Python server. It is the integrated acceptance
gate for both views. Bootstrap, Reload, cold-history and
missing-bundle cases also run in `test`. Installing Chromium is contributor
setup, never a runtime step. On Linux, Playwright's `install --with-deps chromium`
can install the required system libraries.

For direct lifecycle or full-suite pytest runs from the checkout, configure the
same interpreter explicitly:

```sh
uv python install "$(cat ui/.python39-version)"
export FLOATIE_TEST_PYTHON39="$(uv python find --no-project --managed-python --no-python-downloads "$(cat ui/.python39-version)")"
uv run --locked --project ui python -m pytest -q
```

Contributor CI runs on every pull request and every push to `main`, including
documentation-only changes. It uses exact Action commits and locked installs,
checks types, Node tests and source/artifact parity, then runs the complete Python
suite, including core commands, delivery and browser coverage, in one invocation:

```sh
uv run --locked --project ui python -m pytest -q --junitxml=python-tests.xml
```

Any skipped test in `python-tests.xml` fails CI. Git, Bash and the pinned `just`
runner are checked before testing; Chromium and the separate Python 3.9
interpreter are installed for browser and runtime compatibility coverage.

The independent Secrets workflow runs on the same events. It verifies the
official Gitleaks 8.30.1 Linux archive against a fixed SHA256, fetches all Git
history and runs `gitleaks git --redact --log-opts='--all'`. A secret removed from
the current files still fails if it remains in fetched history; output is
redacted and no broad paths are excluded. Both workflows use read-only repository
permissions, disable persisted checkout credentials and receive no configured
secrets or VM access.

`build` typechecks before Vite emits hashed assets with no source maps, external
module imports or lazy chunks. JSX uses the TypeScript automatic transform; no
React Vite plugin is installed. `check-dist` builds in a fresh temporary directory,
compares every relative filename and byte, checks that every shipped file is
tracked, and deletes only its own temporary directory. Missing, extra, ignored,
untracked and stale artifacts fail. Commit source and rebuilt `dist/` together.

If an update leaves the build missing, the server returns 503 with repair
guidance. If a bundle cannot load, visible HTML asks the user to restart/reopen.
Normal update is Git update followed by UI restart/reopen. Rollback is a Git
revert/update of source, assets and serving code, then UI restart; no VM data or
configuration migration is involved.

## Distributed notices

[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) is the canonical notice source
for code actually bundled into the shipped JavaScript. Vite reads it relative to
its config and emits the full text in a preserved leading license comment.
React, React DOM and the transitive Scheduler package are bundled; the embedded
Modernizr attribution in React DOM is retained too. The emitted CommonJS helper
from Rolldown 1.2.14 is distributed runtime code, despite Rolldown being a build
dependency. Its MIT license and the relevant upstream esbuild MIT notice are
included verbatim. The bundler itself and type packages remain contributor tools;
inspect generated helpers as well as package imports when reviewing notices.

When updating dependencies, inspect the locked build's included modules and each
package's actual license files and embedded notices. Copy required notices
verbatim; keep dependency attribution even when removing private project names.
Run `test_built_javascript_contains_distributed_notices` to check every JavaScript
entry and identical bytes from the real Python server, then rebuild and verify
`check-dist`. No public directory, source maps or notice endpoint is needed.

## Dependency updates

Before installing new versions, inspect official
[npm metadata](https://registry.npmjs.org/), [PyPI metadata](https://pypi.org/),
package source repositories, registry signatures and available provenance.
Check both complete lockfiles against [OSV](https://osv.dev/) and run `npm audit`
and `npm audit signatures`. Signatures establish registry integrity; available
provenance links packages to upstream builds. Neither promises the absence of
vulnerabilities. Keep all runtime assets local and all contributor pins exact.
