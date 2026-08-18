# ACP list filtering and SCO compatibility bundle implementation plan

**Goal:** Push the current standalone SenseCore CLI with efficient server-side
job-state filtering and produce a separately delivered SCO v1.2.0 offline
installer for macOS arm64.

## Task 1: Integrate the remote branch safely

1. Compare remote-changed paths with local dirty paths.
2. Fast-forward to `origin/agent/acp-cli` without stashing or resetting local
   work.
3. Run the current test suite to establish the dirty-tree baseline.

## Task 2: Add state pushdown using TDD

1. Add command tests proving default RUNNING and one explicit state are passed
   to `list_jobs_in_workspace`, while multi-state and `--all` pass no service
   state.
2. Run the focused tests and record the expected failure.
3. Compute state selection before workspace queries and pass the single state
   into the HMAC listing call.
4. Add and test the CLI `--page-size` option.

## Task 3: Make HMAC overflow pagination resilient using TDD

1. Add a failing test that simulates a response-overflow error at a large page
   size and succeeds after retry at half the size.
2. Restart pagination from page one on overflow and continue halving down to a
   minimum page size of one.
3. Preserve the SCO fallback for non-overflow API errors.

## Task 4: Build the local SCO archive

1. Copy the audited v1.2.0 macOS arm64 executable into a staging directory
   outside Git.
2. Add the guarded installer, checksum manifest, and compatibility README.
3. Create the tar archive and verify extraction, checksums, temporary install,
   and reported version.

## Task 5: Document installation and compatibility

1. Explain official-current SCO versus the separately supplied pinned
   compatibility bundle.
2. Document the offline install commands and the risk of upgrading the pinned
   binary in place.
3. Update the ACP list reference with service-filter and page-size semantics.

## Task 6: Verify and publish

1. Run focused and full tests, compile sources, and install into a fresh Python
   3.12 environment for entry-point smoke tests.
2. Run diff checks and a credential/binary scan.
3. Stage only intended source and documentation paths, commit, and push
   `agent/acp-cli`.
4. Verify the remote branch SHA and record the local SCO archive path and
   checksum in the DreamDojo KB workstream changelog.
