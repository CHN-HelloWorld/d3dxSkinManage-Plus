"""验证界面对象在所属夹具清理后、外部状态恢复前完成延迟销毁。"""

import pytest
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid


@pytest.fixture
def temp_data_dir(temp_data_dir):
    """保留原目录隔离，在其恢复前检查窗口夹具及原生对象均已清理。"""
    state = {"path": temp_data_dir, "widgets": [], "owner_cleaned": False}
    yield state
    assert state["owner_cleaned"], "窗口所属夹具必须先完成自己的清理"
    assert state["widgets"]
    assert all(not isValid(widget) for widget in state["widgets"]), "Qt 延迟销毁必须先于目录与配置恢复完成"


@pytest.fixture
def owned_widget(qapp, temp_data_dir):
    """模拟仍需在清理阶段访问窗口的业务夹具。"""
    widget = QWidget()
    temp_data_dir["widgets"].append(widget)
    yield widget
    assert isValid(widget), "原生对象销毁不能早于窗口所属夹具的清理"
    widget.close()
    temp_data_dir["owner_cleaned"] = True


def test_widget_cleanup_finishes_between_owner_and_data_restore(qtbot, owned_widget):
    widget = owned_widget
    qtbot.addWidget(widget)
    assert isValid(widget)
