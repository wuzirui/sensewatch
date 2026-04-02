"""pywebview JS↔Python bridge — exposes backend functions to the panel frontend."""

from __future__ import annotations

import json
import subprocess
import threading
import webbrowser
from dataclasses import asdict
from typing import TYPE_CHECKING

from . import config, easter_eggs
from .state import JobState

if TYPE_CHECKING:
    from .app import SenseWatchApp


class Bridge:
    """API object exposed to JavaScript via pywebview. All methods are callable
    from JS as `await pywebview.api.method_name(args)`."""

    def __init__(self, app: "SenseWatchApp"):
        self.app = app

    def get_state(self) -> dict:
        """Return full state snapshot for the frontend to render."""
        state = self.app.state
        my_jobs = state.my_jobs()
        my_cci = state.my_cci_apps()

        # Serialize jobs
        jobs = []
        for j in my_jobs:
            jobs.append({
                "name": j.name,
                "display_name": j.display_name or j.name,
                "workspace": j.workspace,
                "state": j.state.value,
                "gpu_count": j.gpu_count,
                "spec_name": j.spec_name,
                "pool_name": j.pool_name,
                "last_log_line": j.last_log_line,
                "is_terminal": j.is_terminal,
                "uid": j.uid,
                "create_time": j.create_time,
            })

        # Serialize CCI
        cci = []
        for c in my_cci:
            cci.append({
                "name": c.name,
                "display_name": c.display_name or c.name,
                "workspace": c.workspace,
                "state": c.state,
                "gpu_count": c.gpu_count,
            })

        # Serialize GPU
        gpus = []
        for cluster_name in config.CLUSTERS:
            snap = state.gpu_availability.get(cluster_name)
            meta = config.CLUSTERS[cluster_name]
            if snap:
                gpus.append({
                    "cluster": cluster_name,
                    "device": snap.device or meta.get("device", ""),
                    "vram_gb": snap.vram_gb or meta.get("vram_gb", 0),
                    "total": snap.total_gpus,
                    "used": snap.used_gpus,
                    "idle": snap.idle_gpus,
                    "commentary": easter_eggs.gpu_commentary(snap.idle_gpus, snap.total_gpus),
                })

        # Health
        health = {
            "aec2": state.health.aec2_ok,
            "cci": state.health.cci_ok,
            "monitor": state.health.monitor_ok,
        }

        # Flavor text
        running = sum(1 for j in my_jobs if j.state.value in config.ACTIVE_JOB_STATES)
        total_gpus = sum(s.total_gpus for s in state.gpu_availability.values())
        import time
        from datetime import datetime
        start_str = datetime.fromtimestamp(self.app.start_time).strftime("%H:%M")

        flavor = easter_eggs.flavor_text(
            running_jobs=running,
            gpu_total=total_gpus,
            start_time=start_str,
            connected=state.health.aec2_ok,
        )

        # Counts
        other_jobs = state.other_jobs_count()
        other_cci = state.other_cci_count()

        return {
            "jobs": jobs,
            "cci": cci,
            "gpus": gpus,
            "health": health,
            "flavor": flavor,
            "other_jobs_count": other_jobs,
            "other_cci_count": other_cci,
        }

    def refresh(self) -> dict:
        """Trigger all pollers, return updated state."""
        threads = [
            threading.Thread(target=self.app.poller.poll_health, daemon=True),
            threading.Thread(target=self.app.poller.poll_jobs, daemon=True),
            threading.Thread(target=self.app.poller.poll_cci, daemon=True),
            threading.Thread(target=self.app.poller.poll_gpu, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return self.get_state()

    def get_job_logs(self, job_name: str, workspace: str) -> str:
        """Fetch logs for a job (running or terminated)."""
        job = self.app.state.jobs.get(f"{workspace}/{job_name}")
        if not job:
            return f"(Job {job_name} not found)"
        if job.is_terminal:
            return self.app.log_viewer.fetch_offline_logs(job)
        else:
            return self.app.log_viewer.fetch_live_logs(job)

    def cci_start(self, name: str, workspace: str) -> str:
        """Start a CCI container. Returns status message."""
        try:
            self.app.client.start_cci_app(workspace, name)
            self.app.poller.poll_cci()
            return "ok"
        except Exception as e:
            return f"error: {e}"

    def cci_stop(self, name: str, workspace: str) -> str:
        """Stop a CCI container. Returns status message."""
        try:
            self.app.client.stop_cci_app(workspace, name)
            self.app.poller.poll_cci()
            return "ok"
        except Exception as e:
            return f"error: {e}"

    def get_job_detail(self, job_name: str, workspace: str) -> dict:
        """Get workers + events for a job (for the detail view)."""
        workers = []
        job_events = []
        worker_events = []

        try:
            raw = self.app.client.list_workers(workspace, job_name)
            for w in raw.get("workers") or []:
                workers.append({
                    "name": w.get("name", ""),
                    "phase": w.get("phase", ""),
                    "host_ip": w.get("host_ip", ""),
                    "ip": w.get("ip", ""),
                    "device_type": (w.get("containers") or [{}])[0].get("device_type", "") if w.get("containers") else "",
                })
        except Exception:
            pass

        try:
            raw = self.app.client.get_job_events(workspace, job_name)
            for e in raw.get("events") or []:
                job_events.append({
                    "type": e.get("type", ""),
                    "reason": e.get("reason", ""),
                    "age": e.get("age", ""),
                    "message": e.get("message", ""),
                })
        except Exception:
            pass

        # Get per-worker events (FailedScheduling etc.)
        for w in workers:
            if w["name"]:
                try:
                    raw = self.app.client.get_worker_events(workspace, job_name, w["name"])
                    for e in raw.get("events") or []:
                        worker_events.append({
                            "worker": w["name"].split("-")[-1],  # e.g. "worker-0"
                            "type": e.get("type", ""),
                            "reason": e.get("reason", ""),
                            "age": e.get("age", ""),
                            "message": e.get("message", ""),
                        })
                except Exception:
                    pass

        return {
            "workers": workers,
            "job_events": job_events,
            "worker_events": worker_events,
        }

    def get_gpu_breakdown(self, cluster_name: str) -> dict:
        """Get per-node GPU breakdown for a cluster.

        Uses sco's idle count as ground truth. Scans workers to show
        which partially-used nodes have free slots. The remainder
        (sco_idle - sum of per-node idle) is reported as unaccounted.
        """
        from collections import defaultdict
        from .api_client import sco_cluster_usage

        node_usage: dict[str, int] = defaultdict(int)
        gpus_per_node = 8

        # Get the authoritative idle count from sco
        sco_data = sco_cluster_usage(cluster_name)
        sco_idle = sco_data["gpu_idle"] if sco_data and "gpu_idle" in sco_data else None
        sco_total = sco_data["gpu_total"] if sco_data and "gpu_total" in sco_data else None

        for ws in config.WORKSPACES:
            try:
                raw = self.app.client.list_training_jobs(ws, page_size=200)
            except Exception:
                continue
            for job in raw.get("training_jobs") or []:
                pool = (job.get("resource_pool") or {}).get("name", "")
                if pool != cluster_name:
                    continue
                if job.get("state") not in config.ACTIVE_JOB_STATES:
                    continue
                try:
                    workers = self.app.client.list_workers(ws, job["name"])
                    for w in workers.get("workers") or []:
                        host = w.get("host_ip", "")
                        if not host:
                            continue
                        for c in w.get("containers") or []:
                            res = (c.get("resources") or {}).get("limits") or {}
                            node_usage[host] += int(res.get("nvidia.com/gpu", 0))
                except Exception:
                    pass

        # Build node list — only partially used nodes (have idle slots)
        nodes = []
        node_idle_sum = 0
        for host in sorted(node_usage):
            used = node_usage[host]
            idle = max(0, gpus_per_node - used)
            if idle > 0:
                nodes.append({"host": host, "used": used, "idle": idle, "total": gpus_per_node})
                node_idle_sum += idle

        # Use sco as ground truth for total idle
        if sco_idle is not None:
            displayed_idle = sco_idle
            visible_gaps = node_idle_sum
            # Our visible per-node gaps may exceed sco idle (other subscriptions use GPUs we can't see)
            note = ""
            if visible_gaps > sco_idle:
                note = f"({visible_gaps - sco_idle} used by other subscriptions)"
        else:
            displayed_idle = node_idle_sum
            note = ""

        idle_parts = [str(n["idle"]) for n in nodes]
        idle_summary = "+".join(idle_parts) if idle_parts else "0"

        return {
            "cluster": cluster_name,
            "nodes": nodes,
            "idle_summary": idle_summary,
            "sco_idle": displayed_idle,
            "visible_gaps": node_idle_sum,
            "note": note,
            "total_nodes": len(node_usage),
            "full_nodes": sum(1 for v in node_usage.values() if v >= gpus_per_node),
        }

    def open_console(self, url: str) -> None:
        webbrowser.open(url)

    def copy_to_clipboard(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)

    def hide_panel(self) -> None:
        self.app.hide_panel()

    def quit_app(self) -> None:
        import os, signal
        os.kill(os.getpid(), signal.SIGINT)
