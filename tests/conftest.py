"""Test bootstrap: import the script as `strip_images` and isolate every test.

The script is a single stdlib-only file, not a package, so it is reached through
sys.path. Every test runs against a throwaway HOME / CLAUDE_CONFIG_DIR under
tmp_path; the module globals computed at import time are patched to match, so no
test can resolve the real ~/.claude.
"""

import sys
import types
from pathlib import Path

import pytest

_SCRIPTS = str(
    Path(__file__).resolve().parent.parent / "skills" / "transcript-image-cleanup" / "scripts"
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import strip_images


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = home / ".claude"
    projects = cfg / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(strip_images, "HOME", str(home))
    monkeypatch.setattr(strip_images, "CONFIG_DIR", str(cfg))
    monkeypatch.setattr(strip_images, "PROJECTS", str(projects))
    return types.SimpleNamespace(
        root=tmp_path, home=home, cfg=cfg, projects=projects, backup=tmp_path / "backup"
    )
