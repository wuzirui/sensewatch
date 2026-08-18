# Configuration

ACP and CCI share the TOML file:

```text
~/.config/dreamdojo/acp.toml
```

CLI flags take precedence over TOML values. Missing TOML fields fall back to
the package defaults. `acp submit` creates a starter file only when the file is
absent and never overwrites an existing file.

The bundled DreamDojo policy current on 2026-08-05 defaults to `p18-eacv`
with a 64-GPU reporting cap. Loading an older config automatically migrates a
p1 default to p18 and removes the retired p1 quota entry; explicit p1 submits
are rejected.

## Complete schema

```toml
[defaults]
image = "registry.example.com/team/training:latest"
cpus_per_gpu = 8
mem_per_gpu_gb = 128
workspace = "team-workspace"
forward_env = ["WANDB_API_KEY", "HF_TOKEN"]

[identity]
user_name = "IAM_USER_NAME"
user_id = "IAM_USER_UUID"

[afs_mount]
id = "AFS_VOLUME_UUID"
mount_path = "/mnt/afs"
zone = "cn-sh-01e"

[workspace_quota]
"team-workspace" = 40
"second-workspace" = 56

[dnat]
eip_name = "team-eip"
external_ip = "203.0.113.10"

[dnat.port_template]
"cpu-debug" = 22022
"gpu-debug" = 22023
```

## `[defaults]`

| Key | Meaning |
| --- | --- |
| `image` | ACP container image used by `submit` unless `--image` overrides it. |
| `cpus_per_gpu` | Minimum CPU budget used during automatic spec selection. |
| `mem_per_gpu_gb` | Minimum memory budget in GiB per requested GPU. |
| `workspace` | Default ACP submit workspace; `--workspace` overrides it. |
| `forward_env` | Environment variable names copied into new jobs when present locally. |

Only names are configured in `forward_env`; values come from the current
process environment. `WANDB_API_KEY` additionally falls back to the password
of `machine api.wandb.ai` in `~/.netrc`. Missing values are reported but are
not sent as empty job variables.

## `[identity]`

`acp list` and `cci list` filter to this owner by default. Set both fields from
an ACP/CCI object's `ownership` block:

```bash
acp whoami --set IAM_USER_NAME --user-id IAM_USER_UUID
acp whoami
```

Use `--user` for a one-command override or `--all-users` to disable the owner
filter.

## `[afs_mount]`

`id` and `mount_path` become the `sco acp jobs create --storage-mount` value.
`zone` records the cluster/AFS zone. Confirm these values before any real
submission; the CLI does not infer the intended AFS volume.

## `[workspace_quota]`

These are reporting caps used by `acp quota` and submit summaries. They do not
change server-side quota. The calculation counts RUNNING, non-spot ACP jobs
and CCI apps; suspended, pending, stopped, succeeded, failed, and spot
resources are excluded.

Without `--workspace`, `acp quota` prints every workspace listed in this
section. This is intentionally independent from `[defaults].workspace`.

## `[dnat]` and `[dnat.port_template]`

`cci doctor` needs the EIP resource name and public IP. It reads the current
DNAT rules through `sco eip dnat list`, matches a TCP rule whose
`internal_instance_name` equals the CCI app UID and whose internal port is 22,
then probes SSH.

`port_template` is informational. A mismatch produces a warning; the CLI never
rewrites DNAT. A missing binding prints the exact values needed for manual
creation in the SenseCore console.

## Cache

ACP caches discovery data for 24 hours in:

```text
~/.cache/dreamdojo/acp/workspaces.json
~/.cache/dreamdojo/acp/specs.json
```

Refresh both caches:

```bash
acp refresh
```

Refresh workspace discovery only:

```bash
acp refresh --workspaces
```

`acp refresh --specs` is retained for CLI compatibility but currently does not
perform a standalone spec-only refresh. The JSON cache files are rebuildable;
removing them does not delete remote resources.

## Executable paths

Both commands expect `sco` at `~/.sco/bin/sco`. CCI additionally honors a
`SCO_BIN` environment override for its EIP calls:

```bash
SCO_BIN=/custom/path/sco cci doctor cpu-debug --dry-run
```

## Secret handling

Do not store AccessKeys, W&B tokens, HF tokens, cookies, or SSH private keys in
this TOML file. Keep SenseCore credentials in Keychain and the local `sco`
profile. Restrict the TOML file to the current user with `chmod 600`.
