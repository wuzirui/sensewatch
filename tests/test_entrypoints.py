"""Packaging and command-entry-point smoke tests."""

from importlib import import_module
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    ("relative_path", "required_text"),
    [
        ("README.md", "pipx install"),
        ("config.example.toml", "[defaults]"),
        ("docs/setup.md", "sco components install"),
        ("docs/configuration.md", "~/.config/dreamdojo/acp.toml"),
        ("docs/acp.md", "acp submit"),
        ("docs/cci.md", "cci doctor"),
        ("docs/troubleshooting.md", "sco components upgrade"),
    ],
)
def test_operator_documentation_is_present(
    relative_path: str,
    required_text: str,
) -> None:
    content = (ROOT / relative_path).read_text(encoding="utf-8")
    assert required_text in content


def test_readme_relative_markdown_links_resolve() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)#]+\.md)(?:#[^)]+)?\)", readme)
    assert targets
    for target in targets:
        assert (ROOT / target).is_file(), target
