"""显示可取消的索引重建进度。

主线程准备数据库快照，工作线程执行发现与哈希，结果分批暂存并在最终校验后原子发布。
暂存事务不跨越事件循环，取消时等待工作线程结束后处理结果。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from src.core.rebuild_session import RebuildSession
from src.ui import acrylic, anim, theme
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later

_PHASE_LABELS = {
    "校验身份": N_("检查 Mod 信息"),
    "计算哈希": N_("校验文件内容"),
    "准备暂存索引": N_("准备保存扫描结果"),
    "暂存索引": N_("保存扫描结果"),
    "提交前校验": N_("检查扫描结果"),
    "恢复 INI 同步": N_("同步 Mod 配置"),
}


class _PrepareWorker(QThread):
    """在后台执行重建会话的文件准备或提交前校验，将错误文本交回界面线程。"""

    progress = Signal(str, int, int)

    def __init__(self, session, parent, *, validate=False):
        """保存共享重建会话和运行模式，由父窗口持有线程直到任务结束。"""
        super().__init__(parent)
        self.session = session
        self.error = ""
        self.validate = validate

    def run(self):
        """按运行模式调用会话准备或校验，并将一般异常保存为供完成回调处理的文本。"""
        import time

        last = [0.0, ""]

        def progress(phase, done, total):
            """阶段变化和阶段完成时立即报告，其余进度最多每五十毫秒发送一次。"""
            now = time.monotonic()
            if phase != last[1] or done == total or now - last[0] >= 0.05:
                last[:] = [now, phase]
                self.progress.emit(phase, done, total)

        try:
            if self.validate:
                self.session.validate(progress)
            else:
                self.session.prepare(progress)
        except Exception as error:
            self.error = str(error)


class _RestoreWorker(QThread):
    """在索引发布后后台恢复 INI 同步，失败仅记录警告，不撤销已发布索引。"""

    progress = Signal(str, int, int)

    def __init__(self, work, parent):
        """保存会话生成的同步工作对象，并将工作线程归属到父窗口。"""
        super().__init__(parent)
        self.work = work

    def run(self):
        """执行同步恢复并转发数量进度，异常记录日志后让线程正常结束。"""
        from src.utils.logger import logger

        try:
            self.work.run(lambda done, total: self.progress.emit(N_("恢复 INI 同步"), done, total))
        except Exception as error:
            logger.warning("索引已重建，恢复 INI 同步失败: %s", error)


class RebuildProgressDialog(QDialog):
    """重建索引进度对话框（模态，带进度条；不冻结主窗口）。"""

    def __init__(self, mod_manager, game_id: str, parent: QWidget | None = None):
        """固定管理器和目标游戏，初始化成功报告与错误字段，等待首次显示后启动重建。"""
        super().__init__(parent)
        anim.install_intro_fade(self)
        self._mm = mod_manager
        self._gid = game_id
        self._worker: QThread | None = None
        self.report = None  # RebuildReport（成功时）
        self.error: str = ""  # 非空表示失败
        self._started = False
        self._session = None
        self._finished = False
        self.phase = tr("等待")
        self._setup_ui()
        acrylic.apply_dialog_acrylic(self)

    def _setup_ui(self):
        """构建模态重建进度与取消按钮，禁用系统关闭按钮以保持重建生命周期完整。"""
        self.setWindowTitle(tr("重建索引"))
        self.setFixedSize(460, 190)
        self.setModal(True)
        # 禁用关闭按钮，避免重建中途被强关
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        self._title = QLabel(tr("正在重建索引…"))
        self._title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self._title)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        progress_row = QHBoxLayout()
        progress_row.setSpacing(12)
        progress_row.addWidget(self._bar, 1)
        self._percentage = QLabel("0%")
        self._percentage.setMinimumWidth(self._percentage.fontMetrics().horizontalAdvance("100%"))
        self._percentage.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self._percentage)
        layout.addLayout(progress_row)

        self._detail = QLabel(tr("扫描文件库…"))
        self._detail.setStyleSheet(f"color: {theme.color('text.secondary')}; font-size: 12px;")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._cancel = QPushButton(tr("取消"))
        self._cancel.clicked.connect(self.reject)
        layout.addWidget(self._cancel)

    def showEvent(self, event):
        """仅在首次显示后排队启动重建，使界面事件循环先处理窗口显示。"""
        super().showEvent(event)
        if not self._started:
            self._started = True
            # 先让对话框完成首帧绘制，再开工（singleShot(0) 在事件循环里排队）
            call_later(0, self, self._start)

    def _start(self):
        """在主线程创建重建会话快照后启动后台准备，初始化失败转入统一错误收尾。"""
        if self._finished or self._session is not None:
            return
        try:
            self.phase = tr("快照")
            self._session = RebuildSession(self._mm, self._gid)
            self._worker = _PrepareWorker(self._session, self)
            self._worker.progress.connect(self._phase_progress)
            self._worker.finished.connect(self._prepared)
            self._worker.start()
        except Exception as error:
            self._finish_error(str(error))

    def _phase_progress(self, phase, done, total):
        """展示阶段名称、完成数量和百分比，忽略完成或取消后的迟到进度。"""
        if self._finished or (self._session and self._session.cancel.is_set()):
            return
        self.phase = phase
        self._bar.setRange(0, total)
        self._bar.setValue(done)
        self._percentage.setText(f"{max(0, min(100, done * 100 // total))}%" if total else "—")
        display_phase = tr(_PHASE_LABELS.get(phase, phase))
        self._detail.setText(f"{display_phase} {done}/{total}" if total else f"{display_phase}…")

    def _prepared(self):
        """处理后台准备或校验结束；有错误则终止，否则分别进入分批暂存或排队提交。"""
        if self._finished:
            return
        if self._worker.error:
            self._finish_error(self._worker.error)
            return
        if self._worker.validate:
            self.phase = tr("提交索引")
            call_later(0, self, self._commit)
            return
        self._stage_next()

    def _stage_next(self):
        """在主线程暂存一批索引并让出事件循环，全部暂存后启动后台最终校验。"""
        if self._finished:
            return
        try:
            self.phase = N_("暂存索引")
            complete = self._session.stage_batch()
            self._phase_progress(self.phase, self._session.position, len(self._session.enriched))
            if not complete:
                call_later(0, self, self._stage_next)
                return
            self._phase_progress(N_("提交前校验"), 0, 0)
            self._worker.deleteLater()
            self._worker = _PrepareWorker(self._session, self, validate=True)
            self._worker.progress.connect(self._phase_progress)
            self._worker.finished.connect(self._prepared)
            self._worker.start()
        except Exception as error:
            self._finish_error(str(error))

    def _commit(self):
        """在主线程发布校验后的索引并保存报告，成功后禁止取消并启动后台 INI 同步恢复。"""
        if self._finished:
            return
        try:
            self.report = self._session.commit(defer_sync=True)
        except Exception as error:
            self._finish_error(str(error))
            return
        self._cancel.setEnabled(False)
        self._phase_progress(N_("恢复 INI 同步"), 0, 0)
        self._worker.deleteLater()
        self._worker = _RestoreWorker(self._session.sync_work, self)
        self._worker.progress.connect(self._phase_progress)
        self._worker.finished.connect(self._restored)
        self._worker.start()

    def _restored(self):
        """同步线程结束后完成并关闭重建会话，标记完成并接受对话框。"""
        if self._finished:
            return
        self._session.finish()
        self._session.close()
        self._finished = True
        self.accept()

    def _finish_error(self, error):
        """只结算一次错误，关闭已有重建会话并拒绝对话框，错误文本留给调用方展示。"""
        if self._finished:
            return
        self.error = error
        if self._session is not None:
            self._session.close()
        self._finished = True
        super().reject()

    def reject(self):
        """发布前设置取消事件并等待后续检查点收尾；发布后继续等待同步，尚未启动时直接记录取消。"""
        if self._session is not None and not self._finished:
            if self._session._published:
                self._detail.setText(tr("索引已重建，正在同步 Mod 配置…"))
                return
            self._session.cancel.set()
            self._cancel.setEnabled(False)
            self._detail.setText(tr("正在取消，等待后台任务结束…"))
            return
        if not self._finished:
            self.error = tr("重建已取消，原索引已保留")
            self._finished = True
        super().reject()

    def closeEvent(self, event):
        """活动会话期间暂缓系统关闭并请求取消，尚未启动时先标记取消以阻止排队启动。"""
        if self._session is not None and not self._finished:
            self.reject()
            event.ignore()
        else:
            if not self._finished:
                self.reject()
            super().closeEvent(event)
