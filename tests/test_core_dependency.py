"""The MCP server core is a dependency (opencontext-core), not a copy.

These check the seam between that package and this repo: the core must be
importable, must find this repo's plugins, and must be pinned to the same
version everywhere it is declared.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"opencontext-core @ git\+https://\S+?@(v[\d.]+)")


def test_core_is_not_vendored():
    """A stray core/ or server/ directory here would shadow the package
    and silently pin this deployment to whatever was copied in."""
    assert not (REPO_ROOT / "core").exists()
    assert not (REPO_ROOT / "server").exists()


def test_core_and_lambda_handler_import():
    from server.adapters.aws_lambda import lambda_handler

    assert callable(lambda_handler)


def test_core_discovers_this_repos_plugins(monkeypatch):
    """The installed core must find the plugins in THIS repo. config.yaml
    is gitignored here (CI has none), so the check is against the plugin
    directories themselves, plus the enabled plugin when a config exists."""
    from core.plugin_manager import PluginManager
    from core.validators import get_enabled_plugin_config

    monkeypatch.chdir(REPO_ROOT)
    on_disk = {
        path.parent.name
        for path in (REPO_ROOT / "plugins").glob("*/plugin.py")
        if not path.parent.name.startswith("_")
    }
    discovered = dict(PluginManager({"plugins": {}}).discover_plugins())

    assert on_disk and on_disk <= set(discovered)
    for name in on_disk:
        assert discovered[name].parent == REPO_ROOT / "plugins"

    config_path = REPO_ROOT / "config.yaml"
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text("utf-8"))
        enabled, _ = get_enabled_plugin_config(config)
        assert enabled in discovered


def test_core_pin_is_the_same_everywhere():
    pins = {
        name: PIN.findall((REPO_ROOT / name).read_text("utf-8"))
        for name in ("requirements.txt", "pyproject.toml")
    }
    assert all(len(found) == 1 for found in pins.values()), pins
    assert len({found[0] for found in pins.values()}) == 1, pins
