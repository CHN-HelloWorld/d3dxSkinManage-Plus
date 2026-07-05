"""同步编辑的文件校验在界面线程之外执行。"""

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

from src.ui.preset_deploy_worker import DeploymentPhaseWorker
from src.ui.widgets.rounded_progress import RoundedProgressBar
from src.utils.i18n import t as tr


class _FileCheckInputGuard(QObject):
    """保护待提交编辑，不重新处理控件树样式或将界面置灰。"""

    _blocked = frozenset(
        {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
            QEvent.Type.Shortcut,
            QEvent.Type.ShortcutOverride,
            QEvent.Type.Wheel,
            QEvent.Type.ContextMenu,
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
            QEvent.Type.TouchBegin,
            QEvent.Type.TouchUpdate,
            QEvent.Type.TouchEnd,
            QEvent.Type.Close,
        }
    )

    def __init__(self, owner, dialog):
        """记录待保护的所属窗口与文件检查对话框，过滤器由对话框持有。"""
        super().__init__(dialog)
        self.owner = owner.window()
        self.dialog = dialog

    def eventFilter(self, obj, event):
        """阻止所属窗口在检查期间接收输入与关闭事件，同时允许检查对话框正常响应。"""
        if event.type() not in self._blocked or not isinstance(obj, QWidget):
            return False
        if obj is self.dialog or self.dialog.isAncestorOf(obj):
            return False
        if obj is self.owner or self.owner.isAncestorOf(obj):
            if event.type() == QEvent.Type.Close:
                event.ignore()
            else:
                event.accept()
            return True
        return False


def create_file_check_dialog(parent):
    """创建不可取消的应用模态文件检查对话框，使用不定进度并延迟显示。"""
    dialog = QProgressDialog(tr("正在检查文件…"), "", 0, 0, parent)
    dialog.setWindowTitle(tr("检查文件"))
    bar = RoundedProgressBar(dialog)
    bar.setRange(0, 0)
    bar.setTextVisible(False)
    dialog.setBar(bar)
    dialog.setCancelButton(None)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(150)
    dialog.setAutoClose(False)
    return dialog


def run_file_check(work, parent):
    """执行文件检查并同步返回结果或抛出原异常。

    界面线程通过后台任务和局部事件循环保持绘制响应，同时屏蔽所属窗口输入；非界面线程直接执行工作回调。"""
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return work()
    dialog = create_file_check_dialog(parent)
    loop = QEventLoop()
    worker = DeploymentPhaseWorker(work, dialog)
    worker.finished.connect(loop.quit)
    timer = QTimer(dialog)
    timer.setSingleShot(True)
    timer.timeout.connect(dialog.show)
    guard = _FileCheckInputGuard(parent, dialog)
    app.installEventFilter(guard)
    try:
        worker.start(QThread.Priority.LowPriority)
        timer.start(150)
        loop.exec()
        if worker.error is not None:
            raise worker.error
        return worker.result
    finally:
        timer.stop()
        dialog.close()
        app.removeEventFilter(guard)
        worker.deleteLater()
        dialog.deleteLater()
