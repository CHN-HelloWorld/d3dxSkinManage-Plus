"""验证加载器启动路径编辑不会改变当前部署的外观配置来源。"""

from PySide6.QtWidgets import QMessageBox

from src.ui.pages.game import GamePage
from tests.core.test_named_variant_manager import enable
from tests.core.test_variant_preset_switch import env as env


def test_clearing_loader_launch_path_keeps_deployment_runtime_available(env, qtbot, monkeypatch):
    manager, _mod, _preset = enable(env)
    page = GamePage()
    qtbot.addWidget(page)
    page._select("g1")
    page._detail._migoto.clear()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.No)

    assert page._on_save(page._detail.collect())

    assert page._svc.get_game("g1").migoto_path == ""
    assert manager.variants.user_ini_path() == env["user_ini"]
    assert manager._storage.get_deploy_dir() == env["migoto"] / "Mods"
