"""在界面测试之间完成 Qt 对象销毁，避免遗留回调进入下一项测试。"""

import pytest


@pytest.fixture(autouse=True)
def _flush_qt_deferred_deletes(request):
    """在窗口夹具清理后、目录与配置恢复前完成 Qt 延迟销毁。"""
    if "qapp" not in request.fixturenames:
        yield
        return

    request.getfixturevalue("qapp")
    # 提前建立状态夹具依赖，使其恢复晚于本夹具；不为非 Qt 用例创建应用或隔离目录。
    for name in ("temp_data_dir", "monkeypatch"):
        if name in request.fixturenames:
            request.getfixturevalue(name)

    yield

    from PySide6.QtCore import QCoreApplication, QEvent

    # processEvents 本身不保证处理主事件循环之外的 DeferredDelete。
    app = QCoreApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
