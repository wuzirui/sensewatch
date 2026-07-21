"""Contract tests for the packaged CCI lifecycle command."""

from pathlib import Path
from unittest import mock

import pytest

from sensecore_cli import cci


def _app(
    name: str,
    display_name: str,
    state: str = "RUNNING",
    *,
    user_name: str = "L202500193",
    user_id: str = "user-1",
) -> dict:
    return {
        "name": name,
        "uid": f"uid-{name}",
        "display_name": display_name,
        "state": state,
        "_workspace": "p18-eacv",
        "ownership": {"user_name": user_name, "user_id": user_id},
    }


def test_packaged_cci_has_no_dreamdojo_repo_paths() -> None:
    source = Path(cci.__file__).read_text(encoding="utf-8")
    assert cci.HMAC_HELPER.parent == Path(cci.__file__).resolve().parent
    assert "skills/sensecore-api-workflows" not in source


def test_filter_apps_combines_identity_and_state_filters() -> None:
    apps = [
        _app("app-mine-running", "mine", "RUNNING"),
        _app("app-mine-stopped", "mine-old", "SUSPENDED"),
        _app("app-other-running", "other", "RUNNING", user_name="L202500249"),
    ]
    selected = cci.filter_apps(apps, user_name="L202500193", states=["RUNNING"])
    assert [app["name"] for app in selected] == ["app-mine-running"]


def test_find_one_prefers_exact_internal_name_over_display_name() -> None:
    apps = [
        _app("app-exact", "friendly"),
        _app("app-other", "app-exact"),
    ]
    assert cci.find_one(apps, "app-exact")["display_name"] == "friendly"


def test_find_one_rejects_ambiguous_display_name() -> None:
    apps = [_app("app-a", "shared"), _app("app-b", "shared")]
    with pytest.raises(SystemExit) as exc_info:
        cci.find_one(apps, "shared")
    assert exc_info.value.code == 2


def test_start_is_idempotent_for_running_app() -> None:
    args = cci.build_parser().parse_args(["start", "app-running"])
    with mock.patch.object(cci, "locate", return_value=_app("app-running", "running")), \
         mock.patch.object(cci, "do_start") as do_start:
        assert cci.cmd_start(args) == 0
    do_start.assert_not_called()


def test_stop_is_idempotent_for_suspended_app() -> None:
    args = cci.build_parser().parse_args(["stop", "app-stopped"])
    app = _app("app-stopped", "stopped", state="SUSPENDED")
    with mock.patch.object(cci, "locate", return_value=app), \
         mock.patch.object(cci, "do_stop") as do_stop:
        assert cci.cmd_stop(args) == 0
    do_stop.assert_not_called()


def test_restart_refuses_transitional_state_without_force() -> None:
    args = cci.build_parser().parse_args(["restart", "app-starting"])
    app = _app("app-starting", "starting", state="STARTING")
    with mock.patch.object(cci, "locate", return_value=app), \
         mock.patch.object(cci, "do_stop") as do_stop, \
         mock.patch.object(cci, "do_start") as do_start:
        assert cci.cmd_restart(args) == 1
    do_stop.assert_not_called()
    do_start.assert_not_called()


def test_find_current_binding_matches_app_uid() -> None:
    rules = [
        {"name": "ssh-other", "properties": {"internal_instance_name": "uid-other"}},
        {
            "name": "ssh-target",
            "properties": {
                "internal_instance_name": "uid-target",
                "external_port": 22022,
                "internal_port": 22,
                "protocol": "tcp",
            },
        },
    ]
    assert cci.find_current_binding(rules, "uid-target")["name"] == "ssh-target"
    assert cci.find_current_binding(rules, "uid-missing") is None


def test_parser_exposes_all_public_subcommands() -> None:
    parser = cci.build_parser()
    action = next(a for a in parser._actions if getattr(a, "choices", None))
    assert set(action.choices) == {
        "list",
        "status",
        "start",
        "stop",
        "restart",
        "doctor",
    }
