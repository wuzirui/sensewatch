"""Construct the rumps menu tree from the current state snapshot."""

from __future__ import annotations

import subprocess
import webbrowser

import rumps

from . import config
from .state import (
    CCIAppSnapshot,
    ClusterGPUSnapshot,
    JobSnapshot,
    JobState,
    ServiceHealth,
    StateStore,
)

# ── Status indicators ─────────────────────────────────────────────────────

_GREEN = "\u2022"  # bullet
_RED = "\u25cf"    # filled circle
_YELLOW = "\u25cb" # open circle


def _health_dot(ok: bool) -> str:
    return f"  {_GREEN}" if ok else f"  {_RED}"


# ── Section builders ──────────────────────────────────────────────────────

def build_health_section(health: ServiceHealth) -> list[rumps.MenuItem]:
    header = rumps.MenuItem("Connection", callback=None)
    header.set_callback(None)
    items = [
        header,
        rumps.MenuItem(f"  AEC2 (Jobs/GPU){_health_dot(health.aec2_ok)}"),
        rumps.MenuItem(f"  CCI (Containers){_health_dot(health.cci_ok)}"),
        rumps.MenuItem(f"  Monitor (Logs){_health_dot(health.monitor_ok)}"),
    ]
    # Make sub-items non-clickable
    for item in items:
        item.set_callback(None)
    return items


def _make_job_item(job: JobSnapshot, app_ref: "SenseWatchApp | None" = None) -> rumps.MenuItem:
    """Create a menu item for a single training job with actions submenu."""
    state_str = job.state.value
    # Schrodinger's Job easter egg
    if job.state == JobState.STARTING and job.create_time:
        import time
        from datetime import datetime, timezone
        try:
            created = datetime.fromisoformat(job.create_time.replace("Z", "+00:00"))
            elapsed = time.time() - created.timestamp()
            if elapsed > 600:  # 10 minutes
                state_str = "STARTING (or is it?) \U0001f431"
        except (ValueError, OSError):
            pass

    prefix = ""
    if job.state == JobState.FAILED:
        prefix = "\u26a0 "
    elif job.state == JobState.RUNNING:
        prefix = "\u25b6 "

    gpu_label = ""
    if job.gpu_count > 0:
        device = job.spec_name.split(".")[0] if job.spec_name else "GPU"
        gpu_label = f"  {job.gpu_count}x{device}"

    label = f"  {prefix}{job.display_name or job.name}  {state_str}{gpu_label}"
    item = rumps.MenuItem(label)

    # Show last log line for running jobs
    if job.last_log_line and job.state == JobState.RUNNING:
        log_preview = rumps.MenuItem(f"    {job.last_log_line}")
        log_preview.set_callback(None)
        item.add(log_preview)

    # Submenu actions
    # View Logs — works for both running and terminal jobs
    if app_ref is not None:
        view_logs = rumps.MenuItem(
            "View Logs...",
            callback=lambda _, j=job: app_ref.show_job_logs(j),
        )
        item.add(view_logs)

    def copy_name(_):
        subprocess.run(["pbcopy"], input=job.name.encode(), check=True)
        rumps.notification("SenseWatch", "Copied!", f"{job.name}", sound=False)

    copy_item = rumps.MenuItem("Copy Job Name", callback=copy_name)
    item.add(copy_item)

    console_url = (
        f"https://console.sensecore.cn/acp/{config.SUBSCRIPTION}/training?page=1"
    )
    open_console = rumps.MenuItem(
        "Open in Console",
        callback=lambda _: webbrowser.open(console_url),
    )
    item.add(open_console)

    return item


def _make_cci_item(app_snap: CCIAppSnapshot, app_ref=None) -> rumps.MenuItem:
    state_indicator = ""
    if app_snap.state == "RUNNING":
        state_indicator = "\u25b6 "
    elif app_snap.state in ("STOPPED", "SUSPENDED"):
        state_indicator = "\u25a0 "

    label = f"  {state_indicator}{app_snap.display_name or app_snap.name}  {app_snap.state}"
    if app_snap.gpu_count > 0:
        label += f"  {app_snap.gpu_count}GPU"
    item = rumps.MenuItem(label)

    # Start/Stop buttons depending on current state
    if app_ref is not None:
        if app_snap.state in ("STOPPED", "SUSPENDED"):
            start_item = rumps.MenuItem(
                "Start",
                callback=lambda _, s=app_snap: app_ref.cci_start(s),
            )
            item.add(start_item)
        elif app_snap.state == "RUNNING":
            stop_item = rumps.MenuItem(
                "Stop",
                callback=lambda _, s=app_snap: app_ref.cci_stop(s),
            )
            item.add(stop_item)

    console_url = (
        f"https://console.sensecore.cn/cci/{config.SUBSCRIPTION}/app"
    )
    open_console = rumps.MenuItem(
        "Open in Console",
        callback=lambda _: webbrowser.open(console_url),
    )
    item.add(open_console)
    return item


def build_job_section(state: StateStore, app_ref=None) -> list[rumps.MenuItem]:
    items = []
    by_ws = state.jobs_by_workspace(mine_only=True)
    others_count = state.other_jobs_count()

    for ws in config.WORKSPACES:
        jobs = by_ws.get(ws, [])
        label = f"My Jobs ({ws})" if config.MY_USERNAMES else f"Training Jobs ({ws})"
        header = rumps.MenuItem(label)
        header.set_callback(None)
        items.append(header)
        if not jobs:
            empty = rumps.MenuItem("  (no active jobs)")
            empty.set_callback(None)
            items.append(empty)
        else:
            for job in jobs:
                items.append(_make_job_item(job, app_ref))

    if others_count > 0:
        others = rumps.MenuItem(f"  ({others_count} other users' jobs hidden)")
        others.set_callback(None)
        items.append(others)

    return items


def build_cci_section(state: StateStore, app_ref=None) -> list[rumps.MenuItem]:
    items = []
    by_ws = state.cci_by_workspace(mine_only=True)
    others_count = state.other_cci_count()

    has_any = any(apps for apps in by_ws.values())
    if not has_any:
        label = "My CCI Containers" if config.MY_USERNAMES else "CCI Containers"
        header = rumps.MenuItem(label)
        header.set_callback(None)
        items.append(header)
        empty = rumps.MenuItem("  (no containers)")
        empty.set_callback(None)
        items.append(empty)
    else:
        for ws, apps in by_ws.items():
            label = f"My CCI Containers ({ws})" if config.MY_USERNAMES else f"CCI Containers ({ws})"
            header = rumps.MenuItem(label)
            header.set_callback(None)
            items.append(header)
            for app_snap in apps:
                items.append(_make_cci_item(app_snap, app_ref))

    if others_count > 0:
        others = rumps.MenuItem(f"  ({others_count} other users' containers hidden)")
        others.set_callback(None)
        items.append(others)

    return items


def build_gpu_section(state: StateStore) -> list[rumps.MenuItem]:
    header = rumps.MenuItem("GPU Availability")
    header.set_callback(None)
    items = [header]

    if not state.gpu_availability:
        loading = rumps.MenuItem("  Loading...")
        loading.set_callback(None)
        items.append(loading)
        return items

    for cluster_name in config.CLUSTERS:
        snap = state.gpu_availability.get(cluster_name)
        if snap is None:
            continue
        meta = config.CLUSTERS[cluster_name]
        label = f"  {cluster_name}"
        cluster_item = rumps.MenuItem(label)
        cluster_item.set_callback(None)

        detail = f"    Idle: {snap.idle_gpus} / {snap.total_gpus} GPU  ({meta['device']} {meta['vram_gb']}GB)"
        detail_item = rumps.MenuItem(detail)
        detail_item.set_callback(None)
        items.append(cluster_item)
        items.append(detail_item)

    return items


def build_menu(state: StateStore, flavor_text: str = "", app_ref=None) -> list[rumps.MenuItem]:
    """Build the full menu from current state."""
    items: list[rumps.MenuItem] = []

    # Connection health
    items.extend(build_health_section(state.health))
    items.append(rumps.separator)

    # Training jobs
    items.extend(build_job_section(state, app_ref))
    items.append(rumps.separator)

    # CCI containers
    items.extend(build_cci_section(state, app_ref))
    items.append(rumps.separator)

    # GPU availability
    items.extend(build_gpu_section(state))
    items.append(rumps.separator)

    # Refresh button — wire callback directly
    refresh = rumps.MenuItem("Refresh Now")
    if app_ref is not None:
        refresh.set_callback(app_ref._on_refresh)
    items.append(refresh)

    # Flavor text footer
    if flavor_text:
        items.append(rumps.separator)
        flavor_item = rumps.MenuItem(flavor_text)
        flavor_item.set_callback(None)
        items.append(flavor_item)

    return items
