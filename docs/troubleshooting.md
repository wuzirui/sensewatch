# Troubleshooting

## `sco` is missing

```bash
test -x ~/.sco/bin/sco
export PATH="$HOME/.sco/bin:$PATH"
sco version
```

Re-run the official installer from the setup guide if the binary is absent.

## Native wait says `unknown flag: --wait`

The ACP component is too old for server-side quota waiting:

```bash
sco components upgrade
sco acp jobs create --help | rg -- '--wait'
```

Do not replace native wait with a client polling loop. Use `--quota-mode fail`
or `--quota-mode spot` until the component is upgraded.

## Component manifest download fails

If `sco components list`, install, or upgrade reports an HTTP error, verify
that `https://sco.sensecore.cn` is reachable without an intercepting proxy and
retry from the supported network. Existing installed components can still be
checked with their own `--help` commands. Do not reinstall components inside
an ACP startup script.

## HMAC or credential errors

Check presence without revealing values:

```bash
security find-generic-password -a "$USER" -s sensecore_access_key_id >/dev/null
security find-generic-password -a "$USER" -s sensecore_access_key_secret >/dev/null
sco config profiles list
```

The Keychain account must match the Python process user. Recreate a Keychain
item with `security add-generic-password -U` if the account or service name is
wrong. Do not paste credential output into bug reports.

## Workspace is missing or reported as unlinked

Refresh workspace discovery, then query the exact workspace:

```bash
acp refresh --workspaces
acp list --workspace WORKSPACE --all --all-users
```

Confirm `sco ws instances list` shows the workspace as ACTIVE and that its
description includes an active AEC2 binding.

## `acp list --id` briefly says not found

New jobs may take time to appear in indexed list results. Broaden the query:

```bash
acp list --all --since 2h --workspace WORKSPACE --all-users
sco acp jobs describe --workspace-name=WORKSPACE pt-JOB_ID
```

Do not assume submission failed when `sco` already returned a job ID.

## Quota is full

```bash
acp quota
acp submit ... --quota-mode fail
acp submit ... --quota-mode wait
acp submit ... --quota-mode spot
```

`wait` is one server-side `sco --wait` request. `spot` is preemptible. Neither
mode silently changes GPU count, worker spec, workspace, batch size, or image.

## A startup script is rejected for `pip install`

Install dependencies in the persistent environment before submission and
remove runtime installation from the launcher. `--force` bypasses the gate but
should be reserved for a reviewed false positive.

## A `WAIT_QUOTA` job does not stop through the wrapper

The current ACP wrapper's active-state set does not include `WAIT_QUOTA`. Use
the exact native command:

```bash
sco acp jobs stop --workspace-name=WORKSPACE pt-JOB_ID
```

This suspends rather than deletes the job.

## `acp logs` returns no lines

Pass the correct workspace, broaden the query, and avoid combining a narrow
severity filter with an incorrect worker:

```bash
acp logs pt-JOB_ID --workspace WORKSPACE --tail 200
acp logs pt-JOB_ID --workspace WORKSPACE --worker 0
```

For a distributed failure, inspect every worker and find the first Python
traceback; a later NCCL timeout or generic `ChildFailedError` is often only a
symptom.

## CCI target is ambiguous

Use the internal `app-...` name printed by the error:

```bash
cci list --all-users --workspace WORKSPACE
cci status app-EXACT_ID --workspace WORKSPACE
```

## `cci doctor` cannot find a DNAT rule

The doctor intentionally does not mutate DNAT. In the SenseCore console,
create or repair a TCP rule with:

- the configured EIP
- `internal_instance_name` equal to the current CCI app UID
- internal port 22
- the chosen external port

Then rerun:

```bash
cci doctor APP --dry-run
cci doctor APP
```

## SSH probe fails with an existing binding

Check the app is RUNNING, the rule targets the current UID rather than a
replaced app, the external IP/port are correct, and local SSH authentication is
non-interactive. Reproduce the exact probe manually:

```bash
ssh -o BatchMode=yes -p EXTERNAL_PORT root@EXTERNAL_IP true
```

## Debug output

The wrappers keep concise output by default. Use native `sco --debug` commands
for transport details, but scrub request headers, credential material, and raw
cookies before sharing logs.
