# Contributing

Read the [architecture](docs/guides/architecture.md),
[security model](docs/guides/security.md) and [agent guidance](AGENTS.md) first.
Keep changes focused and describe the user-visible problem and resulting behavior.
Discuss substantial architecture or security changes before implementation.
Reproduce bugs before fixing them, update affected public docs, use signed commits,
and obtain human review before merging. Do not put private repository content,
credentials, personal data or real user records in issues, commits or test evidence.
For vulnerabilities, follow [SECURITY.md](SECURITY.md).

Floatie's original code is licensed under the [MIT License](LICENSE). Preserve
existing attribution and contribute only material you have the right to provide.
Do not infer ownership or permission from Git author metadata. Bundled third-party
code remains subject to [its own licenses and notices](ui/THIRD_PARTY_NOTICES.txt).

## Contributor setup and checks

Use the pins in [ui/README.md](ui/README.md): Node 24.22.0, npm 11.19.1,
uv 0.13.1, contributor Python 3.13.15, and separate compatibility Python 3.9.23.
Git, Bash and `just` are needed for core command tests. From the tool checkout:

```sh
just --justfile ui/justfile install
just --justfile ui/justfile build
just --justfile ui/justfile test
just --justfile ui/justfile test-browser
export FLOATIE_TEST_PYTHON39="$(uv python find --no-project --managed-python --no-python-downloads "$(cat ui/.python39-version)")"
uv run --locked --project ui python -m pytest -q
# After reviewing source and rebuilt artifacts together:
git add ui/dist
just --justfile ui/justfile check-dist
```

The full Python suite covers core behavior as well as the UI. Missing interpreters,
Chromium or dependencies are unmet checks; do not silently skip required tests.
Start with a small set of relevant cases, then run the required suites. Read
failures and logs, including late review/CI comments, before requesting merge.

UI runtime serves checked-in files without Node or an install step. Keep source,
canonical notices and rebuilt `ui/dist` in the same change. Exact file names,
bytes and tracked-file parity must pass. Update licenses from actual bundled
packages when dependencies change; an npm license label is not the full notice.
Keep lockfiles and contributor pins exact. Check dependency advisories, registry
integrity and available provenance before upgrades.

## Live verification

Synthetic browser and subprocess tests do not establish live VM acceptance.
For lifecycle, authentication or integration changes, use an explicitly authorized
isolated host/account with synthetic material and scoped credentials. Verify
actual mounts before launching a harness. Start with three to five smoke cases
before larger or paid agent runs; record limits and actual cost. Never run these
against an existing user's VM just because it is available.

Verify create, login, root/worker setup, real prompt round trips, attachment,
restart/reuse and guarded cleanup where affected. Capture only synthetic evidence.
Do not overwrite installed wrappers without a temporary test install directory
and test-only PATH. Upgrade/rollback checks must preserve custom guidance, guest
edits and running worker identities. Before removing owned test resources, check
all repositories for dirty/untracked files, unpushed commits and dependent work.
Never delete shared data or kill processes you did not start.

Report actual commands/results, missing checks and remaining limitations. Keep
raw operational evidence private; product documentation should remain usable
without access to internal plans or historical validation records.
