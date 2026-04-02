"""Tests for state.py — diff detection, job parsing, state transitions."""

import pytest
from sensewatch.state import (
    JobSnapshot,
    JobState,
    StateStore,
    parse_job_snapshots,
)


def _job(name: str, state: str, workspace: str = "share-space-01e", **kw) -> JobSnapshot:
    return JobSnapshot(
        name=name,
        workspace=workspace,
        state=JobState.from_str(state),
        **kw,
    )


class TestStateStore:
    def test_initial_load_suppresses_notifications(self):
        store = StateStore()
        jobs = [_job("pt-abc", "RUNNING"), _job("pt-def", "STARTING")]
        transitions = store.update_jobs("share-space-01e", jobs)
        assert transitions == []  # First load is silent

    def test_second_load_detects_new_job(self):
        store = StateStore()
        store.update_jobs("share-space-01e", [_job("pt-abc", "RUNNING")])

        jobs = [_job("pt-abc", "RUNNING"), _job("pt-new", "STARTING")]
        transitions = store.update_jobs("share-space-01e", jobs)
        assert len(transitions) == 1
        key, old, new = transitions[0]
        assert key == "share-space-01e/pt-new"
        assert old is None
        assert new == JobState.STARTING

    def test_state_change_detected(self):
        store = StateStore()
        store.update_jobs("share-space-01e", [_job("pt-abc", "STARTING")])

        transitions = store.update_jobs(
            "share-space-01e", [_job("pt-abc", "RUNNING")]
        )
        assert len(transitions) == 1
        key, old, new = transitions[0]
        assert old == JobState.STARTING
        assert new == JobState.RUNNING

    def test_no_change_no_transition(self):
        store = StateStore()
        store.update_jobs("share-space-01e", [_job("pt-abc", "RUNNING")])

        transitions = store.update_jobs(
            "share-space-01e", [_job("pt-abc", "RUNNING")]
        )
        assert transitions == []

    def test_job_disappears_emits_deleted(self):
        store = StateStore()
        store.update_jobs("share-space-01e", [_job("pt-abc", "RUNNING")])

        transitions = store.update_jobs("share-space-01e", [])
        assert len(transitions) == 1
        _, old, new = transitions[0]
        assert old == JobState.RUNNING
        assert new == JobState.DELETED

    def test_terminal_job_disappearing_is_silent(self):
        store = StateStore()
        store.update_jobs("share-space-01e", [_job("pt-abc", "SUCCEEDED")])

        transitions = store.update_jobs("share-space-01e", [])
        assert transitions == []  # Terminal jobs don't emit DELETED

    def test_multiple_workspaces_independent(self):
        store = StateStore()
        # First load per workspace is always silent
        store.update_jobs("share-space-01e", [_job("pt-a", "RUNNING", workspace="share-space-01e")])
        store.update_jobs("project-one", [_job("pt-b", "STARTING", workspace="project-one")])

        # Change in one workspace doesn't affect the other
        transitions = store.update_jobs(
            "share-space-01e",
            [_job("pt-a", "SUCCEEDED", workspace="share-space-01e")],
        )
        assert len(transitions) == 1
        assert "pt-b" not in transitions[0][0]
        assert store.jobs.get("project-one/pt-b") is not None

    def test_second_workspace_first_load_is_silent(self):
        """Each workspace's first load is silent independently."""
        store = StateStore()
        # First load of ws1
        store.update_jobs("share-space-01e", [_job("pt-a", "RUNNING", workspace="share-space-01e")])
        # First load of ws2 should also be silent
        transitions = store.update_jobs("project-one", [_job("pt-b", "RUNNING", workspace="project-one")])
        assert transitions == []

    def test_active_jobs_filters_terminal(self):
        store = StateStore()
        store.update_jobs(
            "share-space-01e",
            [
                _job("pt-run", "RUNNING"),
                _job("pt-done", "SUCCEEDED"),
                _job("pt-fail", "FAILED"),
            ],
        )
        active = store.active_jobs()
        assert len(active) == 1
        assert active[0].name == "pt-run"


class TestJobSnapshot:
    def test_key_format(self):
        j = _job("pt-abc", "RUNNING")
        assert j.key == "share-space-01e/pt-abc"

    def test_is_terminal(self):
        assert _job("a", "SUCCEEDED").is_terminal
        assert _job("a", "FAILED").is_terminal
        assert _job("a", "STOPPED").is_terminal
        assert not _job("a", "RUNNING").is_terminal
        assert not _job("a", "STARTING").is_terminal

    def test_from_str_unknown(self):
        assert JobState.from_str("WEIRD_STATE") == JobState.UNKNOWN


class TestParseJobSnapshots:
    def test_basic_parsing(self):
        raw = {
            "training_jobs": [
                {
                    "name": "pt-abc123",
                    "display_name": "my-training",
                    "state": "RUNNING",
                    "uid": "deadbeef",
                    "resource_pool": {"name": "computing-cluster-01e"},
                    "create_time": "2026-04-01T10:00:00Z",
                    "roles": [
                        {
                            "resource_spec": [
                                {
                                    "name": "N6lS.Iu.I10.8.64c1024g",
                                    "replicas": 2,
                                    "device": {"number": 8},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        snapshots = parse_job_snapshots("share-space-01e", raw)
        assert len(snapshots) == 1
        s = snapshots[0]
        assert s.name == "pt-abc123"
        assert s.display_name == "my-training"
        assert s.state == JobState.RUNNING
        assert s.uid == "deadbeef"
        assert s.gpu_count == 16  # 8 GPU * 2 replicas
        assert s.pool_name == "computing-cluster-01e"

    def test_empty_response(self):
        assert parse_job_snapshots("ws", {}) == []
        assert parse_job_snapshots("ws", {"training_jobs": []}) == []
