"""In-memory state store with diff detection for job/CCI/GPU snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import config


class JobState(str, Enum):
    INIT = "INIT"
    PENDING = "PENDING"
    CREATING = "CREATING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    DELETED = "DELETED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_str(cls, s: str) -> "JobState":
        try:
            return cls(s.upper())
        except ValueError:
            return cls.UNKNOWN


@dataclass
class JobSnapshot:
    name: str
    workspace: str
    state: JobState
    display_name: str = ""
    uid: str = ""
    pool_name: str = ""
    gpu_count: int = 0
    spec_name: str = ""
    create_time: str = ""
    owner_username: str = ""
    last_log_line: str = ""  # cached recent log snippet for running jobs

    @property
    def key(self) -> str:
        return f"{self.workspace}/{self.name}"

    @property
    def is_terminal(self) -> bool:
        return self.state.value in config.TERMINAL_JOB_STATES

    @property
    def summary(self) -> str:
        gpu_label = f"{self.gpu_count}x{self.spec_name.split('.')[0]}" if self.spec_name else f"{self.gpu_count}GPU"
        return f"{self.display_name or self.name}  {self.state.value}  {gpu_label}"


@dataclass
class CCIAppSnapshot:
    name: str
    workspace: str
    state: str  # INIT, PROGRESSING, RUNNING, STOPPED, etc.
    display_name: str = ""
    gpu_count: int = 0
    create_time: str = ""
    owner_username: str = ""

    @property
    def key(self) -> str:
        return f"{self.workspace}/{self.name}"


@dataclass
class ClusterGPUSnapshot:
    cluster_name: str
    device: str = ""
    vram_gb: int = 0
    total_gpus: int = 0
    used_gpus: int = 0
    idle_gpus: int = 0

    @property
    def occupancy_pct(self) -> float:
        if self.total_gpus == 0:
            return 0.0
        return self.used_gpus / self.total_gpus


@dataclass
class ServiceHealth:
    aec2_ok: bool = False
    cci_ok: bool = False
    monitor_ok: bool = False
    last_check: float = 0.0


# Transition: (job_key, old_state, new_state)
JobTransition = tuple[str, JobState | None, JobState]
CCITransition = tuple[str, str | None, str]


@dataclass
class StateStore:
    jobs: dict[str, JobSnapshot] = field(default_factory=dict)
    cci_apps: dict[str, CCIAppSnapshot] = field(default_factory=dict)
    gpu_availability: dict[str, ClusterGPUSnapshot] = field(default_factory=dict)
    health: ServiceHealth = field(default_factory=ServiceHealth)
    _initialized_workspaces: set = field(default_factory=set)

    def update_jobs(
        self, workspace: str, new_snapshots: list[JobSnapshot]
    ) -> list[JobTransition]:
        """Update job state. Returns list of (key, old_state, new_state) transitions.

        On the first call per workspace, records state silently (returns empty list)
        to avoid notification spam on startup.
        """
        is_first_load = workspace not in self._initialized_workspaces
        transitions: list[JobTransition] = []
        new_keys = set()

        for snap in new_snapshots:
            new_keys.add(snap.key)
            old = self.jobs.get(snap.key)
            if old is None:
                # New job appeared
                self.jobs[snap.key] = snap
                if not is_first_load:
                    transitions.append((snap.key, None, snap.state))
            elif old.state != snap.state:
                # State changed
                old_state = old.state
                self.jobs[snap.key] = snap
                if not is_first_load:
                    transitions.append((snap.key, old_state, snap.state))
            else:
                # Update metadata without triggering transition
                self.jobs[snap.key] = snap

        # Jobs that disappeared from the workspace (removed/cleaned up)
        for key in list(self.jobs):
            if key.startswith(f"{workspace}/") and key not in new_keys:
                old = self.jobs.pop(key)
                if not is_first_load and not old.is_terminal:
                    transitions.append((key, old.state, JobState.DELETED))

        self._initialized_workspaces.add(workspace)
        return transitions

    def update_cci(
        self, workspace: str, new_apps: list[CCIAppSnapshot]
    ) -> list[CCITransition]:
        """Update CCI app state. Returns transitions."""
        transitions: list[CCITransition] = []
        new_keys = set()

        for app in new_apps:
            new_keys.add(app.key)
            old = self.cci_apps.get(app.key)
            if old is None:
                self.cci_apps[app.key] = app
                transitions.append((app.key, None, app.state))
            elif old.state != app.state:
                old_state = old.state
                self.cci_apps[app.key] = app
                transitions.append((app.key, old_state, app.state))
            else:
                self.cci_apps[app.key] = app

        for key in list(self.cci_apps):
            if key.startswith(f"{workspace}/") and key not in new_keys:
                old = self.cci_apps.pop(key)
                transitions.append((key, old.state, "DELETED"))

        return transitions

    def update_gpu(self, snapshots: list[ClusterGPUSnapshot]) -> None:
        """Replace GPU availability data."""
        self.gpu_availability = {s.cluster_name: s for s in snapshots}

    def active_jobs(self) -> list[JobSnapshot]:
        """Jobs in non-terminal states, sorted by create_time descending."""
        return sorted(
            [j for j in self.jobs.values() if not j.is_terminal],
            key=lambda j: j.create_time or "",
            reverse=True,
        )

    def recent_terminal_jobs(self, limit: int = 5) -> list[JobSnapshot]:
        """Recently terminated jobs."""
        terminal = [j for j in self.jobs.values() if j.is_terminal]
        return sorted(terminal, key=lambda j: j.create_time or "", reverse=True)[:limit]

    def _is_mine(self, owner_username: str) -> bool:
        return bool(config.MY_USERNAMES and owner_username in config.MY_USERNAMES)

    def my_jobs(self) -> list[JobSnapshot]:
        """Jobs owned by MY_USERNAMES (if set), sorted by create_time descending."""
        if not config.MY_USERNAMES:
            return list(self.jobs.values())
        return sorted(
            [j for j in self.jobs.values() if self._is_mine(j.owner_username)],
            key=lambda j: j.create_time or "",
            reverse=True,
        )

    def other_jobs_count(self) -> int:
        if not config.MY_USERNAMES:
            return 0
        return sum(1 for j in self.jobs.values() if not self._is_mine(j.owner_username))

    def jobs_by_workspace(self, mine_only: bool = True) -> dict[str, list[JobSnapshot]]:
        result: dict[str, list[JobSnapshot]] = {ws: [] for ws in config.WORKSPACES}
        source = self.my_jobs() if mine_only and config.MY_USERNAMES else list(self.jobs.values())
        for job in source:
            result.setdefault(job.workspace, []).append(job)
        return result

    def my_cci_apps(self) -> list[CCIAppSnapshot]:
        if not config.MY_USERNAMES:
            return list(self.cci_apps.values())
        return [a for a in self.cci_apps.values() if self._is_mine(a.owner_username)]

    def other_cci_count(self) -> int:
        if not config.MY_USERNAMES:
            return 0
        return sum(1 for a in self.cci_apps.values() if not self._is_mine(a.owner_username))

    def cci_by_workspace(self, mine_only: bool = True) -> dict[str, list[CCIAppSnapshot]]:
        result: dict[str, list[CCIAppSnapshot]] = {}
        source = self.my_cci_apps() if mine_only and config.MY_USERNAMES else list(self.cci_apps.values())
        for app in source:
            result.setdefault(app.workspace, []).append(app)
        return result


def parse_job_snapshots(workspace: str, raw: dict[str, Any]) -> list[JobSnapshot]:
    """Parse API response into JobSnapshot list."""
    from .api_client import gpu_count_from_spec, _extract_gpu_from_spec_name

    snapshots = []
    for job in raw.get("training_jobs") or []:
        state = JobState.from_str(job.get("state", "UNKNOWN"))

        # Extract GPU info from resource spec
        gpu_count = 0
        spec_name = ""
        roles = job.get("roles") or []
        if roles:
            specs = roles[0].get("resource_spec") or []
            if specs:
                spec = specs[0]
                spec_name = spec.get("name", "")
                gpu_count = gpu_count_from_spec(spec)
                replicas = spec.get("replicas", 1)
                if gpu_count == 0 and spec_name:
                    gpu_count = _extract_gpu_from_spec_name(spec_name)
                gpu_count *= int(replicas or 1)

        pool = job.get("resource_pool") or {}
        ownership = job.get("ownership") or {}
        snapshots.append(
            JobSnapshot(
                name=job.get("name", ""),
                workspace=workspace,
                state=state,
                display_name=job.get("display_name", ""),
                uid=job.get("uid", "") or "",
                pool_name=pool.get("name", ""),
                gpu_count=gpu_count,
                spec_name=spec_name,
                create_time=job.get("create_time", ""),
                owner_username=ownership.get("user_name", ""),
            )
        )
    return snapshots
