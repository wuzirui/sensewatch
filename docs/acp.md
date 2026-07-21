# ACP command reference

```text
acp {list,stop,switch,submit,whoami,logs,refresh,quota} ...
```

Exit code `0` means success, `1` means the operation failed, and `2` means a
user-input or preflight error. Run `acp <command> --help` for the parser's exact
flag list.

## `submit`

```bash
acp submit --name NAME --gpus GPUS --command COMMAND \
  [--cpus-per-gpu N] [--mem-per-gpu-gb N] \
  [--workspace WORKSPACE] [--image IMAGE] [--env KEY1,KEY2] \
  [--console-path PATH] [--dry-run] [--force] \
  [--quota-mode fail|wait|spot]
```

Example:

```bash
acp submit \
  --name g8-training-v1 \
  --gpus 8 \
  --command "bash /mnt/afs/team/project/launch.sh" \
  --console-path /mnt/afs/team/project/runs/training-v1/console.log \
  --dry-run
```

The planner:

1. Checks a referenced shell launcher for runtime `pip install`.
2. Discovers linked workspaces with `sco ws` and caches them for 24 hours.
3. Reads resource specs and live cluster usage.
4. Filters specs by GPU divisibility and per-GPU CPU/memory requirements.
5. Prefers fewer replicas, more GPUs per replica, then lighter CPU/memory.
6. Injects `NCCL_NVLS_ENABLE=0`,
   `TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC=3600`, and
   `NCCL_SOCKET_TIMEOUT=3600000` when more than one replica is required.
7. Forwards only configured environment variables that have non-empty values.

Job names should contain a topology tag such as `g8` or `n2x8`. A missing tag
warns but does not block submission.

Quota modes:

- `fail` submits once and returns a quota error with alternatives.
- `wait` submits once with native `sco --wait`; the server may admit the job as
  `WAIT_QUOTA` or `PENDING`. There is no client retry loop.
- `spot` adds `--quota-type=spot`; spot jobs are preemptible.

The legacy `--wait-timeout` and `--wait-interval` flags remain accepted but do
not drive wait-mode polling. `--probe` is reserved and not a complete
probe-then-formal implementation; do not rely on it for production workflow.

Always use `--dry-run` to inspect workspace, cluster, spec, topology, quota,
forwarded variable names, and NCCL injection before a real submission.

## `list`

```bash
acp list [--all] [--state RUNNING,PENDING] [--experiment TEXT] \
  [--since 6h|2d|1w] [--id pt-ID] [--workspace WORKSPACE] \
  [--user NAME_OR_UUID | --all-users] [--json]
```

Defaults are RUNNING jobs owned by `[identity]`, queried across linked
workspaces. Useful examples:

```bash
acp list
acp list --all --since 2d
acp list --state RUNNING,PENDING --experiment training-v1
acp list --id pt-abc12345 --all-users
acp list --workspace team-workspace --json
```

`--id` is an exact lookup and prints a verbose one-job view. `--json` is the
stable choice for downstream scripting.

## `stop`

```bash
acp stop pt-abc12345 [--no-wait] [--timeout 30]
```

Only a canonical `pt-...` ID is accepted. The command locates one job, issues
`sco acp jobs stop`, and normally polls until the state leaves the active set.
This suspends the job; it does not delete it. `--no-wait` returns immediately.

Known limitation: the current wrapper does not classify `WAIT_QUOTA` as an
active state. To cancel such a queued job, use the native command with the
exact workspace and ID:

```bash
sco acp jobs stop --workspace-name=WORKSPACE pt-abc12345
```

## `switch`

```bash
acp switch pt-OLD --name NEW_NAME --command NEW_COMMAND \
  [--workspace WORKSPACE] [--image IMAGE] [--env KEY1,KEY2] \
  [--console-path PATH] [--dry-run] [--force] \
  [--stop-timeout 30] [--no-stop-wait]
```

`switch` reads the source job's exact workspace, cluster, worker spec, replica
count, image, and environment. It submits a copied replacement with native
server-side quota wait, then stops the source only after replacement submission
succeeds. A failed replacement leaves the source untouched.

Omit `--image` and `--env` to preserve copied values. Use `--dry-run` to inspect
the retained topology without submitting or stopping anything.

## `logs`

```bash
acp logs pt-abc12345 [--workspace WORKSPACE] [--worker N] \
  [--tail N] [--severity ERROR|WARN] [--page-size N] \
  [--output PATH] [--json]
```

The log helper discovers job UID, time range, and worker count, then queries
SenseCore user logs with pagination. Without `--worker`, formatted output adds
worker tags. `--tail` uses bounded newest-page queries when no severity filter
is present.

Examples:

```bash
acp logs pt-abc12345 --tail 200
acp logs pt-abc12345 --worker 0 --severity ERROR
acp logs pt-abc12345 --output /tmp/job.log --json
```

The default log workspace is `p18-eacv`; pass `--workspace` for jobs elsewhere.

## `quota`

```bash
acp quota [--workspace WORKSPACE] [--cap GPUS] [--json]
```

The snapshot counts RUNNING, non-spot ACP jobs and CCI apps. Pending,
suspended, stopped, terminal, and spot resources do not count against the
client-side cap. Without `--workspace`, every configured quota workspace is
shown. `--cap` overrides one configured cap for the command only.

## `whoami`

```bash
acp whoami
acp whoami --set IAM_USER_NAME --user-id IAM_USER_UUID
```

The identity controls default owner filtering for both ACP and CCI. Updating it
rewrites only the `[identity]` block.

## `refresh`

```bash
acp refresh
acp refresh --workspaces
```

Use this when a new workspace is missing, a workspace-to-cluster binding has
changed, or cached discovery is stale.
