#!/usr/bin/env python3
"""Agent-friendly ACP CLI — submit, list, stop, logs with auto spec/topology/env.

Usage:
    acp submit --gpus 8 --command "bash /mnt/afs/.../launch.sh" --name g8-ember
    acp submit --gpus 8 ... --quota-mode wait   # native server-side WAIT_QUOTA
    acp submit --gpus 8 ... --quota-mode spot   # submit to spot pool (preemptible)
    acp list                          # my running jobs across all linked workspaces
    acp list --state running,pending --since 2d
    acp list --id pt-abc12345         # verbose single-job view
    acp quota                         # workspace quota snapshot (RUNNING non-spot ACP + CCI)
    acp stop pt-abc12345
    acp switch pt-abc12345 --name g8-new --command "bash /mnt/afs/.../new.sh"
    acp logs pt-abc12345 --tail 50

Packaged with reusable resource parsing and offline-log extraction helpers.
"""

from __future__ import annotations

import argparse
import json
import netrc as netrc_module
import os
import re
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from . import log_extract
from .resources import (
    cpu_allocatable_from_spec,
    gpu_count_from_spec,
    memory_allocatable_from_spec,
)


# =============================================================================
# Constants bootstrapped into ~/.config/dreamdojo/acp.toml
# =============================================================================

HMAC_HELPER = Path(__file__).resolve().with_name("hmac_request.py")

DEFAULT_IMAGE = "registry.cn-sh-01.sensecore.cn/zhicheng_ccr/zirui-dev:test-20260325125726"
DEFAULT_SUBSCRIPTION = "0197ee17-b6eb-7846-b2b4-a77c5f509b92"
DEFAULT_RG = "default"
DEFAULT_WORKSPACE = "p18-eacv"
DEFAULT_WORKSPACE_ZONE = "cn-sh-01z"
DEFAULT_CLUSTER_ZONE = "cn-sh-01e"
DEFAULT_AFS_ID = "019b308b-aa56-79af-b03c-7ce01c67e032"
AEC2_BASE = "https://aec2.cn-sh-01.sensecoreapi.cn"
CCI_BASE = "https://cci.cn-sh-01.sensecore.cn"

CACHE_DIR = Path.home() / ".cache" / "dreamdojo" / "acp"
CONFIG_PATH = Path.home() / ".config" / "dreamdojo" / "acp.toml"
CACHE_TTL_SEC = 24 * 3600

# Last-resort fallback if both `sco ws instances list` and HMAC discovery fail.
# Kept deliberately small — stable, long-lived workspaces only. Do NOT add
# renamed or transient workspaces here; rely on `sco ws` for fresh discovery.
KNOWN_WORKSPACE_IDS = {
    "p18-eacv": "019ebac3-c824-7701-9031-6d48f581ae12",
    "p1-video-world-model-for-robot-learning": "019d9523-828e-7198-88c1-fa43e4b13b93",
    "share-space-01e": "01995848-9da4-7b9a-917c-db5bdea185e5",
}

ACTIVE_STATES = {"RUNNING", "CREATING", "STARTING", "INIT", "PENDING", "QUEUEING", "WAITING"}
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "DELETED", "DELETING", "SUSPENDED", "STOPPED"}
SPOT_QUOTA_TYPES = {"SPOT"}

JOB_ID_RE = re.compile(r"^pt-[a-z0-9]+$")
TOPOLOGY_TAG_RE = re.compile(r"g\d+|n\d+x\d+")


# =============================================================================
# Exceptions
# =============================================================================


class APIError(Exception):
    """HMAC API or `sco` subprocess failure."""


class JobNotFound(Exception):
    """Job ID not found in any cached workspace."""


class NoSpecFit(Exception):
    """No cluster/spec combination fits the request."""


# =============================================================================
# Config — TOML load + first-run bootstrap
# =============================================================================


def default_config() -> dict[str, Any]:
    return {
        "defaults": {
            "image": DEFAULT_IMAGE,
            # 8 CPU / 128 GB per GPU matches the lightest real 8-GPU spec
            # (N6lS.Iu.I10.8.64c1024g). 4-GPU and 1-GPU light variants
            # exceed these, so the "prefer lighter" sort still wins.
            "cpus_per_gpu": 8,
            "mem_per_gpu_gb": 128,
            "workspace": DEFAULT_WORKSPACE,
            "forward_env": ["WANDB_API_KEY", "HF_TOKEN"],
        },
        "identity": {
            # `acp list` filters to jobs owned by this user by default. Populate
            # manually (values visible in any job's `ownership` block) or let
            # `acp submit` auto-learn by describing the job after create.
            "user_name": "",
            "user_id": "",
        },
        "afs_mount": {
            "id": DEFAULT_AFS_ID,
            "mount_path": "/mnt/afs",
            "zone": DEFAULT_CLUSTER_ZONE,
        },
        # Workspace-level GPU caps. Regular quota is workspace-wide and only
        # RUNNING non-spot ACP jobs / CCI apps count against it.
        "workspace_quota": {
            "p18-eacv": 40,
            "p1-video-world-model-for-robot-learning": 56,
        },
    }


def load_config(path: Path) -> dict[str, Any]:
    """Load TOML config, overlay onto built-in defaults. Missing file → pure defaults."""
    cfg = default_config()
    if not path.exists():
        return cfg
    with open(path, "rb") as fh:
        user = tomllib.load(fh)
    for section, values in user.items():
        if section in cfg and isinstance(cfg[section], dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    return cfg


def bootstrap_config(path: Path) -> None:
    """Write starter acp.toml if absent. Idempotent — never overwrites."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    body = (
        "# SenseCore ACP CLI configuration. CLI flags always override.\n\n"
        "[defaults]\n"
        f'image = "{cfg["defaults"]["image"]}"\n'
        f'cpus_per_gpu = {cfg["defaults"]["cpus_per_gpu"]}\n'
        f'mem_per_gpu_gb = {cfg["defaults"]["mem_per_gpu_gb"]}\n'
        f'workspace = "{cfg["defaults"]["workspace"]}"\n'
        f'forward_env = {json.dumps(cfg["defaults"]["forward_env"])}\n\n'
        "[identity]\n"
        "# auto-populated by `acp submit` on first successful create.\n"
        'user_name = ""\n'
        'user_id = ""\n\n'
        "[afs_mount]\n"
        f'id = "{cfg["afs_mount"]["id"]}"\n'
        f'mount_path = "{cfg["afs_mount"]["mount_path"]}"\n'
        f'zone = "{cfg["afs_mount"]["zone"]}"\n\n'
        "[workspace_quota]\n"
        "# Workspace GPU caps. Regular quota is workspace-wide and only RUNNING\n"
        "# non-spot ACP jobs / CCI apps count against it (suspended/pending/spot do NOT count).\n"
        + "".join(
            f'"{ws}" = {cap}\n'
            for ws, cap in cfg["workspace_quota"].items()
        )
    )
    path.write_text(body, encoding="utf-8")


def save_identity(path: Path, user_name: str, user_id: str) -> None:
    """Write or update `[identity]` in the user's acp.toml. Preserves other sections verbatim."""
    if not user_name and not user_id:
        return
    if not path.exists():
        bootstrap_config(path)
    text = path.read_text(encoding="utf-8")
    # Replace or append the `[identity]` block.
    lines = text.splitlines()
    out: list[str] = []
    in_identity = False
    saw_identity = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[identity]":
            in_identity = True
            saw_identity = True
            out.append(line)
            out.append("# auto-populated by `acp submit`.")
            out.append(f'user_name = "{user_name}"')
            out.append(f'user_id = "{user_id}"')
            continue
        if in_identity:
            # Skip existing identity body until next section or blank-then-section.
            if stripped.startswith("[") and stripped.endswith("]"):
                in_identity = False
                out.append("")
                out.append(line)
            # else drop old identity lines
            continue
        out.append(line)
    if not saw_identity:
        if out and out[-1].strip():
            out.append("")
        out.append("[identity]")
        out.append("# auto-populated by `acp submit`.")
        out.append(f'user_name = "{user_name}"')
        out.append(f'user_id = "{user_id}"')
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# =============================================================================
# Cache — 24h TTL JSON files
# =============================================================================


def cache_is_fresh(path: Path, ttl_sec: int) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < ttl_sec


def load_cache(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# =============================================================================
# HMAC client — wraps the packaged hmac_request.py helper
# =============================================================================


class HMACClient:
    """Thin wrapper around the HMAC helper subprocess for GET/POST."""

    def __init__(self, service_base: str = AEC2_BASE, timeout: int = 30) -> None:
        self.service_base = service_base
        self.timeout = timeout

    def _invoke(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        cmd = [
            sys.executable, str(HMAC_HELPER),
            "--service-base", self.service_base,
            "--path", path,
            "--method", method,
            "--timeout", str(self.timeout),
        ]
        if body is not None:
            cmd += ["--data", json.dumps(body)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 10)
        if r.returncode != 0:
            raise APIError(
                f"{method} {path} failed (rc={r.returncode}): "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )
        return _parse_hmac_body(r.stdout)

    def get(self, path: str) -> dict[str, Any]:
        return self._invoke("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("POST", path, body=body)


def _short_api_error(raw: str) -> str:
    """Reduce a multi-line HMAC/sco error to a one-line summary: `STATUS <n> <reason>`."""
    status_m = re.search(r"STATUS\s+(\d+)", raw)
    status = status_m.group(1) if status_m else "?"
    # Try to extract the message field from the JSON body
    msg_m = re.search(r'"message"\s*:\s*"([^"]+)"', raw)
    if msg_m:
        return f"status={status} {msg_m.group(1)}"
    return f"status={status}"


def _parse_hmac_body(text: str) -> dict[str, Any]:
    """Helper prints `STATUS <code>\\n<JSON>\\n` — extract the JSON."""
    lines = text.splitlines()
    if lines and lines[0].startswith("STATUS "):
        lines = lines[1:]
    blob = "\n".join(lines).strip()
    if not blob:
        return {}
    return json.loads(blob)


# =============================================================================
# Workspace & spec discovery
# =============================================================================


def parse_sco_ws_list_table(raw: str) -> list[str]:
    """Extract active workspace names from `sco ws instances list` tabular output."""
    names: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name, _display, state = cells[0], cells[1], cells[2]
        if name == "NAME":  # header row
            continue
        if state.upper() == "ACTIVE":
            names.append(name)
    return names


def parse_sco_ws_describe_clusters(payload: dict[str, Any]) -> list[str]:
    """Extract active cluster names from `sco ws instances describe -o json` output."""
    clusters: list[str] = []
    for aec2 in (payload.get("properties") or {}).get("aec2s") or []:
        if aec2.get("aec2_state") != "ACTIVE":
            continue
        name = aec2.get("aec2_name")
        if name and name not in clusters:
            clusters.append(name)
    return clusters


def parse_resource_specs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize resourceSpecs response to `[{name, gpu, cpu, mem_gb}]`."""
    out = []
    for spec in payload.get("resource_specs") or []:
        out.append({
            "name": spec.get("name", ""),
            "gpu": gpu_count_from_spec(spec),
            "cpu": cpu_allocatable_from_spec(spec),
            "mem_gb": memory_allocatable_from_spec(spec),
        })
    return out


def discover_workspaces(
    client: HMACClient | None = None,
    *,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return active workspaces with cluster bindings, using cache unless stale/forced.

    Uses `sco ws instances list` + `sco ws instances describe` (which our auth can
    access) rather than the HMAC `workspaceAEC2Bindings` endpoint (always 403s on
    our AK/SK — it needs `workspace.instance.get` at Resources scope that we lack).
    Falls back to `KNOWN_WORKSPACE_IDS` only if the sco CLI itself errors.

    `client` kept for backward-compat with existing callers; unused.
    """
    del client  # unused, kept for signature stability
    cache_path = CACHE_DIR / "workspaces.json"
    if not force_refresh and cache_is_fresh(cache_path, CACHE_TTL_SEC):
        cached = load_cache(cache_path)
        if cached:
            return cached

    try:
        names = _sco_ws_list_names()
        workspaces = []
        for name in names:
            payload: dict[str, Any] = {}
            clusters: list[str] = []
            try:
                payload = _sco_ws_describe(name)
                clusters = parse_sco_ws_describe_clusters(payload)
            except APIError as exc:
                print(f"[acp] warning: ws describe failed for {name}: {exc}", file=sys.stderr)
            workspaces.append({
                "name": name,
                "resource_id": payload.get("id") or None,
                "clusters": clusters,
            })
    except APIError as exc:
        print(f"[acp] workspace discovery via sco failed: {exc}; "
              f"using hardcoded fallback ({', '.join(KNOWN_WORKSPACE_IDS)})",
              file=sys.stderr)
        workspaces = [
            {"name": name, "resource_id": rid, "clusters": []}
            for name, rid in KNOWN_WORKSPACE_IDS.items()
        ]
    save_cache(cache_path, workspaces)
    return workspaces


def filter_requested_workspace(
    workspaces: list[dict[str, Any]],
    workspace_name: str | None,
    *,
    refresh_fn: Callable[[], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    if not workspace_name:
        return workspaces
    selected = [w for w in workspaces if w["name"] == workspace_name]
    if selected or refresh_fn is None:
        return selected
    refreshed = refresh_fn()
    return [w for w in refreshed if w["name"] == workspace_name]


def discover_spec_catalog(
    client: HMACClient,
    clusters: Iterable[str],
    *,
    force_refresh: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch and cache per-cluster spec catalogs."""
    cache_path = CACHE_DIR / "specs.json"
    if not force_refresh and cache_is_fresh(cache_path, CACHE_TTL_SEC):
        cached = load_cache(cache_path)
        if cached:
            return cached

    catalog: dict[str, list[dict[str, Any]]] = {}
    for cluster in clusters:
        path = (
            f"/compute/aec2/data/v1/subscriptions/{DEFAULT_SUBSCRIPTION}"
            f"/resourceGroups/{DEFAULT_RG}/zones/{DEFAULT_CLUSTER_ZONE}"
            f"/aec2s/{cluster}/resourceSpecs"
        )
        try:
            payload = client.get(path)
            catalog[cluster] = parse_resource_specs(payload)
        except APIError as exc:
            print(f"[acp] warning: resourceSpecs failed for {cluster}: {exc}", file=sys.stderr)
            catalog[cluster] = []
    save_cache(cache_path, catalog)
    return catalog


# =============================================================================
# Filter parsing
# =============================================================================


def parse_since(text: str) -> int:
    """`6h` → 21600, `2d` → 172800, `1w` → 604800."""
    m = re.fullmatch(r"(\d+)([hdw])", text.strip())
    if not m:
        raise ValueError(f"invalid --since value {text!r}; use e.g. 6h, 2d, 1w")
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"h": 3600, "d": 86400, "w": 7 * 86400}[unit]
    return n * mult


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text: str) -> int:
    """`30` or `30s` → 30; `30m` → 1800; `2h` → 7200; `1d` → 86400; `1w` → 604800.

    Bare integers are treated as seconds. Superset of parse_since — includes `s`
    and `m` for legacy wait timeout/interval flags.
    """
    m = re.fullmatch(r"(\d+)([smhdw]?)", text.strip())
    if not m:
        raise ValueError(
            f"invalid duration {text!r}; use e.g. 30, 30s, 30m, 2h, 1d, 1w"
        )
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * _DURATION_UNITS[unit]


def parse_state_list(text: str) -> list[str]:
    return [s.strip().upper() for s in text.split(",") if s.strip()]


def is_valid_job_id(s: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(s or ""))


# =============================================================================
# Quota detection and client-side workspace-quota math
# =============================================================================

_QUOTA_MARKERS = (
    "member_quota_exceeded",
    "quota_exceeded",
    "quotaexceeded",
    "quota exceeded",
    "exceeds quota",
    "exceed the quota",
    "insufficient quota",
)

_NATIVE_WAIT_UNSUPPORTED_MARKERS = (
    "unknown flag: --wait",
    "unknown option: --wait",
    "flag provided but not defined: -wait",
    "unrecognized option '--wait'",
)


def is_quota_error(text: str) -> bool:
    """Return True when an sco/HMAC error message reports a workspace-quota breach.

    The platform reports this as `MEMBER_QUOTA_EXCEEDED` (HTTP 409) but the exact
    surface wording has varied. Matches common substrings case-insensitively.
    """
    if not text:
        return False
    lower = text.lower()
    return any(marker in lower for marker in _QUOTA_MARKERS)


def is_native_wait_unsupported(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(marker in lower for marker in _NATIVE_WAIT_UNSUPPORTED_MARKERS)


def combined_sco_output(stdout: str, stderr: str) -> str:
    """Concatenate sco stdout+stderr for substring-based error classification.

    `sco` often writes the generic `Error: component "acp" exited with error`
    wrapper to stderr while the real cause (server response body, validation
    failure, MEMBER_QUOTA_EXCEEDED) lands on stdout. Concatenating lets
    `is_quota_error` and friends see both streams.
    """
    return f"{(stdout or '').strip()}\n{(stderr or '').strip()}".strip()


def format_sco_failure(cmd: list[str], rc: int, stdout: str, stderr: str) -> str:
    """Multi-line dump for sco failures whose stderr is the unhelpful
    `Error: component "acp" exited with error` wrapper. Shows rc plus the full
    contents of both streams indented, plus the failing command (with any
    long `--command=...` value truncated so it doesn't flood the terminal)."""
    def indent(text: str) -> str:
        text = (text or "").rstrip()
        if not text:
            return "    (empty)"
        return "\n".join("    " + ln for ln in text.splitlines())

    pretty_cmd = []
    for tok in cmd:
        if tok.startswith("--command=") and len(tok) > 120:
            pretty_cmd.append(tok[:117] + "...")
        else:
            pretty_cmd.append(tok)
    out = (
        f"[acp submit] error: sco create failed (rc={rc})\n"
        f"  stderr:\n{indent(stderr)}\n"
        f"  stdout:\n{indent(stdout)}\n"
        f"  cmd: {' '.join(pretty_cmd)}"
    )
    if "--wait" in cmd and is_native_wait_unsupported(combined_sco_output(stdout, stderr)):
        out += (
            "\n  hint: native quota wait requires sco acp v2.1.8+; "
            "run `sco components upgrade`."
        )
    return out


def format_quota_error_hint(err: str) -> str:
    """Multi-line message for --quota-mode=fail when quota blocks submission.

    Explicitly names wait/spot alternatives so the agent can redirect without
    re-reading the whole CLI help.
    """
    detail = (err or "(empty)").strip().splitlines()
    first = detail[0] if detail else "(empty)"
    return (
        "[acp submit] quota exceeded — workspace cap reached (RUNNING spot jobs/apps do not count)\n"
        f"  detail        {first}\n"
        "  mode          --quota-mode=fail (default — fail fast)\n"
        "  other modes   rerun with one of:\n"
        "                  --quota-mode=wait   submit once with native sco --wait;\n"
        "                                      server may admit as WAIT_QUOTA\n"
        "                  --quota-mode=spot   submit to the spot pool (small & unstable;\n"
        "                                      jobs can be preempted at any time)\n"
        "  check first   acp quota\n"
        "                acp list --all-users --state RUNNING\n"
        "                sco aec2 clusters usage --name=computing-cluster-01e"
    )


def compute_workspace_quota_usage(
    workspace: str,
    *,
    quota_cap: int,
    list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    cci_list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Aggregate RUNNING ACP/CCI GPU draw in a workspace vs its client-side cap.

    Per platform behavior, regular quota is workspace-wide, RUNNING-only, and
    excludes SPOT/idle resources. Suspended/pending jobs and apps neither
    consume nor free quota. `quota_cap=0` means "no cap configured" (available
    is reported as unknown).
    """
    lister = list_fn or (lambda ws: list_jobs_in_workspace(ws, user_name=None, state="RUNNING"))
    cci_lister = cci_list_fn or (list_cci_apps_in_workspace if list_fn is None else (lambda ws: []))
    jobs = lister(workspace)
    cci_apps = cci_lister(workspace)
    running = [j for j in jobs if j.get("state", "").upper() == "RUNNING"]
    running_cci = [a for a in cci_apps if a.get("state", "").upper() == "RUNNING"]
    regular_jobs = [j for j in running if not is_spot_quota_resource(j)]
    spot_jobs = [j for j in running if is_spot_quota_resource(j)]
    regular_cci = [a for a in running_cci if not is_spot_quota_resource(a)]
    spot_cci = [a for a in running_cci if is_spot_quota_resource(a)]
    job_used = sum(int(j.get("gpus", 0) or 0) for j in regular_jobs)
    cci_used = sum(int(a.get("gpus", 0) or 0) for a in regular_cci)
    spot_job_used = sum(int(j.get("gpus", 0) or 0) for j in spot_jobs)
    spot_cci_used = sum(int(a.get("gpus", 0) or 0) for a in spot_cci)
    used = job_used + cci_used
    spot_excluded = spot_job_used + spot_cci_used
    if quota_cap <= 0:
        available: int | None = None
    else:
        available = max(0, quota_cap - used)
    return {
        "workspace": workspace,
        "cap": quota_cap,
        "used": used,
        "job_used": job_used,
        "cci_used": cci_used,
        "spot_job_used": spot_job_used,
        "spot_cci_used": spot_cci_used,
        "spot_excluded": spot_excluded,
        "available": available,
        "running_jobs": len(regular_jobs),
        "running_cci_apps": len(regular_cci),
        "running_spot_jobs": len(spot_jobs),
        "running_spot_cci_apps": len(spot_cci),
    }


def resource_quota_type(resource: dict[str, Any]) -> str:
    scheduling = resource.get("scheduling") or {}
    quota_type = resource.get("quota_type") or scheduling.get("quota_type") or ""
    return str(quota_type).strip().upper()


def is_spot_quota_resource(resource: dict[str, Any]) -> bool:
    return resource_quota_type(resource) in SPOT_QUOTA_TYPES


# =============================================================================
# List filtering & formatting
# =============================================================================


def filter_jobs(
    jobs: list[dict[str, Any]],
    *,
    states: list[str] | None = None,
    experiment: str | None = None,
    since_sec: int | None = None,
    job_id: str | None = None,
    workspace: str | None = None,
    user: str | None = None,
) -> list[dict[str, Any]]:
    out = jobs
    if states:
        up = {s.upper() for s in states}
        out = [j for j in out if j.get("state", "").upper() in up]
    if experiment:
        needle = experiment.lower()
        out = [
            j for j in out
            if needle in j.get("display_name", "").lower()
            or needle in j.get("name", "").lower()
        ]
    if since_sec is not None:
        out = [j for j in out if j.get("age_sec", 0) <= since_sec]
    if job_id:
        out = [j for j in out if j.get("id") == job_id]
    if workspace:
        out = [j for j in out if j.get("workspace") == workspace]
    if user:
        out = [
            j for j in out
            if j.get("owner_user_name") == user or j.get("owner_user_id") == user
        ]
    return out


def format_age(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def format_list_compact(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "(no matching jobs; use --all to include non-running states, or --debug to see API calls)"
    header = f"{'ID':<14}{'STATE':<10}{'AGE':<6}{'GPU':<5}{'CLUSTER':<26}{'WORKSPACE':<18}NAME"
    rows = [header]
    for j in jobs:
        rows.append(
            f"{j.get('id', '')[:13]:<14}"
            f"{j.get('state', '')[:9]:<10}"
            f"{format_age(j.get('age_sec', 0)):<6}"
            f"{j.get('gpus', 0):<5}"
            f"{j.get('cluster', '')[:25]:<26}"
            f"{j.get('workspace', '')[:17]:<18}"
            f"{j.get('display_name') or j.get('name', '')}"
        )
    return "\n".join(rows)


def format_list_verbose(job: dict[str, Any]) -> str:
    state = job.get("state", "UNKNOWN")
    age = format_age(job.get("age_sec", 0))
    return (
        f"{job.get('id', '')}\n"
        f"  state       {state} ({'running' if state == 'RUNNING' else state.lower()} for {age})\n"
        f"  owner       {job.get('owner_user_name', '') or '(unknown)'}\n"
        f"  workspace   {job.get('workspace', '')}\n"
        f"  cluster     {job.get('cluster', '')}\n"
        f"  spec        {job.get('spec', '')} ({job.get('gpus', 0)} GPU × {job.get('replicas', 1)} replica)\n"
        f"  command     {job.get('command', '')}\n"
        f"  console     {job.get('console', '(not recorded; pass --console-path to submit)')}\n"
        f"  next        acp logs {job.get('id', '')} --tail 50"
    )


# =============================================================================
# Submit — spec selection, NCCL injection, preflight
# =============================================================================


def plan_submission(
    *,
    requested_gpus: int,
    cpus_per_gpu: int,
    mem_per_gpu_gb: int,
    spec_catalog: dict[str, list[dict[str, Any]]],
    cluster_usage: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Pick (cluster, spec, topology) that satisfies the request.

    Algorithm:
    1. For each cluster, find specs where spec.gpu ≤ requested_gpus AND requested_gpus % spec.gpu == 0.
    2. Filter specs by cpu_per_gpu and mem_per_gpu_gb constraints.
    3. Prefer single-replica (spec.gpu == requested_gpus), then larger gpus_per_replica.
    4. Within candidates, prefer lightest cpu/mem (per CLAUDE.md).
    5. Rank clusters by idle-fit; submit to first fit.
    """
    rejection_reasons: list[str] = []
    candidates: list[dict[str, Any]] = []

    for cluster, specs in spec_catalog.items():
        usage = cluster_usage.get(cluster, {})
        idle_gpu = usage.get("idle_gpu", 0)

        if idle_gpu < requested_gpus:
            rejection_reasons.append(
                f"{cluster}: {idle_gpu} idle GPU, need {requested_gpus}"
            )
            continue

        for spec in specs:
            gpus_per = spec.get("gpu", 0)
            if gpus_per <= 0 or requested_gpus % gpus_per != 0:
                continue
            replicas = requested_gpus // gpus_per
            needed_cpu = cpus_per_gpu * gpus_per
            needed_mem = mem_per_gpu_gb * gpus_per
            if spec.get("cpu", 0) < needed_cpu or spec.get("mem_gb", 0) < needed_mem:
                continue
            candidates.append({
                "cluster": cluster,
                "spec_name": spec["name"],
                "spec": spec,
                "gpus_per_replica": gpus_per,
                "replicas": replicas,
                "total_gpus": requested_gpus,
                "idle_gpu": idle_gpu,
            })

    if not candidates:
        raise NoSpecFit(
            f"no fit for --gpus {requested_gpus}:\n  " + "\n  ".join(rejection_reasons)
            if rejection_reasons else
            f"no fit for --gpus {requested_gpus}: no cluster has a matching spec with enough CPU/memory"
        )

    # Rank: single-replica first, then higher gpus_per_replica, then lightest cpu, then lightest mem
    candidates.sort(key=lambda c: (
        c["replicas"],                   # fewer replicas first
        -c["gpus_per_replica"],          # more GPUs per replica first (tiebreak)
        c["spec"].get("cpu", 0),         # lightest cpu
        c["spec"].get("mem_gb", 0),      # lightest mem
    ))
    return candidates[0]


def read_wandb_api_key_from_netrc(netrc_path: Path | None = None) -> str:
    """Return the local W&B API key from ~/.netrc without logging or persisting it."""
    path = netrc_path or (Path.home() / ".netrc")
    try:
        auth = netrc_module.netrc(str(path)).authenticators("api.wandb.ai")
    except (FileNotFoundError, netrc_module.NetrcParseError, OSError):
        return ""
    if not auth:
        return ""
    _, _, password = auth
    return password or ""


def build_env_vars(replicas: int, forward_env: list[str]) -> list[dict[str, str]]:
    """Build the ACP env payload. Inject NCCL overrides for multi-replica per CLAUDE.md.

    Env vars not set in the local shell still appear in the payload with an empty
    value so the display can list them as "requested"; the submission code filters
    empties before shelling to `sco` so the container never sees blank overrides.
    WANDB_API_KEY additionally falls back to local ~/.netrc machine api.wandb.ai.
    """
    env: list[dict[str, str]] = []
    if replicas > 1:
        env += [
            {"key": "NCCL_NVLS_ENABLE", "value": "0"},
            {"key": "TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC", "value": "3600"},
            {"key": "NCCL_SOCKET_TIMEOUT", "value": "3600000"},
        ]
    for name in forward_env:
        value = os.environ.get(name, "")
        if not value and name == "WANDB_API_KEY":
            value = read_wandb_api_key_from_netrc()
        env.append({"key": name, "value": value})
    return env


def preflight_check_command(command: str) -> list[str]:
    """Scan referenced .sh scripts for `pip install`. Returns list of warnings."""
    warnings: list[str] = []
    match = re.search(r"(?:bash|sh)\s+(\S+\.sh)", command)
    if not match:
        return warnings
    script_path = Path(match.group(1))
    if not script_path.exists():
        return warnings
    try:
        body = script_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return warnings
    for lineno, line in enumerate(body.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\bpip\s+install\b", stripped):
            warnings.append(f"{script_path}:{lineno}: `pip install` in startup — install on htc first")
    return warnings


def validate_job_name_topology(name: str, gpus: int) -> str | None:
    """Return a warning if --name lacks a topology tag; None if OK (non-blocking)."""
    if TOPOLOGY_TAG_RE.search(name):
        return None
    return (
        f"job name {name!r} has no topology tag (expected like g{gpus}- or n?x{gpus}-); "
        "see CLAUDE.md 'Task names must encode real topology'"
    )


# =============================================================================
# `sco` subprocess wrappers (for non-HMAC calls: clusters usage, job describe/list)
# =============================================================================


SCO = os.path.expanduser("~/.sco/bin/sco")


def _sco_run(args: list[str]) -> str:
    r = subprocess.run([SCO, *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise APIError(f"sco {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _sco_ws_list_names() -> list[str]:
    """`sco ws instances list` — returns ACTIVE workspace names. Table output (no -o json)."""
    raw = _sco_run(["ws", "instances", "list"])
    return parse_sco_ws_list_table(raw)


def _sco_ws_describe(name: str) -> dict[str, Any]:
    """`sco ws instances describe --name X -o json`."""
    raw = _sco_run(["ws", "instances", "describe", f"--name={name}", "-o", "json"])
    return json.loads(raw)


def fetch_cluster_usage(clusters: Iterable[str]) -> dict[str, dict[str, int]]:
    """Parse `sco aec2 clusters usage --name=X` for idle GPU/CPU/memory."""
    usage: dict[str, dict[str, int]] = {}
    for cluster in clusters:
        try:
            raw = _sco_run(["aec2", "clusters", "usage", f"--name={cluster}"])
        except APIError as exc:
            print(f"[acp] warning: cluster usage failed for {cluster}: {exc}", file=sys.stderr)
            usage[cluster] = {"idle_gpu": 0, "idle_cpu": 0, "idle_mem_gb": 0}
            continue
        usage[cluster] = _parse_sco_usage_table(raw)
    return usage


def _parse_sco_usage_table(raw: str) -> dict[str, int]:
    """Parse the `sco aec2 clusters usage` table for reserved idle GPU/CPU/mem."""
    idle = {"idle_gpu": 0, "idle_cpu": 0, "idle_mem_gb": 0}
    for line in raw.splitlines():
        parts = re.split(r"\s{2,}|\|", line)
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            continue
        row_name = parts[0].upper()
        if row_name.startswith("GPU_NUMBER"):
            nums = [int(p) for p in parts[1:] if p.isdigit()]
            if len(nums) >= 3:
                idle["idle_gpu"] = nums[2]
        elif row_name.startswith("VCPU_NUMBER"):
            nums = [int(p) for p in parts[1:] if p.isdigit()]
            if len(nums) >= 3:
                idle["idle_cpu"] = nums[2]
        elif row_name.startswith("MEMORY"):
            nums = [int(p) for p in parts[1:] if p.isdigit()]
            if len(nums) >= 3:
                idle["idle_mem_gb"] = nums[2]
    return idle


def list_jobs_in_workspace(
    workspace: str,
    *,
    user_name: str | None = None,
    state: str | None = None,
    page_size: int = 500,
) -> list[dict[str, Any]]:
    """List jobs in a workspace.

    Tries HMAC `trainingJobs` first (supports server-side `user_name` filter so we
    avoid paging across hundreds of other-user jobs in shared workspaces). Falls
    back to `sco acp jobs list -o json` if HMAC is unavailable — that path has no
    user filter, so the caller must filter client-side.
    """
    try:
        client = HMACClient()
        path = (
            f"/compute/acp/data/v2/subscriptions/{DEFAULT_SUBSCRIPTION}"
            f"/resourceGroups/{DEFAULT_RG}/zones/{DEFAULT_WORKSPACE_ZONE}"
            f"/workspaces/{workspace}/trainingJobs?page_size={page_size}"
        )
        if user_name:
            path += f"&user_name={user_name}"
        if state:
            path += f"&state={state}"
        jobs_raw: list[dict[str, Any]] = []
        page_token = ""
        while True:
            page_path = path
            if page_token:
                page_path += f"&page_token={page_token}"
            payload = client.get(page_path)
            page_jobs = payload.get("training_jobs") or []
            jobs_raw.extend(page_jobs)
            page_token = payload.get("next_page_token") or ""
            if not page_token or not page_jobs:
                break
        return [_normalize_job(j, workspace) for j in jobs_raw]
    except (APIError, json.JSONDecodeError) as exc:
        print(f"[acp] warning: HMAC jobs list failed for workspace={workspace}: "
              f"{_short_api_error(str(exc))}; falling back to sco CLI",
              file=sys.stderr)

    try:
        raw = _sco_run([
            "acp", "jobs", "list",
            f"--workspace-name={workspace}",
            f"--page-size={page_size}",
            "-o", "json",
        ])
    except APIError as exc:
        print(f"[acp] warning: jobs list failed for workspace={workspace}: {exc}", file=sys.stderr)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    # `sco acp jobs list -o json` currently returns a bare array; older versions
    # wrapped it under `training_jobs`/`jobs`. Handle both.
    if isinstance(data, list):
        jobs_raw = data
    elif isinstance(data, dict):
        jobs_raw = data.get("training_jobs") or data.get("jobs") or []
    else:
        jobs_raw = []
    return [_normalize_job(j, workspace) for j in jobs_raw]


def list_cci_apps_in_workspace(
    workspace: str,
    *,
    page_size: int = 200,
) -> list[dict[str, Any]]:
    """List CCI apps in a workspace, normalized for quota accounting."""
    client = HMACClient(service_base=CCI_BASE)
    apps: list[dict[str, Any]] = []
    page_token = ""
    while True:
        path = (
            f"/compute/cci/data/v2/subscriptions/{DEFAULT_SUBSCRIPTION}"
            f"/resourceGroups/{DEFAULT_RG}/zones/{DEFAULT_WORKSPACE_ZONE}"
            f"/workspaces/{workspace}/apps?page_size={page_size}"
        )
        if page_token:
            path += f"&page_token={page_token}"
        try:
            payload = client.get(path)
        except (APIError, json.JSONDecodeError) as exc:
            print(
                f"[acp] warning: CCI apps list failed for workspace={workspace}: "
                f"{_short_api_error(str(exc))}",
                file=sys.stderr,
            )
            return apps
        for app in payload.get("apps") or []:
            apps.append(_normalize_cci_app(app, workspace))
        page_token = payload.get("next_page_token") or ""
        if not page_token:
            break
    return apps


def _normalize_job(raw: dict[str, Any], workspace: str) -> dict[str, Any]:
    """Reduce a full ACP job record to the compact fields used by `list` and `stop`."""
    roles = raw.get("roles") or []
    spec_name = ""
    gpus = 0
    replicas = 1
    if roles:
        r0 = roles[0]
        resource_spec = r0.get("resource_spec") or []
        if resource_spec:
            s0 = resource_spec[0]
            spec_name = s0.get("name", "")
            replicas = s0.get("replicas") or r0.get("total_replicas") or 1
            gpus_per = gpu_count_from_spec(s0)
            gpus = gpus_per * replicas

    create_time = raw.get("create_time") or raw.get("start_time")
    age_sec = 0
    if create_time:
        parsed = log_extract.parse_utc_time(create_time)
        if parsed:
            age_sec = int(time.time() - parsed.timestamp())

    ownership = raw.get("ownership") or {}
    return {
        "id": raw.get("name", ""),
        "name": raw.get("name", ""),
        "display_name": raw.get("display_name", ""),
        "state": raw.get("state", "UNKNOWN"),
        "age_sec": age_sec,
        "gpus": gpus,
        "replicas": replicas,
        "cluster": (raw.get("resource_pool") or {}).get("name", ""),
        "workspace": workspace,
        "spec": spec_name,
        "quota_type": resource_quota_type(raw),
        "owner_user_name": ownership.get("user_name", ""),
        "owner_user_id": ownership.get("user_id", ""),
        "command": ((roles[0].get("startup_script", "") if roles else "") or "").split("\n")[0][:200],
    }


def _normalize_cci_app(raw: dict[str, Any], workspace: str) -> dict[str, Any]:
    """Reduce a full CCI app record to the compact fields used by `quota`."""
    replicas = int(raw.get("replicas") or raw.get("total_replicas") or 1)
    gpus_per_replica = _cci_gpus_per_replica(raw)
    ownership = raw.get("ownership") or {}
    resource_pool = raw.get("resource_pool") or {}
    spec = ((raw.get("template") or {}).get("resource_spec") or {})
    return {
        "id": raw.get("name", ""),
        "name": raw.get("name", ""),
        "display_name": raw.get("display_name", ""),
        "state": raw.get("state", "UNKNOWN"),
        "gpus": gpus_per_replica * replicas,
        "replicas": replicas,
        "cluster": resource_pool.get("name", ""),
        "workspace": workspace,
        "spec": spec.get("name", ""),
        "quota_type": resource_quota_type(raw),
        "owner_user_name": ownership.get("user_name", ""),
        "owner_user_id": ownership.get("user_id", ""),
    }


def _cci_gpus_per_replica(raw: dict[str, Any]) -> int:
    template = raw.get("template") or {}
    for container in template.get("containers") or []:
        request = container.get("resource_request") or {}
        value = request.get("nvidia.com/gpu") or request.get("nvidia.com/mig-3g.40gb")
        if value:
            return _resource_count_as_int(value)
    spec = template.get("resource_spec") or {}
    return gpu_count_from_spec(spec)


def _resource_count_as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


# =============================================================================
# Subcommand: list
# =============================================================================


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config(CONFIG_PATH)
    client = HMACClient()
    if args.workspace:
        workspaces = [{"name": args.workspace, "resource_id": None, "clusters": []}]
    else:
        workspaces = discover_workspaces(client)

    identity = cfg.get("identity") or {}
    if args.all_users:
        user = None
    elif args.user:
        user = args.user
    else:
        user = identity.get("user_name") or identity.get("user_id") or None
        if not user:
            print(
                "[acp list] hint: no user filter — showing ALL jobs in linked workspaces.\n"
                "  fix: set [identity] user_name in ~/.config/dreamdojo/acp.toml, "
                "pass --user <name|id>, or run `acp whoami --set <user_name>`.",
                file=sys.stderr,
            )

    # Server-side filter: if `user` looks like a user_name (not a UUID), pass to
    # HMAC to avoid paging thousands of other-user jobs. UUIDs are ignored
    # server-side (`user_id=` doesn't filter), so for those we list unfiltered
    # and rely on the client-side filter below.
    server_user = user if user and not re.fullmatch(r"[0-9a-f-]{20,}", user) else None

    all_jobs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for jobs in pool.map(
            lambda ws: list_jobs_in_workspace(ws["name"], user_name=server_user),
            workspaces,
        ):
            all_jobs.extend(jobs)

    states = parse_state_list(args.state) if args.state else (None if args.all else ["RUNNING"])
    since_sec = parse_since(args.since) if args.since else None
    filtered = filter_jobs(
        all_jobs,
        states=states,
        experiment=args.experiment,
        since_sec=since_sec,
        job_id=args.id,
        workspace=args.workspace,
        user=user,
    )

    if args.json:
        print(json.dumps(filtered, indent=2, ensure_ascii=False))
    elif args.id:
        if not filtered:
            print(f"[acp list] error: job {args.id} not found in any linked workspace",
                  file=sys.stderr)
            return 1
        print(format_list_verbose(filtered[0]))
    else:
        print(format_list_compact(filtered))
    return 0


# =============================================================================
# Subcommand: stop — dependency-injected for testability
# =============================================================================


def _describe_job(workspace: dict[str, Any], job_id: str, client: HMACClient | None) -> dict[str, Any]:
    """Find a job by ID in one workspace. Raises JobNotFound if absent."""
    try:
        raw = _sco_run([
            "acp", "jobs", "describe",
            f"--workspace-name={workspace['name']}",
            job_id, "-o", "json",
        ])
    except APIError as exc:
        if "not found" in str(exc).lower() or "404" in str(exc):
            raise JobNotFound(job_id) from exc
        raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise JobNotFound(job_id)
    data["workspace"] = workspace["name"]
    return data


def _stop_job(workspace: dict[str, Any], job_id: str, client: HMACClient | None) -> None:
    _sco_run([
        "acp", "jobs", "stop",
        f"--workspace-name={workspace['name']}",
        job_id,
    ])


def _poll_job_state(
    workspace: dict[str, Any],
    job_id: str,
    client: HMACClient | None,
    *,
    timeout: int = 30,
    interval: int = 2,
) -> str:
    """Poll `sco acp jobs describe` until state transitions away from RUNNING/PENDING."""
    deadline = time.time() + timeout
    last = "UNKNOWN"
    while time.time() < deadline:
        try:
            info = _describe_job(workspace, job_id, client)
        except JobNotFound:
            return "DELETED"
        last = info.get("state", "UNKNOWN")
        if last not in ACTIVE_STATES:
            return last
        time.sleep(interval)
    return last


def cmd_stop_main(
    argv: list[str],
    *,
    client_factory: Callable[[], Any],
    workspaces_provider: Callable[[], list[dict[str, Any]]],
    describe_fn: Callable[..., dict[str, Any]],
    stop_fn: Callable[..., None],
    poll_fn: Callable[..., str],
) -> int:
    """Inner stop handler — dependency-injected so tests don't shell out."""
    parser = argparse.ArgumentParser(prog="acp stop", add_help=False)
    parser.add_argument("job_id")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    if not is_valid_job_id(args.job_id):
        print(
            f"[acp stop] error: stop only accepts a job ID like pt-abc12345; "
            f"got {args.job_id!r}\n  next: acp list",
            file=sys.stderr,
        )
        return 2

    client = client_factory()

    found_ws: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    for ws in workspaces_provider():
        try:
            info = describe_fn(ws, args.job_id, client)
            found_ws = ws
            break
        except JobNotFound:
            continue

    if not found_ws or not info:
        print(
            f"[acp stop] error: job {args.job_id} not found in any linked workspace\n"
            f"  next: acp list --all --id {args.job_id}",
            file=sys.stderr,
        )
        return 1

    state = info.get("state", "UNKNOWN")
    print(f"[acp stop] {args.job_id}")
    print(f"  found     workspace={found_ws['name']} state={state}")

    if state not in ACTIVE_STATES:
        print("  no-op     already not in an active state")
        return 0

    print("  stopping  ...")
    stop_fn(found_ws, args.job_id, client)

    if args.no_wait:
        print("  submitted stop request (--no-wait)")
        return 0

    final = poll_fn(found_ws, args.job_id, client, timeout=args.timeout)
    if final in ACTIVE_STATES:
        print(
            f"  warning   state still {final} after {args.timeout}s; ACP scheduler may be slow\n"
            f"            recheck: acp list --id {args.job_id}",
            file=sys.stderr,
        )
        return 1
    print(f"  stopped   state={final}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    return cmd_stop_main(
        [args.job_id] + (["--no-wait"] if args.no_wait else []) + ["--timeout", str(args.timeout)],
        client_factory=HMACClient,
        workspaces_provider=lambda: discover_workspaces(HMACClient()),
        describe_fn=_describe_job,
        stop_fn=_stop_job,
        poll_fn=_poll_job_state,
    )


# =============================================================================
# Subcommand: switch — replacement with the exact old resource shape
# =============================================================================


def extract_switch_source(job: dict[str, Any]) -> dict[str, Any]:
    """Extract the resource shape that `acp switch` must preserve exactly."""
    roles = job.get("roles") or []
    if not roles:
        raise ValueError("source job has no roles")
    worker = roles[0]
    specs = worker.get("resource_spec") or []
    if not specs:
        raise ValueError("source job has no worker resource_spec")
    spec = specs[0]

    spec_name = spec.get("name") or ""
    if not spec_name:
        raise ValueError("source job worker resource_spec has no name")
    replicas = int(spec.get("replicas") or worker.get("total_replicas") or 1)
    gpus_per_replica = gpu_count_from_spec(spec)
    total_gpus = gpus_per_replica * replicas
    if total_gpus <= 0:
        raise ValueError("source job GPU count could not be inferred from resource_spec")

    cluster = (job.get("resource_pool") or {}).get("name") or ""
    if not cluster:
        raise ValueError("source job has no resource_pool.name")
    workspace = job.get("workspace") or ""
    if not workspace:
        raise ValueError("source job workspace is unknown")

    return {
        "workspace": workspace,
        "cluster": cluster,
        "spec_name": spec_name,
        "replicas": replicas,
        "gpus_per_replica": gpus_per_replica,
        "gpus": total_gpus,
    }


def build_switch_copy_command(
    *,
    source: dict[str, Any],
    old_job_id: str,
    new_name: str,
    command: str,
    image: str | None,
    env_list: list[dict[str, str]] | None,
) -> list[str]:
    cmd = [
        SCO, "acp", "jobs", "copy",
        f"--workspace-name={source['workspace']}",
        f"--copy-job-name={old_job_id}",
        f"--job-name={new_name}",
        f"--aec2-name={source['cluster']}",
        "--training-framework=pt",
        f"--worker-spec={source['spec_name']}",
        f"--worker-nodes={source['replicas']}",
        f"--command={command}",
    ]
    if image:
        cmd.append(f"--container-image-url={image}")
    if env_list is not None:
        env_payload = ",".join(f"{e['key']}:{e['value']}" for e in env_list if e["value"])
        if env_payload:
            cmd.append(f"--env={env_payload}")
    return cmd


def cmd_switch_main(
    argv: list[str],
    *,
    client_factory: Callable[[], Any],
    workspaces_provider: Callable[[], list[dict[str, Any]]],
    describe_fn: Callable[..., dict[str, Any]],
    stop_fn: Callable[..., None],
    poll_fn: Callable[..., str],
    submit_fn: Callable[..., tuple[int, str, str]],
    config_loader: Callable[[], dict[str, Any]],
    bootstrap_fn: Callable[[], None],
) -> int:
    parser = argparse.ArgumentParser(prog="acp switch", add_help=False)
    parser.add_argument("old_job_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--image", help="override copied image; omit to preserve source job image")
    parser.add_argument("--env", help="comma-separated env-var names; omit to preserve copied env")
    parser.add_argument("--console-path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="bypass preflight warnings")
    parser.add_argument("--wait-timeout", default="6h")
    parser.add_argument("--wait-interval", default="10s")
    parser.add_argument("--stop-timeout", type=int, default=30)
    parser.add_argument("--no-stop-wait", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    if not is_valid_job_id(args.old_job_id):
        print(
            f"[acp switch] error: source must be a job ID like pt-abc12345; got {args.old_job_id!r}",
            file=sys.stderr,
        )
        return 2

    warnings = preflight_check_command(args.command)
    for w in warnings:
        print(f"[acp switch] preflight warning: {w}", file=sys.stderr)
    if warnings and not args.force:
        print("[acp switch] error: preflight failed; pass --force to override", file=sys.stderr)
        return 2

    try:
        wait_timeout_sec = parse_duration(args.wait_timeout)
        wait_interval_sec = parse_duration(args.wait_interval)
    except ValueError as exc:
        print(f"[acp switch] error: {exc}", file=sys.stderr)
        return 2

    cfg = config_loader()
    bootstrap_fn()
    client = client_factory()
    workspaces = workspaces_provider()
    if args.workspace:
        workspaces = [w for w in workspaces if w["name"] == args.workspace]
        if not workspaces:
            print(f"[acp switch] error: workspace {args.workspace} not linked", file=sys.stderr)
            return 1

    found_ws: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    for ws in workspaces:
        try:
            info = describe_fn(ws, args.old_job_id, client)
            found_ws = ws
            break
        except JobNotFound:
            continue

    if not found_ws or not info:
        print(
            f"[acp switch] error: source job {args.old_job_id} not found in any linked workspace",
            file=sys.stderr,
        )
        return 1
    if not info.get("workspace"):
        info = {**info, "workspace": found_ws["name"]}

    state = str(info.get("state", "UNKNOWN")).upper()
    if state not in ACTIVE_STATES:
        print(
            f"[acp switch] error: source job {args.old_job_id} is not active (state={state}); "
            "refusing to switch",
            file=sys.stderr,
        )
        return 2

    try:
        source = extract_switch_source(info)
    except ValueError as exc:
        print(f"[acp switch] error: {exc}", file=sys.stderr)
        return 2

    name_warn = validate_job_name_topology(args.name, source["gpus"])
    if name_warn:
        print(f"[acp switch] warning: {name_warn}", file=sys.stderr)

    image = None
    if args.image:
        image = cfg["defaults"]["image"] if args.image == "current" else args.image
    env_list = None
    if args.env is not None:
        forward_env = [e.strip() for e in args.env.split(",") if e.strip()]
        env_list = build_env_vars(source["replicas"], forward_env)

    cmd = build_switch_copy_command(
        source=source,
        old_job_id=args.old_job_id,
        new_name=args.name,
        command=args.command,
        image=image,
        env_list=env_list,
    )

    print(f"[acp switch] {args.old_job_id} -> {args.name}")
    print(
        f"  source    workspace={source['workspace']} cluster={source['cluster']} "
        f"spec={source['spec_name']} topology={source['replicas']}x{source['gpus_per_replica']} "
        f"GPU total={source['gpus']}"
    )
    print("  wait      quota-mode=wait native sco --wait (server-side; no client polling)")
    if args.dry_run:
        print("  dry-run   no submission or stop performed")
        return 0

    rc, stdout, stderr = submit_fn(
        cmd,
        quota_mode="wait",
        wait_timeout_sec=wait_timeout_sec,
        wait_interval_sec=wait_interval_sec,
    )
    if rc != 0:
        print(format_sco_failure(cmd, rc, stdout, stderr), file=sys.stderr)
        print("  kept      source job was not stopped because replacement submit failed")
        return 1

    new_job = _extract_job_name(stdout) or "(see sco output)"
    print(f"  submitted {new_job} (replacement queued)")
    if args.console_path:
        print(f"  console   {args.console_path}")

    print(f"  stopping  source {args.old_job_id} ...")
    stop_fn(found_ws, args.old_job_id, client)
    if args.no_stop_wait:
        print("  stopped   submitted stop request (--no-stop-wait)")
        return 0

    final = poll_fn(found_ws, args.old_job_id, client, timeout=args.stop_timeout)
    if final in ACTIVE_STATES:
        print(
            f"  warning   source still {final} after {args.stop_timeout}s; "
            f"recheck: acp list --id {args.old_job_id}",
            file=sys.stderr,
        )
        return 1
    print(f"  stopped   source state={final}")
    print(f"  next      acp list --id {new_job}")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    argv = [args.old_job_id, "--name", args.name, "--command", args.command]
    if args.workspace:
        argv += ["--workspace", args.workspace]
    if args.image:
        argv += ["--image", args.image]
    if args.env is not None:
        argv += ["--env", args.env]
    if args.console_path:
        argv += ["--console-path", args.console_path]
    if args.dry_run:
        argv.append("--dry-run")
    if args.force:
        argv.append("--force")
    argv += ["--wait-timeout", args.wait_timeout]
    argv += ["--wait-interval", args.wait_interval]
    argv += ["--stop-timeout", str(args.stop_timeout)]
    if args.no_stop_wait:
        argv.append("--no-stop-wait")
    return cmd_switch_main(
        argv,
        client_factory=HMACClient,
        workspaces_provider=lambda: discover_workspaces(HMACClient()),
        describe_fn=_describe_job,
        stop_fn=_stop_job,
        poll_fn=_poll_job_state,
        submit_fn=run_sco_create_with_quota_mode,
        config_loader=lambda: load_config(CONFIG_PATH),
        bootstrap_fn=lambda: bootstrap_config(CONFIG_PATH),
    )


# =============================================================================
# Subcommand: submit
# =============================================================================


def cmd_submit(args: argparse.Namespace) -> int:
    cfg = load_config(CONFIG_PATH)
    bootstrap_config(CONFIG_PATH)

    # Preflight: pip-install scan on referenced .sh
    warnings = preflight_check_command(args.command)
    for w in warnings:
        print(f"[acp submit] preflight warning: {w}", file=sys.stderr)
    if warnings and not args.force:
        print("[acp submit] error: preflight failed; pass --force to override", file=sys.stderr)
        return 2

    name_warn = validate_job_name_topology(args.name, args.gpus)
    if name_warn:
        print(f"[acp submit] warning: {name_warn}", file=sys.stderr)

    client = HMACClient()
    workspaces = discover_workspaces(client)
    requested_workspace = (args.workspace or cfg["defaults"].get("workspace") or "").strip()
    if requested_workspace:
        workspaces = filter_requested_workspace(
            workspaces,
            requested_workspace,
            refresh_fn=lambda: discover_workspaces(client, force_refresh=True),
        )
        if not workspaces:
            print(f"[acp submit] error: workspace {requested_workspace} not linked", file=sys.stderr)
            return 1

    all_clusters = sorted({c for ws in workspaces for c in ws["clusters"]})
    if not all_clusters:
        all_clusters = ["computing-cluster-01e"]
    catalog = discover_spec_catalog(client, all_clusters)
    usage = fetch_cluster_usage(all_clusters)

    try:
        plan = plan_submission(
            requested_gpus=args.gpus,
            cpus_per_gpu=args.cpus_per_gpu or cfg["defaults"]["cpus_per_gpu"],
            mem_per_gpu_gb=args.mem_per_gpu_gb or cfg["defaults"]["mem_per_gpu_gb"],
            spec_catalog=catalog,
            cluster_usage=usage,
        )
    except NoSpecFit as exc:
        print(f"[acp submit] error: {exc}", file=sys.stderr)
        return 1

    forward_env = args.env.split(",") if args.env else cfg["defaults"]["forward_env"]
    env_list = build_env_vars(plan["replicas"], forward_env)
    image = args.image if args.image and args.image != "current" else cfg["defaults"]["image"]

    workspace = next((w for w in workspaces if plan["cluster"] in w["clusters"]), workspaces[0])

    quota_mode = args.quota_mode
    try:
        wait_timeout_sec = parse_duration(args.wait_timeout)
        wait_interval_sec = parse_duration(args.wait_interval)
    except ValueError as exc:
        print(f"[acp submit] error: {exc}", file=sys.stderr)
        return 2

    print(f"[acp submit] {args.name}")
    print(f"  picked    cluster={plan['cluster']} spec={plan['spec_name']} "
          f"({plan['gpus_per_replica']} GPU/{cfg['defaults']['cpus_per_gpu']} CPU/"
          f"{cfg['defaults']['mem_per_gpu_gb']} GB per replica)")
    topology_note = (
        "single-node, no NCCL injection needed"
        if plan["replicas"] == 1 else
        f"multi-node, NCCL_NVLS_ENABLE=0 + timeout overrides injected"
    )
    print(f"  topology  {plan['replicas']} replica × {plan['gpus_per_replica']} GPU ({topology_note})")

    # Workspace-quota snapshot (RUNNING non-spot ACP jobs + CCI apps).
    quota_caps = cfg.get("workspace_quota") or {}
    cap = int(quota_caps.get(workspace["name"], 0) or 0)
    quota_line = f"mode={quota_mode}"
    if quota_mode == "wait":
        quota_line += " (native sco --wait; no client polling)"
    if cap > 0:
        try:
            snap = compute_workspace_quota_usage(workspace["name"], quota_cap=cap)
            avail = snap["available"]
            quota_line += (
                f"  workspace={workspace['name']}  used {snap['used']}/{cap} GPU"
                f"  available {avail} GPU  need {args.gpus}"
            )
            if avail is not None and avail < args.gpus:
                quota_line += "  (request exceeds workspace cap)"
        except Exception as exc:  # quota check is advisory — never block submit
            quota_line += f"  workspace={workspace['name']}  (quota check failed: {exc})"
    else:
        quota_line += f"  workspace={workspace['name']}  (no cap configured)"
    print(f"  quota     {quota_line}")

    named = [e["key"] for e in env_list]
    unset = [e["key"] for e in env_list if not e["value"]]
    env_line = " ".join(named) if named else "(none)"
    if unset:
        env_line += f"   [not set locally: {', '.join(unset)}]"
    print(f"  env       {env_line}")

    if args.dry_run:
        print("  dry-run   no submission performed")
        return 0

    cmd = [
        SCO, "acp", "jobs", "create",
        f"--workspace-name={workspace['name']}",
        f"--aec2-name={plan['cluster']}",
        f"--job-name={args.name}",
        "--training-framework=pt",
        f"--worker-spec={plan['spec_name']}",
        f"--worker-nodes={plan['replicas']}",
        f"--container-image-url={image}",
        f"--storage-mount={cfg['afs_mount']['id']}:{cfg['afs_mount']['mount_path']}",
        f"--command={args.command}",
    ]
    if env_list:
        cmd.append(
            "--env=" + ",".join(f"{e['key']}:{e['value']}" for e in env_list if e["value"])
        )
    if quota_mode == "spot":
        cmd.append("--quota-type=spot")

    rc, stdout, stderr = run_sco_create_with_quota_mode(
        cmd,
        quota_mode=quota_mode,
        wait_timeout_sec=wait_timeout_sec,
        wait_interval_sec=wait_interval_sec,
    )
    if rc != 0:
        combined = combined_sco_output(stdout, stderr)
        if is_quota_error(combined) and quota_mode == "fail":
            print(format_quota_error_hint(combined), file=sys.stderr)
        else:
            print(format_sco_failure(cmd, rc, stdout, stderr), file=sys.stderr)
        return 1

    # Try to parse the returned job name
    job_name = _extract_job_name(stdout) or "(see sco output)"
    if quota_mode == "wait":
        tag = "state=WAIT_QUOTA/PENDING, server-wait, quota-mode=wait"
    else:
        tag = f"state=PENDING, queued, quota-mode={quota_mode}"
    print(f"  submitted {job_name} ({tag})")
    if args.console_path:
        print(f"  console   {args.console_path}")
    print(f"  next      acp list --id {job_name}")
    return 0


def _extract_job_name(stdout: str) -> str | None:
    m = re.search(r"\b(pt-[a-z0-9]{8,})\b", stdout)
    return m.group(1) if m else None


def _sco_create_job(cmd: list[str]) -> tuple[int, str, str]:
    """Run `sco acp jobs create` — returns (rc, stdout, stderr). Separated from
    `cmd_submit` so the quota-mode retry loop can mock it in tests."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def run_sco_create_with_quota_mode(
    cmd: list[str],
    *,
    quota_mode: str,
    wait_timeout_sec: int,
    wait_interval_sec: int,
    run_fn: Callable[[list[str]], tuple[int, str, str]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """Dispatch `sco acp jobs create` under the chosen quota handling strategy.

    - `quota_mode='fail'` / `'spot'`: run once, return. (Spot is just about the
      flag appended to `cmd` by the caller — no extra retry behavior.)
    - `quota_mode='wait'`: append native `sco --wait` and run once. The server
      may place the job in WAIT_QUOTA; this helper no longer client-polls.

    Legacy timeout/interval dependencies are accepted for wrapper compatibility
    but are intentionally unused by native wait mode.
    """
    runner = run_fn or _sco_create_job
    if quota_mode == "wait" and "--wait" not in cmd:
        cmd.append("--wait")
    return runner(cmd)


# =============================================================================
# Subcommand: whoami — read/set identity used by `list` default filter
# =============================================================================


def cmd_whoami(args: argparse.Namespace) -> int:
    cfg = load_config(CONFIG_PATH)
    identity = cfg.get("identity") or {}
    if args.set or args.user_id:
        new_name = args.set or identity.get("user_name", "")
        new_id = args.user_id or identity.get("user_id", "")
        save_identity(CONFIG_PATH, new_name, new_id)
        print(f"[acp whoami] saved to {CONFIG_PATH}")
        print(f"  user_name {new_name or '(empty)'}")
        print(f"  user_id   {new_id or '(empty)'}")
        return 0
    print(f"[acp whoami] config {CONFIG_PATH}")
    print(f"  user_name {identity.get('user_name') or '(empty)'}")
    print(f"  user_id   {identity.get('user_id') or '(empty)'}")
    if not (identity.get("user_name") or identity.get("user_id")):
        print("  next      acp whoami --set <user_name> [--user-id <uuid>]")
    return 0


# =============================================================================
# Subcommand: quota — client-side workspace-quota snapshot
# =============================================================================


def cmd_quota(args: argparse.Namespace) -> int:
    cfg = load_config(CONFIG_PATH)
    quota_caps: dict[str, int] = {
        k: int(v) for k, v in (cfg.get("workspace_quota") or {}).items() if int(v or 0) > 0
    }

    if args.workspace:
        cap = args.cap if args.cap else quota_caps.get(args.workspace, 0)
        targets = [(args.workspace, cap)]
    else:
        if not quota_caps:
            print(
                "[acp quota] no workspace caps configured; add them to "
                f"{CONFIG_PATH} under [workspace_quota] or pass --workspace/--cap",
                file=sys.stderr,
            )
            return 1
        targets = list(quota_caps.items())

    rows: list[dict[str, Any]] = []
    for ws, cap in targets:
        try:
            snap = compute_workspace_quota_usage(ws, quota_cap=cap)
        except Exception as exc:
            print(f"[acp quota] warning: {ws}: {exc}", file=sys.stderr)
            continue
        rows.append(snap)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print("[acp quota] workspace quota snapshot (RUNNING non-spot ACP jobs + CCI apps; suspended/pending/spot do not count)")
    for snap in rows:
        ws = snap["workspace"]
        cap = snap["cap"]
        used = snap["used"]
        job_used = snap.get("job_used", used)
        cci_used = snap.get("cci_used", 0)
        spot_excluded = snap.get("spot_excluded", 0)
        spot_jobs = snap.get("running_spot_jobs", 0)
        spot_apps = snap.get("running_spot_cci_apps", 0)
        avail = snap["available"]
        rj = snap["running_jobs"]
        ra = snap.get("running_cci_apps", 0)
        spot_note = ""
        if spot_excluded:
            spot_note = (
                f"; excluded spot {spot_excluded} GPU "
                f"across {spot_jobs} jobs + {spot_apps} apps"
            )
        if cap <= 0 or avail is None:
            print(
                f"  {ws}  used {used} GPU "
                f"(ACP {job_used} across {rj} jobs + CCI {cci_used} across {ra} apps"
                f"{spot_note})  "
                "(no cap configured)"
            )
        else:
            tag = ""
            if avail == 0:
                tag = "  [QUOTA FULL]"
            elif avail <= cap * 0.1:
                tag = "  [near cap]"
            print(
                f"  {ws}  used {used}/{cap} GPU  available {avail} GPU  "
                f"(ACP {job_used} across {rj} jobs + CCI {cci_used} across {ra} apps"
                f"{spot_note}){tag}"
            )
    return 0


# =============================================================================
# Subcommand: logs (shim)
# =============================================================================


def cmd_logs_main(argv: list[str]) -> int:
    """Forward argv to log_extract.main()."""
    saved = sys.argv
    try:
        sys.argv = ["log_extract.py", *argv]
        log_extract.main()
    finally:
        sys.argv = saved
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    return cmd_logs_main(args.logs_args)


# =============================================================================
# Subcommand: refresh
# =============================================================================


def cmd_refresh(args: argparse.Namespace) -> int:
    client = HMACClient()
    if not args.workspaces_only:
        # Need workspaces first to know which clusters to query
        ws = discover_workspaces(client, force_refresh=True)
        clusters = sorted({c for w in ws for c in w["clusters"]}) or ["computing-cluster-01e"]
        discover_spec_catalog(client, clusters, force_refresh=True)
    else:
        discover_workspaces(client, force_refresh=True)
    print(f"[acp refresh] cache rebuilt at {CACHE_DIR}")
    return 0


# =============================================================================
# Main dispatcher
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="acp",
        description="Agent-friendly SenseCore ACP CLI (submit/list/stop/logs).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list ACP jobs across linked workspaces")
    p_list.add_argument("--all", action="store_true", help="include all states (default: RUNNING)")
    p_list.add_argument("--state", help="explicit state filter, e.g. running,pending")
    p_list.add_argument("--experiment", help="substring match on name + display_name")
    p_list.add_argument("--since", help="age limit: 6h, 2d, 1w")
    p_list.add_argument("--id", help="exact job-ID lookup (verbose)")
    p_list.add_argument("--workspace", help="restrict to one workspace")
    p_list.add_argument("--user", help="filter by owner user_name or user_id "
                                       "(default: [identity] user_name from config)")
    p_list.add_argument("--all-users", action="store_true",
                        help="include jobs from all users (disable identity filter)")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(handler=cmd_list)

    p_stop = sub.add_parser("stop", help="stop a single job by ID")
    p_stop.add_argument("job_id")
    p_stop.add_argument("--no-wait", action="store_true")
    p_stop.add_argument("--timeout", type=int, default=30)
    p_stop.set_defaults(handler=cmd_stop)

    p_switch = sub.add_parser("switch", help="copy a source job with identical resources, then stop source")
    p_switch.add_argument("old_job_id")
    p_switch.add_argument("--name", required=True, help="replacement job name")
    p_switch.add_argument("--command", required=True, help="replacement startup command")
    p_switch.add_argument("--workspace", help="restrict source lookup to one workspace")
    p_switch.add_argument("--image", help="override copied image; omit to preserve source job image")
    p_switch.add_argument("--env", help="comma-separated env-var names; omit to preserve copied env")
    p_switch.add_argument("--console-path")
    p_switch.add_argument("--dry-run", action="store_true")
    p_switch.add_argument("--force", action="store_true", help="bypass preflight warnings")
    p_switch.add_argument("--wait-timeout", default="6h",
                          help="deprecated; native sco --wait is used for replacement submit")
    p_switch.add_argument("--wait-interval", default="10s",
                          help="deprecated; native sco --wait is used for replacement submit")
    p_switch.add_argument("--stop-timeout", type=int, default=30,
                          help="seconds to wait for source stop after replacement submits")
    p_switch.add_argument("--no-stop-wait", action="store_true",
                          help="return immediately after sending the source stop request")
    p_switch.set_defaults(handler=cmd_switch)

    p_submit = sub.add_parser("submit", help="submit a new ACP job with auto spec/env")
    p_submit.add_argument("--name", required=True)
    p_submit.add_argument("--gpus", type=int, required=True)
    p_submit.add_argument("--command", required=True)
    p_submit.add_argument("--cpus-per-gpu", type=int, default=0)
    p_submit.add_argument("--mem-per-gpu-gb", type=int, default=0)
    p_submit.add_argument("--workspace")
    p_submit.add_argument("--image", default="current")
    p_submit.add_argument("--env", help="comma-separated env-var names to forward")
    p_submit.add_argument("--console-path")
    p_submit.add_argument("--probe", action="store_true", help="(opt-in) run probe before formal")
    p_submit.add_argument("--dry-run", action="store_true")
    p_submit.add_argument("--force", action="store_true", help="bypass preflight warnings")
    p_submit.add_argument(
        "--quota-mode", choices=["fail", "wait", "spot"], default="fail",
        help="how to handle quota-exceeded: "
             "fail=error out (default); "
             "wait=submit once with native sco --wait (server-side WAIT_QUOTA); "
             "spot=submit to the spot pool (small & unstable — jobs can be preempted)",
    )
    p_submit.add_argument(
        "--wait-timeout", default="2h",
        help="deprecated for submit --quota-mode=wait; accepted for wrapper compatibility",
    )
    p_submit.add_argument(
        "--wait-interval", default="60s",
        help="deprecated for submit --quota-mode=wait; accepted for wrapper compatibility",
    )
    p_submit.set_defaults(handler=cmd_submit)

    p_whoami = sub.add_parser("whoami", help="show/set the identity used by `list` default filter")
    p_whoami.add_argument("--set", help="save user_name to ~/.config/dreamdojo/acp.toml")
    p_whoami.add_argument("--user-id", help="save user_id alongside user_name")
    p_whoami.set_defaults(handler=cmd_whoami)

    p_logs = sub.add_parser("logs", help="extract offline logs for a job")
    p_logs.add_argument("logs_args", nargs=argparse.REMAINDER)
    p_logs.set_defaults(handler=cmd_logs)

    p_refresh = sub.add_parser("refresh", help="force cache refresh")
    p_refresh.add_argument("--specs", dest="specs_only", action="store_true")
    p_refresh.add_argument("--workspaces", dest="workspaces_only", action="store_true")
    p_refresh.set_defaults(handler=cmd_refresh)

    p_quota = sub.add_parser(
        "quota", help="show workspace GPU-quota usage (RUNNING non-spot ACP jobs + CCI apps, client-side)"
    )
    p_quota.add_argument("--workspace", help="workspace name (default: all configured)")
    p_quota.add_argument("--cap", type=int, default=0,
                         help="override configured cap for --workspace (GPU count)")
    p_quota.add_argument("--json", action="store_true", help="machine-readable output")
    p_quota.set_defaults(handler=cmd_quota)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
