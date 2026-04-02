"""Polling engine: schedules API calls, feeds state store, triggers notifications."""

from __future__ import annotations

import logging
import time
import urllib.error
from typing import TYPE_CHECKING

from . import config, easter_eggs
from .api_client import SenseCoreAPIClient, sco_cluster_usage, sco_is_available
from .state import (
    CCIAppSnapshot,
    ClusterGPUSnapshot,
    JobState,
    StateStore,
    parse_job_snapshots,
)
from .notifier import Notifier

if TYPE_CHECKING:
    from .app import SenseWatchApp

log = logging.getLogger("sensewatch")


class Poller:
    def __init__(
        self,
        client: SenseCoreAPIClient,
        state: StateStore,
        notifier: Notifier,
        app: "SenseWatchApp",
    ):
        self.client = client
        self.state = state
        self.notifier = notifier
        self.app = app

    # ── ACP jobs ──────────────────────────────────────────────────────────

    def poll_jobs(self, _timer=None) -> None:
        for workspace in config.WORKSPACES:
            try:
                raw = self.client.list_training_jobs(workspace)
                snapshots = parse_job_snapshots(workspace, raw)
                transitions = self.state.update_jobs(workspace, snapshots)
                for job_key, old_state, new_state in transitions:
                    # Only notify for my jobs
                    job = self.state.jobs.get(job_key)
                    if job and config.MY_USERNAMES and job.owner_username not in config.MY_USERNAMES:
                        continue
                    self.notifier.on_job_transition(job_key, old_state, new_state)
                self.state.health.aec2_ok = True
            except urllib.error.HTTPError as e:
                log.warning("ACP poll failed for %s: HTTP %s", workspace, e.code)
                if e.code == 401:
                    self.state.health.aec2_ok = False
            except Exception:
                log.exception("ACP poll error for %s", workspace)

        self._check_uptime()
        self.app.mark_dirty()

    # ── CCI containers ────────────────────────────────────────────────────

    def poll_cci(self, _timer=None) -> None:
        for workspace in config.WORKSPACES:
            try:
                raw = self.client.list_cci_apps(workspace)
                apps = self._parse_cci_apps(workspace, raw)
                self.state.update_cci(workspace, apps)
                self.state.health.cci_ok = True
            except urllib.error.HTTPError as e:
                # 403/404 for workspaces without CCI access is normal
                if e.code not in (403, 404):
                    log.warning("CCI poll failed for %s: HTTP %s", workspace, e.code)
                    self.state.health.cci_ok = False
            except Exception:
                log.exception("CCI poll error for %s", workspace)

        self.app.mark_dirty()

    def _parse_cci_apps(self, workspace: str, raw: dict) -> list[CCIAppSnapshot]:
        apps = []
        for item in raw.get("apps") or raw.get("items") or []:
            gpu_count = 0
            template = item.get("template") or {}
            containers = template.get("containers") or []
            if containers:
                res_req = containers[0].get("resource_request") or {}
                gpu_str = res_req.get("nvidia.com/gpu", "0")
                try:
                    gpu_count = int(gpu_str)
                except (ValueError, TypeError):
                    pass

            ownership = item.get("ownership") or {}
            apps.append(
                CCIAppSnapshot(
                    name=item.get("name", ""),
                    workspace=workspace,
                    state=item.get("state", "UNKNOWN"),
                    display_name=item.get("display_name", ""),
                    gpu_count=gpu_count,
                    create_time=item.get("create_time", ""),
                    owner_username=ownership.get("user_name", ""),
                )
            )
        return apps

    # ── GPU availability ──────────────────────────────────────────────────

    def poll_gpu(self, _timer=None) -> None:
        snapshots = []
        for cluster_name, meta in config.CLUSTERS.items():
            snap = self._fetch_cluster_gpu(cluster_name, meta)
            if snap:
                snapshots.append(snap)
        self.state.update_gpu(snapshots)
        self.app.mark_dirty()

    def _fetch_cluster_gpu(
        self, cluster_name: str, meta: dict
    ) -> ClusterGPUSnapshot | None:
        # Try sco CLI first (accurate)
        if sco_is_available():
            usage = sco_cluster_usage(cluster_name)
            if usage and "gpu_total" in usage:
                return ClusterGPUSnapshot(
                    cluster_name=cluster_name,
                    device=meta.get("device", ""),
                    vram_gb=meta.get("vram_gb", 0),
                    total_gpus=usage["gpu_total"],
                    used_gpus=usage["gpu_used"],
                    idle_gpus=usage["gpu_idle"],
                )

        # Fallback: estimate from node count
        nodes = meta.get("nodes", 0)
        total = nodes * 8  # assume 8 GPU per node
        return ClusterGPUSnapshot(
            cluster_name=cluster_name,
            device=meta.get("device", ""),
            vram_gb=meta.get("vram_gb", 0),
            total_gpus=total,
            used_gpus=0,
            idle_gpus=total,
        )

    # ── Health ────────────────────────────────────────────────────────────

    _health_initialized: bool = False

    def poll_health(self, _timer=None) -> None:
        old_health = (
            self.state.health.aec2_ok,
            self.state.health.cci_ok,
            self.state.health.monitor_ok,
        )

        aec2_ok = self.client.ping(config.AEC2_BASE)
        cci_ok = self.client.ping(config.CCI_BASE)
        monitor_ok = self.client.ping(config.MONITOR_BASE)

        # Only notify after the first health check (suppress initial transitions)
        if self._health_initialized:
            if old_health[0] != aec2_ok:
                self.notifier.on_connection_change("AEC2", old_health[0], aec2_ok)
            if old_health[1] != cci_ok:
                self.notifier.on_connection_change("CCI", old_health[1], cci_ok)
            if old_health[2] != monitor_ok:
                self.notifier.on_connection_change("Monitor", old_health[2], monitor_ok)
        self._health_initialized = True

        self.state.health.aec2_ok = aec2_ok
        self.state.health.cci_ok = cci_ok
        self.state.health.monitor_ok = monitor_ok
        self.state.health.last_check = time.time()

        self.app.mark_dirty()

    # ── Log preview for running jobs ────────────────────────────────────

    def poll_log_previews(self, _timer=None) -> None:
        """Fetch last log line for each of my running jobs."""
        my_running = [
            j for j in self.state.my_jobs()
            if j.state == JobState.RUNNING
        ]
        if not my_running:
            return

        for job in my_running:
            try:
                lines = self.app.log_viewer.fetch_live_log_preview(job, timeout=3.0)
                if lines:
                    job.last_log_line = lines[-1][:120]
            except Exception:
                log.debug("Log preview failed for %s", job.name)

        self.app.mark_dirty()

    # ── Uptime ────────────────────────────────────────────────────────────

    def _check_uptime(self) -> None:
        msg = easter_eggs.uptime_check(self.app.start_time)
        if msg:
            self.notifier.on_uptime_milestone(msg)
