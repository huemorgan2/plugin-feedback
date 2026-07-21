"""Version stamps must agree (pyproject / luna-plugin.toml / manifest) and the
toml tool list must match what the code registers."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_version_stamps_agree():
    pyproject = _toml(ROOT / "pyproject.toml")["project"]["version"]
    plugin_toml = _toml(ROOT / "plugin_feedback" / "luna-plugin.toml")["version"]

    from plugin_feedback import FeedbackPlugin

    manifest = FeedbackPlugin.manifest.version
    assert pyproject == plugin_toml == manifest


def test_toml_tools_match_code():
    plugin_toml = _toml(ROOT / "plugin_feedback" / "luna-plugin.toml")
    toml_names = {t["name"] for t in plugin_toml["tools"]}
    assert toml_names == {
        "feedback_ticket_send", "feedback_ticket_list", "feedback_ticket_get", "feedback_ticket_reply",
        "report_issue",
    }
    assert plugin_toml["requires"]["tools"] == len(toml_names)


def test_manifest_declares_pane():
    from plugin_feedback import FeedbackPlugin

    m = FeedbackPlugin.manifest
    assert m.routes_module == "routes"
    assert any(s.id == "feedback" for s in m.sidebar_sections)
