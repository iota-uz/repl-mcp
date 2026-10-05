# Verification

The 3.x entrypoint is the native Rust binary. Python 2.x modules remain in the
source tree to preserve regression evidence; binary wheels do not ship them.

`make verify` builds the binary, runs strict Rust fmt/clippy, lints the worker and
native acceptance tests, then runs Rust tests and the complete Python suite.
Set `CARGO_TARGET_DIR` to keep build artifacts outside the checkout if desired.
Use `make wheel` and `make sdist` for distribution checks.

The GitHub Actions matrix uses explicit `macos-15`/`ubuntu-24.04` images and covers
Python 3.10/3.12/3.14 and MCP SDK
2.2/2.3. The development extra pins SDK 2.2; the matrix deliberately installs its
selected SDK afterwards. Protocol fixtures test both legacy and current MCP.
A separate job builds and installs the source distribution. Jobs also install
the binary wheel and check the installed executable.
Both workflows have read-only repository permissions. Regression output names
each test and dumps thread stacks after 60 seconds of a stalled test. Cancellation
tests synchronize on actual worker output and readiness rather than a fixed
startup delay.

Acceptance tests use real stdio processes and local HTTP/OAuth fixtures. They
check full schemas/results, cancellation and late effects, parent death during
GIL-blocking native work, descendants, byte limits, async state and credential
validation. These are reproducible wire tests, not live agent UI evaluations.
Actual local evidence and remaining rollout limits are recorded in
[audit coverage](docs/audit-coverage.md).

## Release artifacts

`Release artifacts` builds on pushes to `rust-migration` and supports manual dispatch
with an exact 40-character `revision` SHA. Each job checks out and verifies that
revision (or the triggering push SHA). Publication waits for both the complete
Test matrix and these artifact checks. It has read-only repository access
and does not publish a release or upload to PyPI.

The three wheel jobs build locked, stripped release binaries with Maturin 1.15.0:
macOS arm64 on `macos-15`, macOS x86_64 on `macos-15-intel`, and Linux x86_64 in
`manylinux_2_28` (glibc 2.28+). A fourth job builds the source archive and installs
that archive through its own build backend. Each artifact is installed in a fresh
venv and tested outside the checkout with `tests/fixtures/installed_smoke.py`.
The smoke checks version, tool inventory, lazy startup, sibling Python selection,
pinned packages, state across cells and semantic Python errors over real stdio.

Artifacts are retained for 14 days as `repl-mcp-macos-arm64`,
`repl-mcp-macos-x86_64`, `repl-mcp-linux-x86_64` and `repl-mcp-sdist`.
After every job succeeds, the release coordinator downloads them, verifies the
filenames/checksums and attaches them to the GitHub release for that same commit.
GitHub Releases is the distribution channel; this workflow has no PyPI credentials.
