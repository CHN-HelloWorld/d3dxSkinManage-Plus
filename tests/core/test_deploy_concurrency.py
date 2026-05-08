from __future__ import annotations

import json

import pytest

from src.core.config import Config


@pytest.fixture
def hardware(monkeypatch):
    from src.core import deploy_concurrency

    deploy_concurrency.recommended_deploy_concurrency.cache_clear()

    def configure(cpus, memory_gib):
        deploy_concurrency.recommended_deploy_concurrency.cache_clear()
        monkeypatch.setattr(deploy_concurrency.os, "cpu_count", lambda: cpus)
        memory = None if memory_gib is None else memory_gib * 1024**3
        monkeypatch.setattr(deploy_concurrency, "_total_memory_bytes", lambda: memory)

    yield configure
    deploy_concurrency.recommended_deploy_concurrency.cache_clear()


@pytest.mark.parametrize(
    ("cpus", "memory_gib", "expected"),
    [
        (1, 4, 1),
        (4, 8, 2),
        (8, 16, 4),
        (12, 32, 6),
        (32, 64, 8),
        (32, 4, 1),
        (32, 8, 2),
        (None, None, 1),
        (16, None, 2),
    ],
)
def test_auto_limit_respects_cpu_memory_and_missing_probe(hardware, cpus, memory_gib, expected):
    from src.core.deploy_concurrency import effective_deploy_concurrency

    hardware(cpus, memory_gib)
    assert effective_deploy_concurrency(0) == expected


@pytest.mark.parametrize("value", [1, 12, 16])
def test_manual_limit_can_exceed_device_recommendation(hardware, value):
    from src.core.deploy_concurrency import effective_deploy_concurrency

    hardware(2, 4)
    assert effective_deploy_concurrency(value) == value


def test_missing_setting_uses_auto_without_rewriting_existing_preferences(temp_data_dir, hardware):
    from src.core.deploy_concurrency import effective_deploy_concurrency

    hardware(8, 16)
    path = temp_data_dir / "config" / "settings.json"
    original = '{"general": {"theme": "light"}, "mods": {"delete_method": "recycle_bin"}}'
    path.write_text(original, encoding="utf-8")

    config = Config()
    assert effective_deploy_concurrency(config.get("mods.deploy_concurrency")) == 4
    assert config.get("mods.deploy_concurrency") == 0
    assert config.get("general.theme") == "light"
    assert config.get("mods.delete_method") == "recycle_bin"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("value", [-1, True, "4", 1.5, None, 17, 1024, 2**31])
def test_invalid_saved_limit_falls_back_to_auto_only(temp_data_dir, hardware, value):
    from src.core.deploy_concurrency import effective_deploy_concurrency

    hardware(8, 16)
    path = temp_data_dir / "config" / "settings.json"
    path.write_text(json.dumps({"mods": {"deploy_concurrency": value, "delete_method": "recycle_bin"}}))

    config = Config()
    assert config.get("mods.deploy_concurrency") == 0
    assert effective_deploy_concurrency(value) == 4
    assert config.get("mods.delete_method") == "recycle_bin"


def test_manual_value_survives_reload_and_reset_returns_to_auto(temp_data_dir, hardware):
    from src.core.deploy_concurrency import effective_deploy_concurrency

    hardware(8, 16)
    config = Config()
    config.set("mods.deploy_concurrency", 12)
    config.reload()
    assert effective_deploy_concurrency(config.get("mods.deploy_concurrency")) == 12

    hardware(2, 4)
    config.reload()
    assert effective_deploy_concurrency(config.get("mods.deploy_concurrency")) == 12

    assert config.reset()
    assert config.get("mods.deploy_concurrency") == 0
    assert effective_deploy_concurrency(config.get("mods.deploy_concurrency")) == 1
