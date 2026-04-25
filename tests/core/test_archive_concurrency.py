import json

import pytest

from src.core.config import Config
from tests.core.test_deploy_concurrency import hardware as hardware


@pytest.mark.parametrize("kind", ["import", "export"])
def test_missing_archive_workers_follow_hardware_without_rewriting_settings(temp_data_dir, hardware, kind):
    hardware(8, 16)
    path = temp_data_dir / "config" / "settings.json"
    original = '{"general":{"theme":"light"}}'
    path.write_text(original, encoding="utf-8")
    config = Config()
    assert config.get(f"{kind}.parallel_workers") == 0

    from src.core.archive_concurrency import effective_archive_workers

    assert effective_archive_workers(config.get(f"{kind}.parallel_workers")) == 4
    hardware(32, 4)
    assert effective_archive_workers(config.get(f"{kind}.parallel_workers")) == 1
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("kind", ["import", "export"])
@pytest.mark.parametrize("value", [-1, True, "4", 1.5, None, 17, 1024, 2**31])
def test_invalid_archive_worker_preference_falls_back_only_that_key(temp_data_dir, kind, value):
    path = temp_data_dir / "config" / "settings.json"
    path.write_text(json.dumps({kind: {"parallel_workers": value}, "general": {"theme": "light"}}))
    config = Config()
    assert config.get(f"{kind}.parallel_workers") == 0
    assert config.get("general.theme") == "light"


@pytest.mark.parametrize(("configured", "expected"), [(1, 1), (16, 16), (17, 4), (1024, 4)])
def test_runtime_archive_workers_enforce_the_manual_limit(hardware, configured, expected):
    from src.core.archive_concurrency import effective_archive_workers

    hardware(8, 16)
    assert effective_archive_workers(configured) == expected


@pytest.mark.parametrize("kind", ["import", "export"])
def test_manual_archive_worker_preference_survives_hardware_change_until_reset(temp_data_dir, hardware, kind):
    hardware(8, 16)
    config = Config()
    config.set(f"{kind}.parallel_workers", 12)
    hardware(2, 4)
    config.reload()
    assert config.get(f"{kind}.parallel_workers") == 12

    from src.core.archive_concurrency import effective_archive_workers

    assert effective_archive_workers(config.get(f"{kind}.parallel_workers")) == 12
    assert config.reset()
    assert config.get(f"{kind}.parallel_workers") == 0
    assert effective_archive_workers(config.get(f"{kind}.parallel_workers")) == 1
