# ACP + CCI CLI Branch Design

## Goal

Publish the DreamDojo-tested SenseCore ACP and CCI command-line tools on an
independent branch of `wuzirui/sensewatch`, with no history or files inherited
from the macOS application on `main`.

## Branch contract

- Branch: `agent/acp-cli`
- History: orphan branch, unrelated to `main`
- Delivery: push the branch directly; do not open a merge PR against `main`
- Source baseline: the current DreamDojo KB working-tree versions, including
  native server-side `sco --wait`, the `p18-eacv` submit default, `p1` quota
  visibility, and W&B netrc fallback behavior

## Scope

The branch contains two public commands:

- `acp`: list, stop, switch, submit, whoami, logs, refresh, and quota
- `cci`: list, status, start, stop, restart, and doctor

It also contains only their reusable dependency closure:

- HMAC request signing and macOS Keychain credential loading
- ACP offline-log extraction
- resource-spec GPU/CPU/memory parsing
- packaging, tests, example configuration, and operator documentation

DreamDojo-specific training launchers, dataset download scripts, session
reports, fallback research notes, and model configuration generators are out of
scope. In particular, the large historical
`sensecore_acp_resource_aware_launch.py` is not copied; only its three generic
resource-spec parsing helpers are extracted.

## Package layout

```text
README.md
LICENSE
pyproject.toml
config.example.toml
docs/
  setup.md
  configuration.md
  acp.md
  cci.md
  troubleshooting.md
  plans/
src/sensecore_cli/
  __init__.py
  acp.py
  cci.py
  hmac_request.py
  log_extract.py
  resources.py
tests/
  test_acp.py
  test_cci.py
  test_entrypoints.py
  test_resources.py
```

The package uses Python 3.11+ and standard-library code. `sco` remains an
external runtime prerequisite. `pyproject.toml` installs `acp` and `cci`
console entry points.

## Compatibility

The first release is a packaging migration, not a behavioral redesign.
Existing command names, options, exit codes, state semantics, configuration
location (`~/.config/dreamdojo/acp.toml`), cache location, and Keychain service
names remain compatible. Imports and repo-relative helper paths are the only
intentional code changes.

All secret material stays outside Git. Documentation shows placeholders and
checks only for credential presence. No AccessKey, SecretKey, raw cookie,
W&B token, or Hugging Face token is committed.

## Verification

- Port and run the current ACP suite (baseline: 84 passing tests).
- Add CCI unit coverage for filtering, identifier resolution, state gates, and
  DNAT binding lookup.
- Add packaging tests proving both console entry points import and render help.
- Install into a clean virtual environment and run `acp --help` and
  `cci --help`.
- Run syntax compilation and a secret-pattern scan before commit and push.

No live job submission, stop, restart, or CCI lifecycle mutation is part of
verification.
