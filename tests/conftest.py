"""A config for the tests to run against.

fleet ships no default coordinates, so every test needs one. Rather than each test
building its own, one fixture writes a config file and points `$FLEET_CONFIG` at it for
the whole session — which also means the example config is exercised in shape on every
run, not just read by people.

⚠ Autouse and function-scoped, and it resets the module cache. The config is cached after
the first read, so a test that changes it and a test that does not would otherwise see
whichever one ran first.
"""

from __future__ import annotations

import pytest

from fleet import config

CONFIG = """
[project]
repo = "owner/app"
workspace = "{ws}"
code_repo = "{ws}"
prod_host = "prod"
prod_containers = ["app-web-1", "app-api-1"]
build_workflow = "images"
deploy_workflow = "deploy"

[runners.agy]
enabled = true
per_account = 2
floor = 0.10

[runners.claude]
enabled = true
concurrent = 2
floor = 0.25

[runners.cursor]
enabled = true
concurrent = 1
floor = 0.05
"""


@pytest.fixture(autouse=True)
def fleet_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(ws=tmp_path.as_posix()), encoding="utf-8")
    monkeypatch.setenv("FLEET_CONFIG", str(path))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    config.reset()
    yield path
    config.reset()
