"""Tests for tools/acp.py — the agent-friendly ACP CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


from sensecore_cli import acp, log_extract


def test_packaged_acp_has_no_dreamdojo_repo_imports() -> None:
    source = Path(acp.__file__).read_text(encoding="utf-8")
    assert acp.HMAC_HELPER.parent == Path(acp.__file__).resolve().parent
    assert "from tools" not in source
    assert "skills/sensecore-api-workflows" not in source


# ---------------------------------------------------------------------------
# Phase 1: Foundation — cache, config, HMAC client, workspace discovery
# ---------------------------------------------------------------------------


class ConfigLoadingTests(unittest.TestCase):
    def test_load_config_returns_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.toml"
            cfg = acp.load_config(path)
        # Defaults chosen to match the lightest real 8-GPU spec (64c1024g = 8 CPU/GPU, 128 GB/GPU)
        # so the "prefer lighter" sort actually wins by default.
        self.assertEqual(cfg["defaults"]["cpus_per_gpu"], 8)
        self.assertEqual(cfg["defaults"]["mem_per_gpu_gb"], 128)
        self.assertEqual(cfg["defaults"]["workspace"], "p18-eacv")
        self.assertIn("WANDB_API_KEY", cfg["defaults"]["forward_env"])
        self.assertEqual(cfg["afs_mount"]["mount_path"], "/mnt/afs")
        self.assertEqual(cfg["workspace_quota"], {"p18-eacv": 64})

    def test_load_config_migrates_retired_p1_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acp.toml"
            path.write_text(
                '[defaults]\n'
                'workspace = "p1-video-world-model-for-robot-learning"\n'
                '[workspace_quota]\n'
                '"p18-eacv" = 64\n'
                '"p1-video-world-model-for-robot-learning" = 56\n',
                encoding="utf-8",
            )
            cfg = acp.load_config(path)
        self.assertEqual(cfg["defaults"]["workspace"], "p18-eacv")
        self.assertEqual(cfg["workspace_quota"], {"p18-eacv": 64})

    def test_load_config_overlays_user_overrides_on_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acp.toml"
            path.write_text(
                '[defaults]\n'
                'cpus_per_gpu = 16\n'
                'forward_env = ["MY_TOKEN"]\n',
                encoding="utf-8",
            )
            cfg = acp.load_config(path)
        self.assertEqual(cfg["defaults"]["cpus_per_gpu"], 16)
        self.assertEqual(cfg["defaults"]["forward_env"], ["MY_TOKEN"])
        # Unchanged defaults still present
        self.assertEqual(cfg["defaults"]["mem_per_gpu_gb"], 128)
        self.assertEqual(cfg["afs_mount"]["mount_path"], "/mnt/afs")

    def test_bootstrap_config_writes_starter_toml_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "acp.toml"
            acp.bootstrap_config(path)
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("[defaults]", text)
            self.assertIn("cpus_per_gpu", text)
            self.assertIn("[afs_mount]", text)

    def test_bootstrap_config_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acp.toml"
            path.write_text("# user-authored\n", encoding="utf-8")
            acp.bootstrap_config(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "# user-authored\n")


class CacheFreshnessTests(unittest.TestCase):
    def test_cache_missing_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.json"
            self.assertFalse(acp.cache_is_fresh(path, ttl_sec=3600))

    def test_cache_young_file_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{}", encoding="utf-8")
            self.assertTrue(acp.cache_is_fresh(path, ttl_sec=3600))

    def test_cache_old_file_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{}", encoding="utf-8")
            old = time.time() - 7200
            os.utime(path, (old, old))
            self.assertFalse(acp.cache_is_fresh(path, ttl_sec=3600))

    def test_save_and_load_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "cache.json"
            data = {"hello": "world", "list": [1, 2, 3]}
            acp.save_cache(path, data)
            self.assertEqual(acp.load_cache(path), data)

    def test_load_cache_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(acp.load_cache(Path(tmp) / "nope.json"))


class HMACClientTests(unittest.TestCase):
    """HMACClient wraps hmac_request.py so callers don't touch subprocess."""

    def test_get_invokes_hmac_helper_and_parses_json_body(self):
        fake_output = 'STATUS 200\n{"workspace_AEC2_bindings": []}\n'
        with mock.patch.object(acp.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(
                returncode=0, stdout=fake_output, stderr=""
            )
            client = acp.HMACClient()
            result = client.get("/some/path")
        self.assertEqual(result, {"workspace_AEC2_bindings": []})
        # Assert the HMAC helper was called, method GET, path passed through
        args, _ = run_mock.call_args
        cmd = args[0]
        self.assertIn("hmac_request.py", " ".join(cmd))
        self.assertIn("--method", cmd)
        self.assertEqual(cmd[cmd.index("--method") + 1], "GET")
        self.assertEqual(cmd[cmd.index("--path") + 1], "/some/path")

    def test_post_forwards_json_body(self):
        fake_output = 'STATUS 200\n{"ok": true}\n'
        with mock.patch.object(acp.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(
                returncode=0, stdout=fake_output, stderr=""
            )
            client = acp.HMACClient()
            body = {"training_job_names": ["pt-abc123"]}
            result = client.post("/stop", body=body)
        self.assertEqual(result, {"ok": True})
        args, _ = run_mock.call_args
        cmd = args[0]
        self.assertEqual(cmd[cmd.index("--method") + 1], "POST")
        self.assertEqual(cmd[cmd.index("--data") + 1], json.dumps(body))

    def test_non_zero_exit_raises_structured_error(self):
        with mock.patch.object(acp.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr='STATUS 401\n{"error":"invalid credentials"}',
            )
            client = acp.HMACClient()
            with self.assertRaises(acp.APIError) as cm:
                client.get("/whatever")
        self.assertIn("401", str(cm.exception))


class WorkspaceDiscoveryTests(unittest.TestCase):
    def test_discover_workspaces_filters_retired_p1_from_fresh_cache(self):
        cached = [
            {"name": "p1-video-world-model-for-robot-learning", "clusters": []},
            {"name": "p18-eacv", "clusters": ["computing-cluster-01e"]},
            {"name": "share-space-01e", "clusters": ["computing-cluster-01e"]},
        ]
        with mock.patch("sensecore_cli.acp.cache_is_fresh", return_value=True), \
             mock.patch("sensecore_cli.acp.load_cache", return_value=cached):
            workspaces = acp.discover_workspaces()
        self.assertEqual(
            [workspace["name"] for workspace in workspaces],
            ["p18-eacv", "share-space-01e"],
        )

    def test_parse_sco_ws_list_table_extracts_active_names(self):
        raw = (
            "+---+---+---+---+\n"
            "|  NAME                                   | DISPLAY NAME | STATE    | CREATE TIME |\n"
            "+---+---+---+---+\n"
            "| p1-video-world-model-for-robot-learning | p1_display   | ACTIVE   | 2026-04-16  |\n"
            "| share-space-01e                         | share_space  | ACTIVE   | 2025-09-17  |\n"
            "| old-space                               | old          | INACTIVE | 2024-01-01  |\n"
            "+---+---+---+---+\n"
        )
        names = acp.parse_sco_ws_list_table(raw)
        self.assertEqual(names, ["p1-video-world-model-for-robot-learning", "share-space-01e"])

    def test_parse_sco_ws_describe_clusters_filters_active(self):
        payload = {
            "name": "share-space-01e",
            "id": "/subscriptions/x/resourceGroups/default/zones/z/workspaces/share-space-01e",
            "properties": {
                "aec2s": [
                    {"aec2_name": "computing-cluster-01e", "aec2_state": "ACTIVE"},
                    {"aec2_name": "debug-cluster-01e", "aec2_state": "ACTIVE"},
                    {"aec2_name": "retired-cluster", "aec2_state": "DELETED"},
                ]
            },
        }
        clusters = acp.parse_sco_ws_describe_clusters(payload)
        self.assertEqual(clusters, ["computing-cluster-01e", "debug-cluster-01e"])

    def test_parse_sco_ws_describe_clusters_handles_empty(self):
        self.assertEqual(acp.parse_sco_ws_describe_clusters({}), [])
        self.assertEqual(acp.parse_sco_ws_describe_clusters({"properties": {}}), [])

    def test_filter_requested_workspace_refreshes_when_cache_misses_name(self):
        stale = [{"name": "share-space-01e", "resource_id": "rid-old", "clusters": []}]
        fresh = [
            {
                "name": "p1-video-world-model-for-robot-learning",
                "resource_id": "rid-p1",
                "clusters": ["computing-cluster-01e"],
            }
        ]
        refresh_calls = []

        def refresh_fn():
            refresh_calls.append(True)
            return fresh

        selected = acp.filter_requested_workspace(
            stale,
            "p1-video-world-model-for-robot-learning",
            refresh_fn=refresh_fn,
        )

        self.assertEqual(selected, fresh)
        self.assertEqual(refresh_calls, [True])


class SpecCatalogParsingTests(unittest.TestCase):
    def test_parse_resource_specs_extracts_gpu_cpu_mem(self):
        payload = {
            "resource_specs": [
                {
                    "name": "N6lS.Iu.I10.8.64c1024g",
                    "device": {"number": 8, "type": "N6lS"},
                    "cpu": {"vcpu_allocatable": 64},
                    "memory": {"allocatable": 1024},
                },
                {
                    "name": "N6lS.Iu.I10.4.56c832g",
                    "device": {"number": 4, "type": "N6lS"},
                    "cpu": {"vcpu_allocatable": 56},
                    "memory": {"allocatable": 832},
                },
            ]
        }
        out = acp.parse_resource_specs(payload)
        self.assertEqual(len(out), 2)
        by_name = {spec["name"]: spec for spec in out}
        self.assertEqual(by_name["N6lS.Iu.I10.8.64c1024g"]["gpu"], 8)
        self.assertEqual(by_name["N6lS.Iu.I10.8.64c1024g"]["cpu"], 64.0)
        self.assertEqual(by_name["N6lS.Iu.I10.8.64c1024g"]["mem_gb"], 1024.0)


# ---------------------------------------------------------------------------
# Phase 2: list
# ---------------------------------------------------------------------------


class FilterParsingTests(unittest.TestCase):
    def test_parse_since_handles_hours_days_weeks(self):
        self.assertEqual(acp.parse_since("6h"), 6 * 3600)
        self.assertEqual(acp.parse_since("2d"), 2 * 86400)
        self.assertEqual(acp.parse_since("1w"), 7 * 86400)

    def test_parse_since_rejects_bad_format(self):
        with self.assertRaises(ValueError):
            acp.parse_since("yesterday")
        with self.assertRaises(ValueError):
            acp.parse_since("2")
        with self.assertRaises(ValueError):
            acp.parse_since("3m")  # minutes not supported

    def test_parse_state_list_splits_and_uppercases(self):
        self.assertEqual(
            acp.parse_state_list("running,pending"),
            ["RUNNING", "PENDING"],
        )
        self.assertEqual(acp.parse_state_list("RUNNING"), ["RUNNING"])

    def test_job_id_regex_accepts_canonical_shape(self):
        self.assertTrue(acp.is_valid_job_id("pt-abc12345"))
        self.assertTrue(acp.is_valid_job_id("pt-v5ifrf3x"))

    def test_job_id_regex_rejects_names_and_patterns(self):
        self.assertFalse(acp.is_valid_job_id("g8-ember-overfit"))
        self.assertFalse(acp.is_valid_job_id("pt-ABC123"))  # uppercase
        self.assertFalse(acp.is_valid_job_id("pt-*"))
        self.assertFalse(acp.is_valid_job_id(""))


class ListFilteringTests(unittest.TestCase):
    def _job(self, **overrides):
        base = {
            "id": "pt-aaaa1111",
            "name": "pt-aaaa1111",
            "display_name": "g8-ember-overfit",
            "state": "RUNNING",
            "age_sec": 3600,
            "gpus": 8,
            "cluster": "computing-cluster-01e",
            "workspace": "share-space-01e",
            "spec": "N6lS.Iu.I10.8.64c1024g",
        }
        base.update(overrides)
        return base

    def test_filter_by_state(self):
        jobs = [
            self._job(state="RUNNING"),
            self._job(id="pt-b", state="FAILED"),
            self._job(id="pt-c", state="PENDING"),
        ]
        out = acp.filter_jobs(jobs, states=["RUNNING", "PENDING"])
        self.assertEqual({j["id"] for j in out}, {"pt-aaaa1111", "pt-c"})

    def test_filter_by_experiment_substring(self):
        jobs = [
            self._job(display_name="g8-ember-overfit"),
            self._job(id="pt-b", display_name="g16-matcha-n8x2"),
            self._job(id="pt-c", name="pt-matcha-foo", display_name="x"),
        ]
        out = acp.filter_jobs(jobs, experiment="matcha")
        self.assertEqual({j["id"] for j in out}, {"pt-b", "pt-c"})

    def test_filter_by_since_seconds(self):
        jobs = [
            self._job(age_sec=300),       # 5 min old
            self._job(id="pt-b", age_sec=3 * 86400),  # 3d old
        ]
        out = acp.filter_jobs(jobs, since_sec=86400)  # last 1d
        self.assertEqual([j["id"] for j in out], ["pt-aaaa1111"])

    def test_filter_by_id_returns_exact_match_only(self):
        jobs = [
            self._job(id="pt-aaaa1111"),
            self._job(id="pt-bbbb2222"),
        ]
        out = acp.filter_jobs(jobs, job_id="pt-aaaa1111")
        self.assertEqual([j["id"] for j in out], ["pt-aaaa1111"])

    def test_filter_by_user_matches_name_or_id(self):
        jobs = [
            self._job(id="pt-mine1", owner_user_name="L202500193",
                      owner_user_id="019c9e7c-c355-7581-9a16-d4eb79609e5b"),
            self._job(id="pt-other", owner_user_name="L202500249",
                      owner_user_id="019d6c7d-a8b7-7e78-8502-88af67932b92"),
            self._job(id="pt-mine2", owner_user_name="L202500193",
                      owner_user_id="019c9e7c-c355-7581-9a16-d4eb79609e5b"),
        ]
        by_name = acp.filter_jobs(jobs, user="L202500193")
        self.assertEqual({j["id"] for j in by_name}, {"pt-mine1", "pt-mine2"})
        by_id = acp.filter_jobs(jobs, user="019c9e7c-c355-7581-9a16-d4eb79609e5b")
        self.assertEqual({j["id"] for j in by_id}, {"pt-mine1", "pt-mine2"})


class JobListingTests(unittest.TestCase):
    def _raw_job(self, name: str, state: str, gpus: int = 8) -> dict:
        return {
            "name": name,
            "display_name": name,
            "state": state,
            "roles": [
                {
                    "resource_spec": [
                        {
                            "name": f"N6lS.Iu.I10.{gpus}.64c1024g",
                            "replicas": 1,
                            "limits": {"nvidia.com/gpu": str(gpus)},
                        }
                    ],
                    "total_replicas": 1,
                    "startup_script": "echo hi",
                }
            ],
        }

    def test_hmac_training_jobs_listing_follows_next_page_token(self):
        client = mock.Mock()
        client.get.side_effect = [
            {
                "training_jobs": [self._raw_job("pt-old1111", "SUCCEEDED")],
                "next_page_token": "page-2",
            },
            {
                "training_jobs": [self._raw_job("pt-run2222", "RUNNING")],
            },
        ]

        with mock.patch.object(acp, "HMACClient", return_value=client):
            jobs = acp.list_jobs_in_workspace("p1-video-world-model-for-robot-learning", page_size=1)

        self.assertEqual([j["id"] for j in jobs], ["pt-old1111", "pt-run2222"])
        self.assertEqual(jobs[1]["state"], "RUNNING")
        self.assertIn("page_token=page-2", client.get.call_args_list[1].args[0])

    def test_hmac_training_jobs_listing_retries_overflow_with_smaller_page(self):
        client = mock.Mock()
        client.get.side_effect = [
            acp.APIError("grpc: received message larger than max"),
            {"training_jobs": []},
        ]

        with mock.patch.object(acp, "HMACClient", return_value=client), \
             mock.patch.object(acp, "_sco_run") as sco_run:
            jobs = acp.list_jobs_in_workspace("p18-eacv", page_size=500)

        self.assertEqual(jobs, [])
        self.assertIn("page_size=500", client.get.call_args_list[0].args[0])
        self.assertIn("page_size=250", client.get.call_args_list[1].args[0])
        sco_run.assert_not_called()


class CommandListServerStateTests(unittest.TestCase):
    def test_cmd_list_pushes_only_single_effective_state_to_workspace_query(self):
        cases = [
            ("default", None, False, "RUNNING"),
            ("explicit-single", "pending", False, "PENDING"),
            ("all", None, True, None),
            ("explicit-multiple", "running,pending", False, None),
        ]

        for label, state, all_states, expected_server_state in cases:
            with self.subTest(label=label):
                args = argparse.Namespace(
                    workspace="p18-eacv",
                    all_users=False,
                    user="L202500193",
                    page_size=125,
                    state=state,
                    all=all_states,
                    since=None,
                    experiment=None,
                    id=None,
                    json=False,
                )
                with mock.patch.object(acp, "load_config", return_value={"identity": {}}), \
                     mock.patch.object(acp, "HMACClient"), \
                     mock.patch.object(
                         acp, "list_jobs_in_workspace", return_value=[]
                     ) as list_mock:
                    rc = acp.cmd_list(args)

                self.assertEqual(rc, 0)
                list_mock.assert_called_once_with(
                    "p18-eacv",
                    user_name="L202500193",
                    state=expected_server_state,
                    page_size=125,
                )

    def test_list_parser_accepts_page_size(self):
        with mock.patch.object(acp, "load_config", return_value={"identity": {}}), \
             mock.patch.object(acp, "HMACClient"), \
             mock.patch.object(acp, "list_jobs_in_workspace", return_value=[]) as list_mock:
            rc = acp.main([
                "list",
                "--workspace", "p18-eacv",
                "--user", "L202500193",
                "--page-size", "62",
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(list_mock.call_args.kwargs["page_size"], 62)


class IdentityPersistenceTests(unittest.TestCase):
    def test_save_identity_adds_block_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acp.toml"
            path.write_text("[defaults]\ncpus_per_gpu = 8\n", encoding="utf-8")
            acp.save_identity(path, "L202500193", "019c9e7c-c355-7581-9a16-d4eb79609e5b")
            text = path.read_text(encoding="utf-8")
        self.assertIn("[identity]", text)
        self.assertIn('user_name = "L202500193"', text)
        self.assertIn('user_id = "019c9e7c-c355-7581-9a16-d4eb79609e5b"', text)
        # Other sections preserved
        self.assertIn("cpus_per_gpu = 8", text)

    def test_save_identity_replaces_existing_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acp.toml"
            path.write_text(
                '[defaults]\ncpus_per_gpu = 8\n\n'
                '[identity]\nuser_name = "old"\nuser_id = "old-id"\n\n'
                '[afs_mount]\nmount_path = "/mnt/afs"\n',
                encoding="utf-8",
            )
            acp.save_identity(path, "L202500193", "019c9e7c-new-id")
            text = path.read_text(encoding="utf-8")
        self.assertNotIn('user_name = "old"', text)
        self.assertIn('user_name = "L202500193"', text)
        self.assertIn('user_id = "019c9e7c-new-id"', text)
        self.assertIn('mount_path = "/mnt/afs"', text)  # trailing section kept


class ListFormattingTests(unittest.TestCase):
    def _job(self, **overrides):
        base = {
            "id": "pt-abc12345",
            "name": "pt-abc12345",
            "display_name": "g8-ember-overfit",
            "state": "RUNNING",
            "age_sec": 3 * 3600 + 12 * 60,
            "gpus": 8,
            "cluster": "computing-cluster-01e",
            "workspace": "share-space-01e",
            "spec": "N6lS.Iu.I10.8.64c1024g",
        }
        base.update(overrides)
        return base

    def test_format_age_human_readable(self):
        self.assertEqual(acp.format_age(45), "45s")
        self.assertEqual(acp.format_age(12 * 60), "12m")
        self.assertEqual(acp.format_age(3 * 3600 + 12 * 60), "3h")
        self.assertEqual(acp.format_age(2 * 86400), "2d")

    def test_format_list_compact_has_header_and_row(self):
        text = acp.format_list_compact([self._job()])
        self.assertIn("ID", text)
        self.assertIn("STATE", text)
        self.assertIn("pt-abc12345", text)
        self.assertIn("RUNNING", text)
        self.assertIn("g8-ember-overfit", text)

    def test_format_list_compact_empty_has_guidance(self):
        text = acp.format_list_compact([])
        self.assertIn("no", text.lower())


# ---------------------------------------------------------------------------
# Phase 3: stop
# ---------------------------------------------------------------------------


class StopGateTests(unittest.TestCase):
    def test_stop_rejects_non_id_inputs(self):
        with mock.patch.object(acp.sys, "stderr"):
            rc = acp.cmd_stop_main(
                ["g8-ember-overfit"], client_factory=lambda: None,
                workspaces_provider=lambda: [], describe_fn=lambda *a, **k: None,
                stop_fn=lambda *a, **k: None, poll_fn=lambda *a, **k: "STOPPED",
            )
        self.assertNotEqual(rc, 0)

    def test_stop_idempotent_noop_for_already_terminal(self):
        workspaces = [{"name": "share-space-01e", "resource_id": "rid1"}]
        describe_calls = []

        def describe_fn(workspace, job_id, client):
            describe_calls.append((workspace["name"], job_id))
            return {"state": "SUCCEEDED", "workspace": workspace["name"]}

        stop_calls = []

        def stop_fn(workspace, job_id, client):
            stop_calls.append(job_id)

        rc = acp.cmd_stop_main(
            ["pt-abc12345"],
            client_factory=lambda: mock.Mock(),
            workspaces_provider=lambda: workspaces,
            describe_fn=describe_fn,
            stop_fn=stop_fn,
            poll_fn=lambda *a, **k: "SUCCEEDED",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(stop_calls, [])

    def test_stop_calls_api_and_polls_on_running_job(self):
        workspaces = [{"name": "share-space-01e", "resource_id": "rid1"}]
        stop_calls = []
        poll_states = iter(["STOPPED"])

        def describe_fn(workspace, job_id, client):
            return {"state": "RUNNING", "workspace": workspace["name"]}

        def stop_fn(workspace, job_id, client):
            stop_calls.append((workspace["name"], job_id))

        def poll_fn(workspace, job_id, client, timeout=30, interval=2):
            return next(poll_states)

        rc = acp.cmd_stop_main(
            ["pt-abc12345"],
            client_factory=lambda: mock.Mock(),
            workspaces_provider=lambda: workspaces,
            describe_fn=describe_fn,
            stop_fn=stop_fn,
            poll_fn=poll_fn,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(stop_calls, [("share-space-01e", "pt-abc12345")])

    def test_stop_returns_non_zero_when_job_not_found(self):
        workspaces = [{"name": "share-space-01e", "resource_id": "rid1"}]

        def describe_fn(workspace, job_id, client):
            raise acp.JobNotFound(job_id)

        rc = acp.cmd_stop_main(
            ["pt-missing"],
            client_factory=lambda: mock.Mock(),
            workspaces_provider=lambda: workspaces,
            describe_fn=describe_fn,
            stop_fn=lambda *a, **k: None,
            poll_fn=lambda *a, **k: "UNKNOWN",
        )
        self.assertNotEqual(rc, 0)


# ---------------------------------------------------------------------------
# Phase 3b: switch
# ---------------------------------------------------------------------------


class SwitchJobTests(unittest.TestCase):
    def _source_job(self, state: str = "RUNNING") -> dict:
        return {
            "name": "pt-oldjob1",
            "display_name": "old-job",
            "state": state,
            "workspace": "p1-video-world-model-for-robot-learning",
            "resource_pool": {"name": "computing-cluster-01e"},
            "roles": [
                {
                    "resource_spec": [
                        {
                            "name": "N6lS.Iu.I10.8.64c1024g",
                            "replicas": 1,
                            "limits": {"nvidia.com/gpu": "8"},
                        }
                    ],
                    "total_replicas": 1,
                    "startup_script": "bash /mnt/afs/old.sh",
                }
            ],
        }

    def test_extract_switch_source_preserves_old_resource_shape(self):
        source = acp.extract_switch_source(self._source_job())
        self.assertEqual(source["workspace"], "p1-video-world-model-for-robot-learning")
        self.assertEqual(source["cluster"], "computing-cluster-01e")
        self.assertEqual(source["spec_name"], "N6lS.Iu.I10.8.64c1024g")
        self.assertEqual(source["replicas"], 1)
        self.assertEqual(source["gpus"], 8)

    def test_switch_wait_submits_new_copy_before_stopping_old_job(self):
        workspaces = [{"name": "p1-video-world-model-for-robot-learning", "resource_id": "rid1"}]
        events = []
        copied_cmds = []

        def describe_fn(workspace, job_id, client):
            events.append(("describe", job_id))
            return self._source_job()

        def run_fn(cmd, **kwargs):
            copied_cmds.append((cmd, kwargs))
            events.append(("submit", cmd))
            return 0, "created pt-newjob1", ""

        def stop_fn(workspace, job_id, client):
            events.append(("stop", job_id))

        rc = acp.cmd_switch_main(
            [
                "pt-oldjob1",
                "--name", "g8-new-job",
                "--command", "bash /mnt/afs/new.sh",
                "--wait-interval", "5s",
                "--wait-timeout", "1h",
            ],
            client_factory=lambda: mock.Mock(),
            workspaces_provider=lambda: workspaces,
            describe_fn=describe_fn,
            stop_fn=stop_fn,
            poll_fn=lambda *a, **k: "SUSPENDED",
            submit_fn=run_fn,
            config_loader=lambda: acp.default_config(),
            bootstrap_fn=lambda: None,
        )

        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in events], ["describe", "submit", "stop"])
        cmd, kwargs = copied_cmds[0]
        self.assertIn("copy", cmd)
        self.assertIn("--copy-job-name=pt-oldjob1", cmd)
        self.assertIn("--workspace-name=p1-video-world-model-for-robot-learning", cmd)
        self.assertIn("--aec2-name=computing-cluster-01e", cmd)
        self.assertIn("--worker-spec=N6lS.Iu.I10.8.64c1024g", cmd)
        self.assertIn("--worker-nodes=1", cmd)
        self.assertIn("--command=bash /mnt/afs/new.sh", cmd)
        self.assertEqual(kwargs["quota_mode"], "wait")
        self.assertEqual(kwargs["wait_interval_sec"], 5)
        self.assertEqual(kwargs["wait_timeout_sec"], 3600)

    def test_switch_does_not_stop_old_job_when_new_submit_fails(self):
        workspaces = [{"name": "p1-video-world-model-for-robot-learning", "resource_id": "rid1"}]
        stop_calls = []

        rc = acp.cmd_switch_main(
            ["pt-oldjob1", "--name", "g8-new-job", "--command", "bash /mnt/afs/new.sh"],
            client_factory=lambda: mock.Mock(),
            workspaces_provider=lambda: workspaces,
            describe_fn=lambda *a, **k: self._source_job(),
            stop_fn=lambda workspace, job_id, client: stop_calls.append(job_id),
            poll_fn=lambda *a, **k: "SUSPENDED",
            submit_fn=lambda cmd, **kwargs: (1, "", "MEMBER_QUOTA_EXCEEDED"),
            config_loader=lambda: acp.default_config(),
            bootstrap_fn=lambda: None,
        )

        self.assertNotEqual(rc, 0)
        self.assertEqual(stop_calls, [])


# ---------------------------------------------------------------------------
# Phase 4: submit
# ---------------------------------------------------------------------------


class SpecSelectionForSubmitTests(unittest.TestCase):
    def _spec_catalog(self):
        return {
            "computing-cluster-01e": [
                {"name": "N6lS.Iu.I10.8.64c1024g", "gpu": 8, "cpu": 64, "mem_gb": 1024},
                {"name": "N6lS.Iu.I10.8.112c1664g", "gpu": 8, "cpu": 112, "mem_gb": 1664},
                {"name": "N6lS.Iu.I10.4.56c832g", "gpu": 4, "cpu": 56, "mem_gb": 832},
                {"name": "N6lS.Iu.I10.1", "gpu": 1, "cpu": 14, "mem_gb": 200},
            ],
            "computing-cluster-01e-hbxx": [
                {"name": "n11ls.Iu.8", "gpu": 8, "cpu": 80, "mem_gb": 2000},
            ],
        }

    def _usage(self):
        return {
            "computing-cluster-01e": {"idle_gpu": 24, "idle_cpu": 500, "idle_mem_gb": 8000},
            "computing-cluster-01e-hbxx": {"idle_gpu": 0, "idle_cpu": 0, "idle_mem_gb": 0},
        }

    def test_selects_single_node_spec_at_requested_gpu_count(self):
        plan = acp.plan_submission(
            requested_gpus=8,
            cpus_per_gpu=8,
            mem_per_gpu_gb=128,
            spec_catalog=self._spec_catalog(),
            cluster_usage=self._usage(),
        )
        self.assertEqual(plan["cluster"], "computing-cluster-01e")
        self.assertEqual(plan["spec_name"], "N6lS.Iu.I10.8.64c1024g")
        self.assertEqual(plan["replicas"], 1)
        self.assertEqual(plan["gpus_per_replica"], 8)

    def test_prefers_lighter_spec_at_same_gpu_count(self):
        plan = acp.plan_submission(
            requested_gpus=8,
            cpus_per_gpu=8,
            mem_per_gpu_gb=128,
            spec_catalog=self._spec_catalog(),
            cluster_usage=self._usage(),
        )
        # 64c1024g is lighter than 112c1664g at 8 GPU
        self.assertEqual(plan["spec_name"], "N6lS.Iu.I10.8.64c1024g")

    def test_falls_back_to_multi_replica_when_no_single_node_spec(self):
        catalog = {"computing-cluster-01e": [
            {"name": "N6lS.Iu.I10.4.56c832g", "gpu": 4, "cpu": 56, "mem_gb": 832},
        ]}
        usage = {"computing-cluster-01e": {"idle_gpu": 16, "idle_cpu": 500, "idle_mem_gb": 8000}}
        plan = acp.plan_submission(
            requested_gpus=16,
            cpus_per_gpu=14,
            mem_per_gpu_gb=200,
            spec_catalog=catalog,
            cluster_usage=usage,
        )
        self.assertEqual(plan["gpus_per_replica"], 4)
        self.assertEqual(plan["replicas"], 4)
        self.assertEqual(plan["total_gpus"], 16)

    def test_skips_clusters_without_enough_idle_gpus(self):
        usage = {
            "computing-cluster-01e": {"idle_gpu": 2, "idle_cpu": 500, "idle_mem_gb": 8000},
            "computing-cluster-01e-hbxx": {"idle_gpu": 100, "idle_cpu": 500, "idle_mem_gb": 8000},
        }
        plan = acp.plan_submission(
            requested_gpus=8,
            cpus_per_gpu=8,
            mem_per_gpu_gb=128,
            spec_catalog=self._spec_catalog(),
            cluster_usage=usage,
        )
        self.assertEqual(plan["cluster"], "computing-cluster-01e-hbxx")

    def test_returns_no_fit_reasons_when_nothing_fits(self):
        usage = {
            "computing-cluster-01e": {"idle_gpu": 2, "idle_cpu": 500, "idle_mem_gb": 8000},
            "computing-cluster-01e-hbxx": {"idle_gpu": 0, "idle_cpu": 0, "idle_mem_gb": 0},
        }
        with self.assertRaises(acp.NoSpecFit) as cm:
            acp.plan_submission(
                requested_gpus=8,
                cpus_per_gpu=14,
                mem_per_gpu_gb=200,
                spec_catalog=self._spec_catalog(),
                cluster_usage=usage,
            )
        msg = str(cm.exception)
        self.assertIn("computing-cluster-01e", msg)
        self.assertIn("idle", msg.lower())


class NCCLInjectionTests(unittest.TestCase):
    def test_single_replica_gets_no_nccl_overrides(self):
        env = acp.build_env_vars(replicas=1, forward_env=["WANDB_API_KEY"])
        keys = {e["key"] for e in env}
        self.assertNotIn("NCCL_NVLS_ENABLE", keys)
        self.assertIn("WANDB_API_KEY", keys)

    def test_wandb_api_key_falls_back_to_local_netrc(self):
        with tempfile.TemporaryDirectory() as tmp:
            netrc_path = Path(tmp) / ".netrc"
            netrc_path.write_text(
                "machine api.wandb.ai login user password netrc-token\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(acp.Path, "home", return_value=Path(tmp)):
                    env = acp.build_env_vars(replicas=1, forward_env=["WANDB_API_KEY"])
        kv = {e["key"]: e["value"] for e in env}
        self.assertEqual(kv["WANDB_API_KEY"], "netrc-token")

    def test_multi_replica_injects_nccl_overrides(self):
        env = acp.build_env_vars(replicas=2, forward_env=[])
        kv = {e["key"]: e["value"] for e in env}
        self.assertEqual(kv["NCCL_NVLS_ENABLE"], "0")
        self.assertEqual(kv["TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC"], "3600")
        self.assertEqual(kv["NCCL_SOCKET_TIMEOUT"], "3600000")
        # Must NOT set NCCL_P2P_DISABLE per CLAUDE.md
        self.assertNotIn("NCCL_P2P_DISABLE", kv)


class PreflightTests(unittest.TestCase):
    def test_preflight_detects_pip_install_in_shell_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "launch.sh"
            script.write_text(
                "#!/bin/bash\nset -euo pipefail\npip install torch\npython train.py\n",
                encoding="utf-8",
            )
            warnings = acp.preflight_check_command(f"bash {script}")
        self.assertTrue(any("pip install" in w for w in warnings))

    def test_preflight_passes_clean_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "launch.sh"
            script.write_text(
                "#!/bin/bash\nset -euo pipefail\npython train.py\n",
                encoding="utf-8",
            )
            warnings = acp.preflight_check_command(f"bash {script}")
        self.assertEqual(warnings, [])

    def test_preflight_skips_when_command_is_inline(self):
        warnings = acp.preflight_check_command("python -c 'print(1)'")
        self.assertEqual(warnings, [])


class SubmitWorkspaceDefaultTests(unittest.TestCase):
    def _args(self, workspace=None):
        return argparse.Namespace(
            command="echo hi",
            force=False,
            name="g1-default-workspace-probe",
            gpus=1,
            workspace=workspace,
            cpus_per_gpu=None,
            mem_per_gpu_gb=None,
            env=None,
            image=None,
            quota_mode="fail",
            wait_timeout="6h",
            wait_interval="10s",
            dry_run=True,
            console_path=None,
        )

    def _config(self):
        cfg = acp.default_config()
        cfg["defaults"]["workspace"] = "p18-eacv"
        cfg["workspace_quota"] = {"p18-eacv": 64}
        return cfg

    def test_submit_uses_config_default_workspace_before_cache_order(self):
        workspaces = [
            {"name": "p1-video-world-model-for-robot-learning", "clusters": ["computing-cluster-01e"]},
            {"name": "p18-eacv", "clusters": ["computing-cluster-01e"]},
        ]
        catalog = {"computing-cluster-01e": [
            {"name": "N6lS.Iu.I10.1.8c128g", "gpu": 1, "cpu": 8, "mem_gb": 128},
        ]}
        usage = {"computing-cluster-01e": {"idle_gpu": 8, "idle_cpu": 64, "idle_mem_gb": 1024}}

        with mock.patch("sensecore_cli.acp.load_config", return_value=self._config()), \
             mock.patch("sensecore_cli.acp.bootstrap_config"), \
             mock.patch("sensecore_cli.acp.HMACClient", return_value=mock.Mock()), \
             mock.patch("sensecore_cli.acp.discover_workspaces", return_value=workspaces), \
             mock.patch("sensecore_cli.acp.discover_spec_catalog", return_value=catalog), \
             mock.patch("sensecore_cli.acp.fetch_cluster_usage", return_value=usage), \
             mock.patch("sensecore_cli.acp.compute_workspace_quota_usage", return_value={
                 "available": 40,
                 "used": 0,
             }), \
             mock.patch("sys.stdout") as stdout:
            rc = acp.cmd_submit(self._args())

        self.assertEqual(rc, 0)
        output = "".join(call.args[0] + "\n" for call in stdout.write.call_args_list if call.args)
        self.assertIn("workspace=p18-eacv", output)

    def test_submit_explicit_accessible_workspace_overrides_config_default(self):
        workspaces = [
            {"name": "share-space-01e", "clusters": ["computing-cluster-01e"]},
            {"name": "p18-eacv", "clusters": ["computing-cluster-01e"]},
        ]
        catalog = {"computing-cluster-01e": [
            {"name": "N6lS.Iu.I10.1.8c128g", "gpu": 1, "cpu": 8, "mem_gb": 128},
        ]}
        usage = {"computing-cluster-01e": {"idle_gpu": 8, "idle_cpu": 64, "idle_mem_gb": 1024}}

        with mock.patch("sensecore_cli.acp.load_config", return_value=self._config()), \
             mock.patch("sensecore_cli.acp.bootstrap_config"), \
             mock.patch("sensecore_cli.acp.HMACClient", return_value=mock.Mock()), \
             mock.patch("sensecore_cli.acp.discover_workspaces", return_value=workspaces), \
             mock.patch("sensecore_cli.acp.discover_spec_catalog", return_value=catalog), \
             mock.patch("sensecore_cli.acp.fetch_cluster_usage", return_value=usage), \
             mock.patch("sys.stdout") as stdout:
            rc = acp.cmd_submit(self._args(workspace="share-space-01e"))

        self.assertEqual(rc, 0)
        output = "".join(call.args[0] + "\n" for call in stdout.write.call_args_list if call.args)
        self.assertIn("workspace=share-space-01e", output)

    def test_submit_rejects_retired_p1_before_discovery(self):
        workspaces = [
            {
                "name": "p1-video-world-model-for-robot-learning",
                "clusters": ["computing-cluster-01e"],
            },
        ]
        catalog = {"computing-cluster-01e": [
            {"name": "N6lS.Iu.I10.1.8c128g", "gpu": 1, "cpu": 8, "mem_gb": 128},
        ]}
        usage = {"computing-cluster-01e": {"idle_gpu": 8, "idle_cpu": 64, "idle_mem_gb": 1024}}

        with mock.patch("sensecore_cli.acp.load_config", return_value=self._config()), \
             mock.patch("sensecore_cli.acp.bootstrap_config"), \
             mock.patch("sensecore_cli.acp.HMACClient", return_value=mock.Mock()) as hmac_client, \
             mock.patch("sensecore_cli.acp.discover_workspaces", return_value=workspaces), \
             mock.patch("sensecore_cli.acp.discover_spec_catalog", return_value=catalog), \
             mock.patch("sensecore_cli.acp.fetch_cluster_usage", return_value=usage), \
             mock.patch("sys.stderr") as stderr:
            rc = acp.cmd_submit(
                self._args(workspace="p1-video-world-model-for-robot-learning")
            )

        self.assertEqual(rc, 2)
        hmac_client.assert_not_called()
        output = "".join(
            call.args[0] + "\n"
            for call in stderr.write.call_args_list
            if call.args
        )
        self.assertIn("no longer accessible", output)
        self.assertIn("p18-eacv", output)


class JobNameTopologyWarningTests(unittest.TestCase):
    def test_good_name_has_no_warning(self):
        self.assertIsNone(acp.validate_job_name_topology("g8-ember-overfit", gpus=8))
        self.assertIsNone(acp.validate_job_name_topology("g16-matcha-n8x2", gpus=16))

    def test_missing_topology_tag_returns_warning(self):
        warn = acp.validate_job_name_topology("ember-overfit-test", gpus=8)
        self.assertIsNotNone(warn)
        self.assertIn("g", warn.lower())


# ---------------------------------------------------------------------------
# Phase 5: logs shim
# ---------------------------------------------------------------------------


class LogsShimTests(unittest.TestCase):
    def test_logs_forwards_argv_to_sco_extract_logs(self):
        captured = {}

        def fake_main():
            captured["argv"] = sys.argv[:]

        with mock.patch("sensecore_cli.log_extract.main", side_effect=fake_main):
            acp.cmd_logs_main(["pt-abc12345", "--tail", "50"])
        self.assertEqual(captured["argv"][1:], ["pt-abc12345", "--tail", "50"])

    def test_logs_default_workspace_is_p18(self):
        captured = {}

        def fake_extract_logs(**kwargs):
            captured.update(kwargs)
            return {"total_workers": 1}, []

        with mock.patch("sensecore_cli.log_extract.extract_logs", side_effect=fake_extract_logs):
            acp.cmd_logs_main(["pt-abc12345"])
        self.assertEqual(captured["workspace"], "p18-eacv")

    def test_logs_tail_fetches_only_bounded_newest_pages_per_worker(self):
        info = {
            "uid": "abc123",
            "name": "pt-demo",
            "display_name": "demo",
            "state": "RUNNING",
            "create_time": "2026-05-04T00:00:00Z",
            "start_time": "2026-05-04T00:05:00Z",
            "complete_time": None,
            "total_workers": 2,
        }
        calls = []

        def hit(worker, second):
            return {
                "log_time": f"2026-05-04T00:00:{second:02d}Z",
                "severity_text": "INFO",
                "body": f"w{worker}-{second}",
                "attributes": {"k8s.pod.name": f"pt-abc123-worker-{worker}"},
            }

        def fake_query_logs_page(resource_id, start_cst, end_cst, filters, page_size, offset):
            pod_filter = next(val for key, val in filters if key == "Attributes.k8s.pod.name")
            worker = int(pod_filter.rsplit("-", 1)[1])
            calls.append((pod_filter, page_size, offset))
            newest = [hit(worker, 10 + worker), hit(worker, 8 + worker)]
            return {"total": "1000", "hits": newest}

        with mock.patch("sensecore_cli.log_extract.get_job_info", return_value=info), \
             mock.patch("sensecore_cli.log_extract.query_logs_page", side_effect=fake_query_logs_page):
            _, hits = log_extract.extract_logs(
                workspace="p18-eacv",
                job_name="pt-demo",
                tail=2,
            )

        self.assertEqual(
            calls,
            [
                ("pt-abc123-worker-0", 2, 0),
                ("pt-abc123-worker-1", 2, 0),
            ],
        )
        self.assertEqual([h["body"] for h in hits], ["w0-10", "w1-11"])


# ---------------------------------------------------------------------------
# Phase 6: quota-aware submit (fail/wait/spot) + workspace-quota math
# ---------------------------------------------------------------------------


class ParseDurationTests(unittest.TestCase):
    def test_plain_int_is_seconds(self):
        self.assertEqual(acp.parse_duration("30"), 30)

    def test_unit_suffixes_map_to_seconds(self):
        self.assertEqual(acp.parse_duration("30s"), 30)
        self.assertEqual(acp.parse_duration("30m"), 30 * 60)
        self.assertEqual(acp.parse_duration("2h"), 2 * 3600)
        self.assertEqual(acp.parse_duration("1d"), 86400)
        self.assertEqual(acp.parse_duration("1w"), 7 * 86400)

    def test_rejects_bad_format(self):
        with self.assertRaises(ValueError):
            acp.parse_duration("soon")
        with self.assertRaises(ValueError):
            acp.parse_duration("3y")
        with self.assertRaises(ValueError):
            acp.parse_duration("")


class QuotaErrorDetectionTests(unittest.TestCase):
    def test_matches_member_quota_exceeded_token(self):
        self.assertTrue(acp.is_quota_error(
            "STATUS 409\n{\"error\":\"MEMBER_QUOTA_EXCEEDED: 32 GPU requested\"}"
        ))

    def test_matches_free_form_quota_exceeded(self):
        self.assertTrue(acp.is_quota_error("workspace quota exceeded"))
        self.assertTrue(acp.is_quota_error("insufficient quota remaining"))
        self.assertTrue(acp.is_quota_error("this job exceeds quota of 96"))

    def test_ignores_unrelated_errors(self):
        self.assertFalse(acp.is_quota_error("invalid worker-spec name"))
        self.assertFalse(acp.is_quota_error("image pull failed: not found"))
        self.assertFalse(acp.is_quota_error(""))


class QuotaErrorHintTests(unittest.TestCase):
    def test_hint_names_all_three_modes(self):
        hint = acp.format_quota_error_hint("MEMBER_QUOTA_EXCEEDED: need 32")
        lower = hint.lower()
        self.assertIn("--quota-mode=wait", lower)
        self.assertIn("--quota-mode=spot", lower)
        self.assertIn("--quota-mode=fail", lower)
        self.assertIn("member_quota_exceeded", lower)
        # Points users to the quota subcommand for a check-first workflow.
        self.assertIn("acp quota", lower)


class RunScoCreateWithQuotaModeTests(unittest.TestCase):
    def test_fail_mode_returns_immediately_on_quota_error(self):
        calls: list[list[str]] = []

        def run_fn(cmd):
            calls.append(cmd)
            return 1, "", "STATUS 409 MEMBER_QUOTA_EXCEEDED"

        rc, stdout, stderr = acp.run_sco_create_with_quota_mode(
            ["sco", "acp", "jobs", "create"],
            quota_mode="fail",
            wait_timeout_sec=3600,
            wait_interval_sec=60,
            run_fn=run_fn,
            sleep_fn=lambda s: self.fail(f"fail mode should not sleep (got {s}s)"),
        )
        self.assertEqual(rc, 1)
        self.assertIn("QUOTA_EXCEEDED", stderr)
        self.assertEqual(len(calls), 1)

    def test_spot_mode_runs_once_no_retry(self):
        # Caller appends --quota-type=spot; helper just executes under spot mode
        # with no retry loop.
        calls: list[list[str]] = []

        def run_fn(cmd):
            calls.append(cmd)
            return 0, "created pt-abc12345", ""

        rc, stdout, _ = acp.run_sco_create_with_quota_mode(
            ["sco", "acp", "jobs", "create", "--quota-type=spot"],
            quota_mode="spot",
            wait_timeout_sec=0,
            wait_interval_sec=0,
            run_fn=run_fn,
            sleep_fn=lambda s: self.fail("spot mode should not sleep"),
        )
        self.assertEqual(rc, 0)
        self.assertIn("pt-abc12345", stdout)
        self.assertEqual(len(calls), 1)
        self.assertIn("--quota-type=spot", calls[0])

    def test_wait_mode_appends_native_wait_and_runs_once(self):
        calls: list[list[str]] = []

        rc, stdout, _ = acp.run_sco_create_with_quota_mode(
            ["sco", "acp", "jobs", "create"],
            quota_mode="wait",
            wait_timeout_sec=60,
            wait_interval_sec=1,
            run_fn=lambda cmd: (calls.append(cmd.copy()) or (0, "created pt-xyz78901", "")),
            sleep_fn=lambda s: self.fail("native wait mode should not sleep"),
            monotonic_fn=lambda: self.fail("native wait mode should not poll time"),
            log_fn=lambda m: None,
        )
        self.assertEqual(rc, 0)
        self.assertIn("pt-xyz78901", stdout)
        self.assertEqual(len(calls), 1)
        self.assertIn("--wait", calls[0])

    def test_wait_mode_returns_native_quota_error_without_client_retry(self):
        calls: list[list[str]] = []

        rc, _, stderr = acp.run_sco_create_with_quota_mode(
            ["sco", "acp", "jobs", "create"],
            quota_mode="wait",
            wait_timeout_sec=10,
            wait_interval_sec=5,
            run_fn=lambda cmd: (calls.append(cmd.copy()) or (1, "", "MEMBER_QUOTA_EXCEEDED native response")),
            sleep_fn=lambda s: self.fail("native wait mode should not sleep"),
            monotonic_fn=lambda: self.fail("native wait mode should not poll time"),
            log_fn=lambda m: None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("QUOTA_EXCEEDED", stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("--wait", calls[0])

    def test_wait_mode_propagates_non_quota_errors_without_retry(self):
        calls = [0]

        def run_fn(cmd):
            calls[0] += 1
            return 1, "", "sco: invalid worker-spec"

        rc, _, stderr = acp.run_sco_create_with_quota_mode(
            ["sco", "acp", "jobs", "create"],
            quota_mode="wait",
            wait_timeout_sec=60,
            wait_interval_sec=1,
            run_fn=run_fn,
            sleep_fn=lambda s: self.fail("non-quota errors must not trigger sleep"),
            monotonic_fn=lambda: 0.0,
            log_fn=lambda m: None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("invalid worker-spec", stderr)
        self.assertEqual(calls[0], 1)

    def test_wait_mode_preserves_existing_wait_flag(self):
        calls: list[list[str]] = []

        rc, stdout, _ = acp.run_sco_create_with_quota_mode(
            ["sco", "acp", "jobs", "create", "--wait"],
            quota_mode="wait",
            wait_timeout_sec=60,
            wait_interval_sec=1,
            run_fn=lambda cmd: (calls.append(cmd.copy()) or (0, "created pt-abc99999", "")),
            sleep_fn=lambda s: self.fail("native wait mode should not sleep"),
            monotonic_fn=lambda: self.fail("native wait mode should not poll time"),
            log_fn=lambda m: None,
        )
        self.assertEqual(rc, 0)
        self.assertIn("pt-abc99999", stdout)
        self.assertEqual(calls[0].count("--wait"), 1)

    def test_wait_mode_keeps_wait_flag_on_command_for_error_reporting(self):
        cmd = ["sco", "acp", "jobs", "create"]

        rc, _, stderr = acp.run_sco_create_with_quota_mode(
            cmd,
            quota_mode="wait",
            wait_timeout_sec=60,
            wait_interval_sec=1,
            run_fn=lambda c: (1, "", "unknown flag: --wait"),
            sleep_fn=lambda s: self.fail("native wait mode should not sleep"),
            monotonic_fn=lambda: self.fail("native wait mode should not poll time"),
            log_fn=lambda m: None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("unknown flag", stderr)
        self.assertIn("--wait", cmd)


class FormatScoFailureTests(unittest.TestCase):
    def test_dump_includes_rc_both_streams_and_cmd(self):
        out = acp.format_sco_failure(
            ["sco", "acp", "jobs", "create", "--workspace-name=ws"],
            rc=1,
            stdout="server says: validation failed",
            stderr='Error: component "acp" exited with error',
        )
        self.assertIn("rc=1", out)
        self.assertIn("validation failed", out)
        self.assertIn("component \"acp\" exited", out)
        self.assertIn("--workspace-name=ws", out)

    def test_dump_truncates_long_command_value(self):
        big = "bash -c '" + "x" * 500 + "'"
        out = acp.format_sco_failure(
            ["sco", "acp", "jobs", "create", f"--command={big}"],
            rc=1, stdout="", stderr="boom",
        )
        # Truncated marker present, full payload not.
        self.assertIn("...", out)
        self.assertNotIn("x" * 500, out)

    def test_dump_handles_empty_streams(self):
        out = acp.format_sco_failure(["sco"], rc=2, stdout="", stderr="")
        self.assertIn("(empty)", out)

    def test_dump_hints_sco_upgrade_when_native_wait_flag_is_unknown(self):
        out = acp.format_sco_failure(
            ["sco", "acp", "jobs", "create", "--wait"],
            rc=1,
            stdout="",
            stderr="unknown flag: --wait",
        )
        self.assertIn("sco components upgrade", out)


class WorkspaceQuotaComputationTests(unittest.TestCase):
    def _job(self, state: str, gpus: int, quota_type: str = "RESERVED") -> dict:
        return {"state": state, "gpus": gpus, "quota_type": quota_type}

    def _cci_app(self, state: str, gpus: int, quota_type: str = "RESERVED") -> dict:
        return {"state": state, "gpus": gpus, "quota_type": quota_type}

    def test_only_running_counts_against_cap(self):
        jobs = [
            self._job("RUNNING", 32),
            self._job("RUNNING", 16),
            self._job("SUSPENDED", 8),   # does NOT consume quota
            self._job("PENDING", 16),    # does NOT consume quota
            self._job("SUCCEEDED", 64),  # does NOT consume quota
        ]
        snap = acp.compute_workspace_quota_usage(
            "p1-video-world-model-for-robot-learning",
            quota_cap=96,
            list_fn=lambda ws: jobs,
        )
        self.assertEqual(snap["used"], 48)
        self.assertEqual(snap["cap"], 96)
        self.assertEqual(snap["available"], 48)
        self.assertEqual(snap["running_jobs"], 2)

    def test_running_cci_apps_count_against_cap(self):
        jobs = [self._job("RUNNING", 32)]
        apps = [
            self._cci_app("RUNNING", 8),
            self._cci_app("SUSPENDED", 16),
        ]
        snap = acp.compute_workspace_quota_usage(
            "p1-video-world-model-for-robot-learning",
            quota_cap=96,
            list_fn=lambda ws: jobs,
            cci_list_fn=lambda ws: apps,
        )
        self.assertEqual(snap["used"], 40)
        self.assertEqual(snap["available"], 56)
        self.assertEqual(snap["running_jobs"], 1)
        self.assertEqual(snap["running_cci_apps"], 1)

    def test_running_spot_resources_do_not_count_against_reserved_cap(self):
        jobs = [
            self._job("RUNNING", 32),
            self._job("RUNNING", 3, quota_type="SPOT"),
        ]
        apps = [
            self._cci_app("RUNNING", 8),
            self._cci_app("RUNNING", 2, quota_type="SPOT"),
        ]
        snap = acp.compute_workspace_quota_usage(
            "p1-video-world-model-for-robot-learning",
            quota_cap=96,
            list_fn=lambda ws: jobs,
            cci_list_fn=lambda ws: apps,
        )
        self.assertEqual(snap["used"], 40)
        self.assertEqual(snap["job_used"], 32)
        self.assertEqual(snap["cci_used"], 8)
        self.assertEqual(snap["spot_job_used"], 3)
        self.assertEqual(snap["spot_cci_used"], 2)
        self.assertEqual(snap["spot_excluded"], 5)
        self.assertEqual(snap["available"], 56)

    def test_normalize_job_preserves_scheduling_quota_type(self):
        raw = {
            "name": "pt-spot",
            "display_name": "spot-job",
            "state": "RUNNING",
            "scheduling": {"quota_type": "SPOT"},
            "roles": [
                {
                    "resource_spec": [
                        {
                            "name": "N6lS.Iu.I10.1",
                            "replicas": 1,
                            "limits": {"nvidia.com/gpu": "1"},
                            "requests": {"nvidia.com/gpu": "1"},
                        }
                    ],
                    "total_replicas": 1,
                    "startup_script": "echo hi",
                }
            ],
        }
        job = acp._normalize_job(raw, "ws")
        self.assertEqual(job["quota_type"], "SPOT")

    def test_normalize_cci_app_preserves_scheduling_quota_type(self):
        raw = {
            "name": "app-spot",
            "display_name": "spot-app",
            "state": "RUNNING",
            "scheduling": {"quota_type": "SPOT"},
            "template": {
                "containers": [
                    {"resource_request": {"nvidia.com/gpu": "2"}}
                ],
                "resource_spec": {"name": "gpu-2"},
            },
            "replicas": 1,
        }
        app = acp._normalize_cci_app(raw, "ws")
        self.assertEqual(app["quota_type"], "SPOT")

    def test_available_never_goes_negative(self):
        # Quota can be exceeded temporarily when a prior admission stacked past
        # the cap — report available as 0, not a negative number.
        jobs = [self._job("RUNNING", 120)]
        snap = acp.compute_workspace_quota_usage(
            "ws", quota_cap=96, list_fn=lambda ws: jobs,
        )
        self.assertEqual(snap["used"], 120)
        self.assertEqual(snap["available"], 0)

    def test_zero_cap_reports_available_as_none(self):
        jobs = [self._job("RUNNING", 16)]
        snap = acp.compute_workspace_quota_usage(
            "ws", quota_cap=0, list_fn=lambda ws: jobs,
        )
        self.assertIsNone(snap["available"])
        self.assertEqual(snap["used"], 16)


class BootstrapConfigQuotaTests(unittest.TestCase):
    def test_bootstrap_writes_workspace_quota_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acp.toml"
            acp.bootstrap_config(path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("[workspace_quota]", text)
        self.assertIn('workspace = "p18-eacv"', text)
        self.assertIn('"p18-eacv" = 64', text)
        self.assertNotIn('"p1-video-world-model-for-robot-learning"', text)


def test_log_extractor_excludes_retired_p1() -> None:
    assert "p18-eacv" in log_extract.WORKSPACE_RESOURCE_IDS
    assert "p1-video-world-model-for-robot-learning" not in log_extract.WORKSPACE_RESOURCE_IDS


if __name__ == "__main__":
    unittest.main()
