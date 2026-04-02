"""HTTP API client for SenseCore services (ACP, CCI, AEC2, Monitor)."""

from __future__ import annotations

import shutil
import subprocess
import json
import re
from pathlib import Path
from typing import Any

from .auth import SenseCoreAuth
from . import config


def gpu_count_from_spec(spec: dict[str, Any]) -> int:
    """Extract GPU count from a resource spec dict.

    Ported from sensecore_acp_resource_aware_launch.py::gpu_count_from_spec.
    """
    device = spec.get("device")
    if isinstance(device, dict):
        value = device.get("number")
        if value is not None:
            return int(value)
    limits = spec.get("limits") or {}
    value = limits.get("nvidia.com/gpu", 0) or limits.get("device", 0) or 0
    return int(value)


def _extract_gpu_from_spec_name(spec_name: str) -> int:
    """Heuristic: extract GPU count from spec name like 'N6lS.Iu.I10.8.64c1024g'."""
    # The number after the device prefix and before optional cpu/mem suffix
    parts = spec_name.split(".")
    for part in reversed(parts):
        # Look for a bare number (not containing 'c' or 'g' which are cpu/mem)
        if part.isdigit():
            return int(part)
    return 1


class SenseCoreAPIClient:
    """Thin client wrapping HMAC-signed requests to SenseCore APIs."""

    def __init__(self, auth: SenseCoreAuth):
        self.auth = auth

    # ── Path helpers ──────────────────────────────────────────────────────

    def _acp_base_path(self, workspace: str) -> str:
        return (
            f"/compute/acp/data/v2/subscriptions/{config.SUBSCRIPTION}"
            f"/resourceGroups/{config.RESOURCE_GROUP}"
            f"/zones/{config.WORKSPACE_ZONE}"
            f"/workspaces/{workspace}"
        )

    def _cci_base_path(self, workspace: str) -> str:
        return (
            f"/compute/cci/data/v2/subscriptions/{config.SUBSCRIPTION}"
            f"/resourceGroups/{config.RESOURCE_GROUP}"
            f"/zones/{config.WORKSPACE_ZONE}"
            f"/workspaces/{workspace}"
        )

    def _aec2_base_path(self) -> str:
        return (
            f"/compute/workspace/data/v1/subscriptions/{config.SUBSCRIPTION}"
            f"/resourceGroups/{config.RESOURCE_GROUP}"
            f"/zones/{config.WORKSPACE_ZONE}"
        )

    # ── ACP (Training Jobs) ──────────────────────────────────────────────

    def list_training_jobs(
        self,
        workspace: str,
        page_size: int = 200,
        user_name: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """List training jobs with pagination and optional filters."""
        all_jobs: list[dict[str, Any]] = []
        page_token = ""
        while True:
            path = f"{self._acp_base_path(workspace)}/trainingJobs?page_size={page_size}"
            if user_name:
                path += f"&user_name={user_name}"
            if state:
                path += f"&state={state}"
            if page_token:
                path += f"&page_token={page_token}"
            raw = self.auth.request_json("GET", config.AEC2_BASE, path)
            jobs = raw.get("training_jobs") or []
            all_jobs.extend(jobs)
            page_token = raw.get("next_page_token", "")
            if not page_token or not jobs:
                break
        return {"training_jobs": all_jobs}

    def get_training_job(self, workspace: str, job_name: str) -> dict[str, Any]:
        path = f"{self._acp_base_path(workspace)}/trainingJobs/{job_name}"
        return self.auth.request_json("GET", config.AEC2_BASE, path)

    # ── CCI (Container Instances) ────────────────────────────────────────

    def list_cci_apps(self, workspace: str) -> dict[str, Any]:
        """List CCI apps with pagination (workspace may have hundreds)."""
        all_apps: list[dict[str, Any]] = []
        page_token = ""
        while True:
            path = f"{self._cci_base_path(workspace)}/apps?page_size=200"
            if page_token:
                path += f"&page_token={page_token}"
            raw = self.auth.request_json("GET", config.CCI_BASE, path)
            apps = raw.get("apps") or []
            all_apps.extend(apps)
            page_token = raw.get("next_page_token", "")
            if not page_token or not apps:
                break
        return {"apps": all_apps}

    def get_cci_app(self, workspace: str, app_name: str) -> dict[str, Any]:
        path = f"{self._cci_base_path(workspace)}/apps/{app_name}"
        return self.auth.request_json("GET", config.CCI_BASE, path)

    def start_cci_app(self, workspace: str, app_name: str) -> dict[str, Any]:
        path = f"{self._cci_base_path(workspace)}/apps/{app_name}:start"
        return self.auth.request_json("POST", config.CCI_BASE, path)

    def stop_cci_app(self, workspace: str, app_name: str) -> dict[str, Any]:
        path = f"{self._cci_base_path(workspace)}/apps/{app_name}:stop"
        return self.auth.request_json("POST", config.CCI_BASE, path)

    # ── ACP Workers & Events ───────────────────────────────────────────────

    def list_workers(self, workspace: str, job_name: str) -> dict[str, Any]:
        path = f"{self._acp_base_path(workspace)}/trainingJobs/{job_name}/workers"
        return self.auth.request_json("GET", config.AEC2_BASE, path)

    def get_job_events(self, workspace: str, job_name: str) -> dict[str, Any]:
        path = f"{self._acp_base_path(workspace)}/trainingJobs/{job_name}/events"
        return self.auth.request_json("GET", config.AEC2_BASE, path)

    def get_worker_events(self, workspace: str, job_name: str, worker_name: str) -> dict[str, Any]:
        path = f"{self._acp_base_path(workspace)}/trainingJobs/{job_name}/workers/{worker_name}/events"
        return self.auth.request_json("GET", config.AEC2_BASE, path)

    # ── AEC2 (Cluster Resources) ─────────────────────────────────────────

    def list_workspace_bindings(self, workspace: str) -> dict[str, Any]:
        path = (
            f"{self._aec2_base_path()}"
            f"/workspaces/{workspace}/workspaceAEC2Bindings?page_size=100"
        )
        return self.auth.request_json("GET", config.AEC2_BASE, path)

    # ── Monitor (Logs) ───────────────────────────────────────────────────

    def query_logs(
        self,
        telemetry_station: str,
        product: str,
        body: dict[str, Any],
        zone: str | None = None,
    ) -> dict[str, Any]:
        zone = zone or config.WORKSPACE_ZONE
        path = (
            f"/monitor/ts/data/v1/subscriptions/{config.SUBSCRIPTION}"
            f"/resourceGroups/{config.RESOURCE_GROUP}"
            f"/zones/{zone}"
            f"/telemetryStations/{telemetry_station}"
            f"/logStream/products/{product}/logs"
        )
        return self.auth.request_json("POST", config.MONITOR_BASE, path, body=body)

    # ── Health checks ────────────────────────────────────────────────────

    def ping(self, base_url: str) -> bool:
        """Lightweight connectivity check — try a signed GET with page_size=1."""
        try:
            # Use a minimal ACP list call as the health probe
            path = f"{self._acp_base_path('share-space-01e')}/trainingJobs?page_size=1"
            if "cci" in base_url:
                path = f"{self._cci_base_path('share-space-01e')}/apps"
            elif "monitor" in base_url:
                # Monitor needs POST, just check TCP reachability
                import socket
                host = base_url.replace("https://", "").split("/")[0]
                sock = socket.create_connection((host, 443), timeout=5)
                sock.close()
                return True

            self.auth.request_json("GET", base_url, path)
            return True
        except Exception:
            return False


# ── sco CLI wrapper (for accurate GPU counts) ────────────────────────────

_sco_path: str | None = None


def _find_sco() -> str | None:
    """Find the sco binary — check PATH and common install locations."""
    path = shutil.which("sco")
    if path:
        return path
    # sco installs to ~/.sco/bin/sco by default
    home_sco = Path.home() / ".sco" / "bin" / "sco"
    if home_sco.is_file():
        return str(home_sco)
    return None


def sco_is_available() -> bool:
    """Check if the sco CLI is installed."""
    global _sco_path
    if _sco_path is None:
        _sco_path = _find_sco() or ""
    return bool(_sco_path)


def sco_cluster_usage(cluster_name: str) -> dict[str, Any] | None:
    """Run `sco aec2 clusters usage` and parse the table output.

    Returns dict like:
        {"gpu_total": 312, "gpu_used": 236, "gpu_idle": 76,
         "vcpu_total": 6800, "vcpu_idle": 2418, "mem_total_gib": 74880, "mem_idle_gib": 22170}
    Returns None if sco is not available or the command fails.
    """
    if not sco_is_available():
        return None
    try:
        result = subprocess.run(
            [_sco_path, "aec2", "clusters", "usage", f"--name={cluster_name}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return _parse_sco_usage_table(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _parse_sco_usage_table(text: str) -> dict[str, Any]:
    """Parse the tabular output of `sco aec2 clusters usage`.

    Actual output format (pipe-delimited with border lines):
    +-----------------+----------------+...+
    |  RESOURCE NAME  | RESERVED TOTAL | RESERVED USED | RESERVED IDLE | ...
    +-----------------+----------------+...+
    |   GPU_NUMBER    |      304       |      277      |      27       | ...
    | GPU_MEMORY (GB) |     24320      |     22160     |     2160      | ...
    |   vCPU_NUMBER   |      6688      |     5035      |     1653      | ...
    |  MEMORY (GiB)   |     72960      |     60437     |     12523     | ...
    +-----------------+----------------+...+
    """
    parsed: dict[str, Any] = {}
    for line in text.strip().splitlines():
        # Skip border lines
        if line.strip().startswith("+"):
            continue
        # Split by | and strip whitespace; first and last are empty from leading/trailing |
        cols = [c.strip() for c in line.split("|")]
        # Filter out empty strings from leading/trailing pipes
        cols = [c for c in cols if c]
        if len(cols) < 4:
            continue
        item = cols[0].upper()
        # Skip header row
        if "RESOURCE" in item or "NAME" in item:
            continue
        try:
            if "GPU_NUMBER" in item or item.startswith("GPU_N"):
                parsed["gpu_total"] = int(cols[1])
                parsed["gpu_used"] = int(cols[2])
                parsed["gpu_idle"] = int(cols[3])
            elif "VCPU" in item or "CPU_NUMBER" in item:
                parsed["vcpu_total"] = int(cols[1])
                parsed["vcpu_idle"] = int(cols[3])
            elif "MEMORY" in item and "GPU" not in item:
                parsed["mem_total_gib"] = int(cols[1])
                parsed["mem_idle_gib"] = int(cols[3])
        except (ValueError, IndexError):
            continue
    return parsed
