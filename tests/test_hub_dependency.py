"""The shared ArcGIS package is a dependency (opencontext-arcgis-hub).

Same seam checks as test_core_dependency.py: the package must be
importable, must not be shadowed by a copy in this repo, and must be
pinned to the same version everywhere it is declared.

This deployment uses one module of it, ``arcgis_hub.where_validator``.
The plugin is not an ``ArcGISHubPlugin`` subclass.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"opencontext-arcgis-hub @ git\+https://\S+?@(v[\d.]+)")


def test_hub_is_not_vendored():
    """A stray arcgis_hub/ directory here would shadow the package, and
    the old in-repo copies of the validators must not come back."""
    assert not (REPO_ROOT / "arcgis_hub").exists()
    assert not (REPO_ROOT / "plugins" / "arcgis").exists()
    assert not (REPO_ROOT / "plugins" / "anchorage_gis").exists()


def test_plugin_uses_the_hub_validators():
    import arcgis_hub.where_validator as shared
    from plugins.anchorage_parcels import plugin

    assert plugin.WhereValidator is shared.WhereValidator
    assert plugin.OutFieldsValidator is shared.OutFieldsValidator
    assert plugin.OrderByValidator is shared.OrderByValidator


def test_plugin_does_not_load_the_hub_plugin():
    """Only the validators are used. Importing arcgis_hub.plugin would
    pull in pyclipper, which requirements.txt deliberately does not list:
    a package built by deploy.sh's pip path has none, and the Lambda
    would fail to load its one plugin."""
    code = (
        "import sys, plugins.anchorage_parcels.plugin; "
        "bad = {'arcgis_hub.plugin', 'pyclipper'} & set(sys.modules); "
        "sys.exit(f'imported: {sorted(bad)}' if bad else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


def test_installed_hub_matches_the_pin():
    """A stale environment would otherwise test one hub and ship
    another: the pin is what deploy.sh installs."""
    import importlib.metadata

    pinned = PIN.findall((REPO_ROOT / "requirements.txt").read_text("utf-8"))
    assert ["v" + importlib.metadata.version("opencontext-arcgis-hub")] == pinned


def test_hub_pin_is_the_same_everywhere():
    pins = {
        name: PIN.findall((REPO_ROOT / name).read_text("utf-8"))
        for name in ("requirements.txt", "pyproject.toml")
    }
    assert all(len(found) == 1 for found in pins.values()), pins
    assert len({found[0] for found in pins.values()}) == 1, pins
