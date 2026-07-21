# CCI command reference

```text
cci {list,status,start,stop,restart,doctor} ...
```

CCI manages existing debug-container apps. It does not create or delete apps.
Identity and workspace defaults come from the ACP config and workspace cache.

## App resolution

Lifecycle commands accept an internal name such as `app-abc123` or a
`display_name` such as `cpu-debug`. Resolution prefers an exact internal name,
then an exact display name. Multiple display-name matches exit with code 2 and
print internal names for disambiguation.

Common filters:

```text
--workspace WORKSPACE
--user IAM_NAME_OR_UUID
--all-users
```

## `list`

```bash
cci list [--workspace WORKSPACE] [--user USER | --all-users] \
  [--state RUNNING,SUSPENDED] [--json]
```

Examples:

```bash
cci list
cci list --workspace team-workspace --state RUNNING
cci list --all-users --json
```

The default owner comes from `[identity]`. Output includes internal name,
display name, state, cluster, spec, ready/replica count, and age.

## `status`

```bash
cci status APP [--workspace WORKSPACE]
```

Shows workspace, owner, state, cluster, spec, timestamps, image, and resource
request for one app.

## `start` and `stop`

```bash
cci start APP [--workspace WORKSPACE]
cci stop APP [--workspace WORKSPACE]
```

`start` is a no-op when the app is already in an active state. Otherwise it
sends the CCI start action and polls for an active state. `stop` is a no-op for
SUSPENDED or STOPPED; otherwise it sends the stop action and polls for
SUSPENDED. Neither command deletes the app or its definition.

## `restart`

```bash
cci restart APP [--workspace WORKSPACE] [--force]
```

- SUSPENDED, STOPPED, or FAILED: start the app.
- RUNNING: stop, wait, then start.
- Transitional states such as STARTING or PROGRESSING: refuse unless
  `--force` is supplied.
- Unknown states: refuse unless `--force` is supplied.

Use `--force` only after checking `cci status`; it may interrupt an app that is
already progressing normally.

## `doctor`

```bash
cci doctor APP [--workspace WORKSPACE] [--dry-run] \
  [--eip EIP_NAME] [--external-ip IP] \
  [--ssh-timeout SECONDS] [--ssh-interval SECONDS]

cci doctor --all [--dry-run]
```

`cci doctor` performs four stages:

1. Resolve the selected app and start/restart it if it is down.
2. Read DNAT rules from the configured EIP.
3. Match a TCP rule whose internal instance name is the app UID and whose
   internal port is 22.
4. Poll batch-mode SSH on the rule's external port until success or timeout.

Use `--dry-run` first. The DNAT lookup is read-only. If no matching rule exists,
the command exits nonzero and prints the UID, EIP, protocol, internal port, and
the remaining external-port choice for manual entry in the SenseCore console.

`--all` selects apps whose display names appear in `[dnat.port_template]`. If
several apps share a display name, it prefers a RUNNING app, then the most
recently updated app.

Prerequisites:

- `[dnat].eip_name` and `[dnat].external_ip` in the TOML file, or matching CLI
  flags
- the `sco` EIP component
- local SSH keys/config that can authenticate as `root`
- an existing manual DNAT rule for TCP port 22

## Exit codes

- `0`: requested state or SSH probe succeeded
- `1`: HMAC/CCI/SSH operation failed or the state is unsafe for the request
- `2`: missing, invalid, or ambiguous target/configuration
