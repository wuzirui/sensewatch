"""Packaging and command-entry-point smoke tests."""

from importlib import import_module


def test_public_modules_import() -> None:
    assert import_module("sensecore_cli.acp")
    assert import_module("sensecore_cli.cci")
