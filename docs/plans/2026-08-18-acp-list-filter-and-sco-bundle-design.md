# ACP list filtering and SCO compatibility bundle design

## Goal

Publish the current `agent/acp-cli` branch with the active DreamDojo SenseCore
policy, make the common `acp list` path query only RUNNING jobs at the service,
and prepare the known-working SCO v1.2.0 binary as a local offline installer.

## Delivery boundary

- GitHub receives only the Python package, tests, example configuration, and
  documentation.
- The SCO binary and its installer archive stay outside the repository and are
  not attached to a GitHub release.
- The existing orphan-branch contract remains unchanged: push
  `agent/acp-cli` directly and do not merge it into the SenseWatch application
  history on `main`.

## ACP list query contract

`acp list` computes the effective state selection before querying workspaces.
When exactly one state is selected, it passes that state to the HMAC
`trainingJobs` endpoint. This covers the default RUNNING query and an explicit
single `--state`. Multiple states and `--all` remain client-side filters because
the endpoint accepts only one state value.

The CLI also exposes `--page-size` as an escape hatch. HMAC pagination follows
`next_page_token`; a response-overflow error retries from the first page at a
smaller page size, down to one. Other API errors retain the existing SCO CLI
fallback.

## Offline SCO bundle

The local archive targets macOS Apple Silicon and contains:

- the current `~/.sco/bin/sco` v1.2.0 executable;
- `install.sh`, defaulting to `~/.sco` and supporting `--install-root`;
- `README.md` with install and profile-setup instructions;
- `SHA256SUMS` covering the archive contents.

The installer verifies Darwin/arm64, checks the binary digest, refuses an
existing destination unless `--force` is passed, and backs up the replaced
binary. Tests install into a temporary directory and never change the active
`~/.sco` installation.

## Verification

- Focused RED/GREEN tests for state pushdown, `--page-size`, and overflow
  backoff.
- Full pytest suite and bytecode compilation.
- Fresh virtual-environment installation and command help smoke tests.
- Archive extraction, checksum verification, temporary installation, and
  `sco version` smoke test.
- Diff/secret scan proving that no SCO executable or credentials enter Git.
