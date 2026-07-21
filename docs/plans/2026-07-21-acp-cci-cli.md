# ACP + CCI CLI Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish installable, documented `acp` and `cci` commands on the independent `agent/acp-cli` branch without changing the verified DreamDojo operational contract.

**Architecture:** Package the two existing CLIs under `src/sensecore_cli/`, replace DreamDojo repo-relative imports with package-relative helpers, and extract only the three generic resource parsers used by ACP. Preserve the existing config, cache, Keychain, `sco`, state, and exit-code behavior while adding packaging and CCI coverage.

**Tech Stack:** Python 3.11+, standard library, setuptools, pytest, external SenseCore `sco` CLI, macOS Keychain.

---

### Task 1: Create the installable package skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `src/sensecore_cli/__init__.py`
- Create: `tests/test_entrypoints.py`
- Create: `.gitignore`

**Step 1: Write the failing import/entry-point test**

```python
from importlib import import_module


def test_public_modules_import():
    assert import_module("sensecore_cli.acp")
    assert import_module("sensecore_cli.cci")
```

**Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_entrypoints.py -q`

Expected: FAIL because `sensecore_cli.acp` and `sensecore_cli.cci` do not exist.

**Step 3: Add minimal packaging metadata**

Define project `sensecore-acp-cli`, Python `>=3.11`, package discovery under
`src`, and console scripts:

```toml
[project.scripts]
acp = "sensecore_cli.acp:main"
cci = "sensecore_cli.cci:main"
```

Create the package marker and ignore `.venv/`, caches, build outputs, and editor
metadata. Add temporary importable ACP/CCI stubs only if needed to reach the
next TDD checkpoint.

**Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_entrypoints.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add .gitignore pyproject.toml src/sensecore_cli tests/test_entrypoints.py
git commit -m "build: initialize SenseCore CLI package"
```

### Task 2: Extract resource-spec parsing

**Files:**
- Create: `src/sensecore_cli/resources.py`
- Create: `tests/test_resources.py`

**Step 1: Write failing tests for the three generic helpers**

Cover GPU counts represented as resource quantities and as accelerator arrays,
CPU quantities with `m` suffixes, and memory quantities in bytes/GiB. Assert
the exact values used by ACP spec selection.

```python
def test_gpu_count_from_spec_reads_allocatable_resource():
    spec = {"resource": {"allocatable": {"nvidia.com/gpu": "8"}}}
    assert gpu_count_from_spec(spec) == 8
```

**Step 2: Run tests and verify failure**

Run: `python3 -m pytest tests/test_resources.py -q`

Expected: FAIL because the helpers do not exist.

**Step 3: Port only the verified helper implementations**

Copy `gpu_count_from_spec`, `cpu_allocatable_from_spec`, and
`memory_allocatable_from_spec` from the DreamDojo launcher. Do not copy any
training-job request builder, YAML generator, probe script, or runtime `pip`
installation behavior.

**Step 4: Run tests and verify pass**

Run: `python3 -m pytest tests/test_resources.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/sensecore_cli/resources.py tests/test_resources.py
git commit -m "refactor: isolate resource spec parsing"
```

### Task 3: Port shared HMAC and log helpers

**Files:**
- Create: `src/sensecore_cli/hmac_request.py`
- Create: `src/sensecore_cli/log_extract.py`
- Modify: `tests/test_entrypoints.py`

**Step 1: Add failing smoke tests**

Assert that the helper modules import without contacting the network and that
`hmac_request.main(["--help"])` / `log_extract.main(["--help"])` expose parser
help without credential lookup.

**Step 2: Run tests and verify failure**

Run: `python3 -m pytest tests/test_entrypoints.py -q`

Expected: FAIL because the helper modules are missing.

**Step 3: Port helpers with package-safe paths**

Preserve Keychain service names and signing behavior. Rename only the module
path; do not log credential values. Preserve log pagination, worker filtering,
severity filtering, tail behavior, JSON mode, and the current `p18-eacv`
default.

**Step 4: Run tests and verify pass**

Run: `python3 -m pytest tests/test_entrypoints.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/sensecore_cli/hmac_request.py src/sensecore_cli/log_extract.py tests/test_entrypoints.py
git commit -m "feat: add shared SenseCore request and log helpers"
```

### Task 4: Port ACP and its verified test suite

**Files:**
- Create: `src/sensecore_cli/acp.py`
- Create: `tests/test_acp.py`
- Modify: `tests/test_entrypoints.py`

**Step 1: Port tests first and make imports target the package**

Copy the current 84-test DreamDojo ACP suite, changing only:

```python
from sensecore_cli import acp, log_extract
```

Add an assertion that `acp.HMAC_HELPER` resolves inside the installed package
and that ACP imports no `tools` or `skills` module.

**Step 2: Run tests and verify failure**

Run: `python3 -m pytest tests/test_acp.py tests/test_entrypoints.py -q`

Expected: FAIL until ACP exists and uses package-relative dependencies.

**Step 3: Port ACP with minimal path adaptations**

- Replace `from tools ...` with `.resources` and `.log_extract`.
- Point `HMAC_HELPER` at the packaged `hmac_request.py`.
- Preserve the `~/.config/dreamdojo/acp.toml` and
  `~/.cache/dreamdojo/acp/` compatibility paths.
- Preserve current working-tree behavior, especially native `sco --wait`,
  workspace defaults, quota accounting, W&B netrc fallback, exact-ID stop
  friction, and copy-before-stop switch safety.
- Update only user-facing source-path strings that still say `tools/acp.py`.

**Step 4: Run tests and verify pass**

Run: `python3 -m pytest tests/test_acp.py tests/test_entrypoints.py -q`

Expected: all ported ACP tests pass.

**Step 5: Commit**

```bash
git add src/sensecore_cli/acp.py tests/test_acp.py tests/test_entrypoints.py
git commit -m "feat: package the ACP command"
```

### Task 5: Port CCI and add contract coverage

**Files:**
- Create: `src/sensecore_cli/cci.py`
- Create: `tests/test_cci.py`
- Modify: `tests/test_entrypoints.py`

**Step 1: Write failing CCI contract tests**

Cover:

- identity and workspace filtering
- exact internal-name preference over display-name matches
- ambiguous display-name rejection
- start/stop idempotent state gates
- restart refusal in transitional states without `--force`
- DNAT binding lookup by application UID
- parser exposure of `list`, `status`, `start`, `stop`, `restart`, and `doctor`

All lifecycle and HMAC calls must be mocked; tests must not mutate SenseCore.

**Step 2: Run tests and verify failure**

Run: `python3 -m pytest tests/test_cci.py tests/test_entrypoints.py -q`

Expected: FAIL until packaged CCI exists.

**Step 3: Port CCI with package-safe HMAC path**

Preserve command behavior, state sets, `sco`/SSH subprocess contracts,
configuration compatibility, cache sharing with ACP, and manual DNAT rebind
guidance. Change only helper paths and user-facing source-path strings.

**Step 4: Run tests and verify pass**

Run: `python3 -m pytest tests/test_cci.py tests/test_entrypoints.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/sensecore_cli/cci.py tests/test_cci.py tests/test_entrypoints.py
git commit -m "feat: package the CCI command"
```

### Task 6: Write complete setup and usage documentation

**Files:**
- Create: `README.md`
- Create: `config.example.toml`
- Create: `docs/setup.md`
- Create: `docs/configuration.md`
- Create: `docs/acp.md`
- Create: `docs/cci.md`
- Create: `docs/troubleshooting.md`

**Step 1: Add a documentation-presence test**

Extend `tests/test_entrypoints.py` to assert that README links resolve and every
document contains at least one tested command or configuration path.

**Step 2: Run the test and verify failure**

Run: `python3 -m pytest tests/test_entrypoints.py -q`

Expected: FAIL because documentation files do not exist.

**Step 3: Write documentation**

Document:

- Python and macOS prerequisites
- official `sco` installation and required component installation/upgrade
- `sco` profile setup without printing secrets
- Keychain setup for `sensecore_access_key_id` and
  `sensecore_access_key_secret`
- editable, pip, and pipx-style install paths
- full TOML schema and cache behavior
- every ACP and CCI subcommand with examples and safety notes
- `fail`, native server-side `wait`, and preemptible `spot` quota semantics
- multi-node NCCL injection
- offline log extraction
- CCI doctor/DNAT behavior
- troubleshooting for stale cache, missing workspace, old `sco --wait`, missing
  identity, quota errors, HMAC errors, and SSH/DNAT failures
- uninstall instructions

Examples use placeholders for secrets. Values are never echoed in verification
commands.

**Step 4: Run tests and link checks**

Run: `python3 -m pytest tests/test_entrypoints.py -q`

Expected: PASS.

Run: `rg -n '\]\([^)#]+\.md(#[^)]+)?\)' README.md docs | sort`

Expected: every relative Markdown target exists.

**Step 5: Commit**

```bash
git add README.md config.example.toml docs tests/test_entrypoints.py
git commit -m "docs: add ACP and CCI setup and usage guides"
```

### Task 7: Verify installation and release hygiene

**Files:**
- Modify if needed: `pyproject.toml`
- Modify if needed: package or tests found by verification

**Step 1: Run the complete local suite**

Run: `python3 -m pytest tests/ -q`

Expected: PASS.

**Step 2: Compile all Python sources**

Run: `python3 -m compileall -q src tests`

Expected: exit 0.

**Step 3: Test a clean virtual-environment install**

Create a temporary virtual environment, install the branch, and run:

```bash
acp --help
cci --help
acp submit --help
cci doctor --help
```

Expected: all commands exit 0 without contacting SenseCore.

**Step 4: Audit the package**

Run checks for untracked build output, absolute DreamDojo source paths,
repo-relative `tools`/`skills` imports, credential material, and whitespace
errors. Inspect the final diff and commit history.

Expected: no secrets, no KB-only imports, no unintended files, and no diff
errors.

**Step 5: Commit any verification fixes**

```bash
git add <explicit-files>
git commit -m "test: verify standalone CLI installation"
```

Skip this commit when verification requires no tracked changes.

### Task 8: Record provenance in the DreamDojo KB

**Files:**
- Modify: `/Users/wuzirui/dreamdojo_knowledge_base/open-ws/barista.md`
- Modify: `/Users/wuzirui/dreamdojo_knowledge_base/open-ws/barista.cn.md`

**Step 1: Inspect existing user changes**

Run: `git diff -- open-ws/barista.md open-ws/barista.cn.md`

Expected: identify and preserve all pre-existing edits.

**Step 2: Add matched English and Chinese changelog entries**

Use the current system time in `### YYYY-MM-DD Ddd HH:MM:SS` format. Record the
target repo, orphan branch, source files, test count, install commands, commit,
and remote branch URL. Keep paths, commands, hashes, and numbers untranslated.

**Step 3: Verify changelog ordering**

Run: `python3 tools/ws-monitor/check_changelog_order.py`

Expected: PASS.

Do not commit unrelated KB changes as part of the CLI branch.

### Task 9: Push the independent branch

**Files:** None.

**Step 1: Confirm publish scope**

Run: `git status -sb`, `git log --oneline --decorate --reverse`, and inspect all
tracked files.

Expected: only ACP/CCI package, tests, docs, and plans are present.

**Step 2: Push with upstream tracking**

Run: `git push -u origin agent/acp-cli`

Expected: remote branch created successfully.

**Step 3: Verify remote branch**

Run: `git ls-remote --heads origin refs/heads/agent/acp-cli` and
`gh api repos/wuzirui/sensewatch/branches/agent%2Facp-cli`.

Expected: both report the local HEAD commit.

Do not open a PR because an orphan branch has unrelated history and is intended
as an independent distribution branch.
