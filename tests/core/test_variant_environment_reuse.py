"""验证批量外观监控共用环境解析，下一次刷新仍能读取最新配置。"""

from pathlib import Path
from types import SimpleNamespace

from src.core.mod_storage import ModStorage
from src.core.mod_variant_manager import ModVariantManager
from src.core.user_ini_watcher import UserIniWatcher
from src.utils import fs


def _environment(tmp_path, count):
    """建立真实部署目录与只读索引投影，隔离监控身份生成所需的文件操作。"""
    loader = tmp_path / "loader"
    root = loader / "Mods"
    root.mkdir(parents=True)
    (loader / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
    storage = ModStorage()
    storage.set_migoto_mods_dir(root)
    mods = []
    for index in range(count):
        name = f"Sample{index}"
        deployed = root / name
        deployed.mkdir()
        mods.append(
            SimpleNamespace(
                mod_id=f"{index + 1:032x}",
                game_id="sample",
                content_hash="a" * 40,
                path=str(tmp_path / "library" / f"{name}.zip"),
                folder_name=name,
                deployed_path=str(deployed),
                is_enabled=True,
            )
        )
    active = list(mods)
    manager = SimpleNamespace(
        current_game_id="sample",
        _storage=storage,
        _index=SimpleNamespace(get_enabled_mods=lambda game_id: active),
    )
    return ModVariantManager(manager), active, mods, loader


def test_watch_environment_reads_do_not_grow_with_enabled_mod_count(tmp_path, monkeypatch):
    service, active, mods, _loader = _environment(tmp_path, 100)
    original = fs.read_bytes
    reads = []

    def read(path):
        """只统计真实的加载器配置读取，保留完整文件访问行为。"""
        if Path(path).name == "d3dx.ini":
            reads.append(path)
        return original(path)

    monkeypatch.setattr(fs, "read_bytes", read)
    active[:] = mods[:1]
    service._refresh_watch_context()
    single = len(reads)
    reads.clear()
    active[:] = mods

    service._refresh_watch_context()

    assert len(service._watch_context["mods"]) == 100
    assert 0 < len(reads) == single <= 4


def test_next_watch_refresh_uses_changed_user_configuration(tmp_path):
    service, _active, _mods, loader = _environment(tmp_path, 3)
    service._watcher = UserIniWatcher(service._on_user_ini_changed, enable_watch=False)
    try:
        service._refresh_watch_context()
        assert service._watcher.target == loader / "d3dx_user.ini"
        (loader / "d3dx.ini").write_text(
            "[Include]\ninclude_recursive = Mods\nuser_config = selected.ini\n", encoding="utf-8"
        )

        service._refresh_watch_context()

        assert service._watcher.target == loader / "selected.ini"
        assert len(service._watch_context["mods"]) == 3
    finally:
        service.stop_watch()
