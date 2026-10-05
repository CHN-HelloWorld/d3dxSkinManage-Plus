import json
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def sample_plugins(temp_data_dir):
    source = Path(__file__).resolve().parents[2] / "plugins"
    target = temp_data_dir / "plugins"
    for plugin_id in ("example.mod-inventory", "example.preset-audit", "example.preset-cycle"):
        shutil.copytree(source / plugin_id, target / plugin_id)
    return target


@pytest.fixture
def trusted_sample_plugins(sample_plugins, temp_data_dir):
    state = {}
    for manifest in sample_plugins.glob("*/plugin.json"):
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        state[metadata["id"]] = {
            "enabled": True,
            "trusted": True,
            "granted_permissions": metadata["permissions"],
        }
    (temp_data_dir / "config/plugin_state.json").write_text(json.dumps(state), encoding="utf-8")
    return sample_plugins
