"""Packaging and command-entry-point smoke tests."""

from importlib import import_module

import pytest


def test_public_modules_import() -> None:
    assert import_module("sensecore_cli.acp")
    assert import_module("sensecore_cli.cci")


@pytest.mark.parametrize(
    "module_name",
    ["sensecore_cli.hmac_request", "sensecore_cli.log_extract"],
)
def test_helper_modules_render_help_without_external_io(
    module_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = import_module(module_name)
    with pytest.raises(SystemExit) as exc_info:
        module.main(["--help"])
    assert exc_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
