"""Guard: the package version and the plugin manifest version must agree.

The release version lives in two places that no tool keeps in lockstep:

  * ``pyproject.toml``                          — Python packaging
  * ``plugins/callstack/.claude-plugin/plugin.json`` — Claude Code plugin

They drifted once (pyproject lagged at 0.1.1 while the plugin and the git
tags were already at 0.2.0). This test fails loudly the moment they
diverge again, so a release can't ship two different version strings.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_PLUGIN_MANIFEST = _ROOT / "plugins" / "callstack" / ".claude-plugin" / "plugin.json"


def _pyproject_version() -> str:
    with _PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def _plugin_version() -> str:
    return json.loads(_PLUGIN_MANIFEST.read_text())["version"]


def test_package_and_plugin_versions_match():
    pyproject = _pyproject_version()
    plugin = _plugin_version()
    assert pyproject == plugin, (
        f"version drift: pyproject.toml={pyproject!r} but "
        f"{_PLUGIN_MANIFEST.name}={plugin!r}. Bump both together when releasing."
    )
