"""快捷键录制、冲突替换及恢复默认的模态对话框。"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core.shortcuts import validate_binding
from src.ui import anim, icons
from src.ui.dialog_chrome import decorate_dialog
from src.ui.shortcut_manager import ShortcutSettings, binding_from_event
from src.ui.widgets.shortcut_controls import KeyCaps, button_style, color
from src.utils.i18n import t as tr


def _label(text="", name="", *, wrap=False):
    """创建用于快捷键说明的标签，按需换行并采用不接受文本交互的尺寸策略。"""
    label = QLabel(text)
    label.setObjectName(name)
    label.setIndent(0)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


def _button(text: str, callback, *, primary=False, ghost=False):
    """创建并连接快捷键对话框按钮，记录主操作或透明按钮样式，关闭自动默认行为。"""
    button = QPushButton(text)
    button.setProperty("primary", primary)
    button.setProperty("ghost", ghost)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(callback)
    return button


class _DialogContent(QWidget):
    """在录制区域外侧绘制焦点环，不占用内部布局空间。"""

    focus_target: QWidget | None = None

    def paintEvent(self, event):
        """在录制控件拥有焦点时，于其布局外侧绘制主题焦点环。"""
        super().paintEvent(event)
        target = self.focus_target
        if target is None or not target.hasFocus():
            return
        position = target.mapTo(self, QPoint())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(color("accent")), 2))
        painter.drawRoundedRect(
            QRectF(position.x() - 4, position.y() - 4, target.width() + 8, target.height() + 8), 12, 12
        )


class _PrototypeDialog(QDialog):
    """快捷键内容卡，共用应用的自定义标题栏与窗口边框。"""

    def __init__(self, parent: QWidget, title: str, subtitle: str):
        """以宿主顶层窗口为父对象创建固定宽度内容卡，并接入共享标题栏和边框。"""
        super().__init__(parent.window())
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setFixedWidth(470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("shortcutDialogCard")
        self.card.setFixedWidth(468)
        self.card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.card)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        self.content = _DialogContent()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(24, 24, 24, 24)
        self.body.setSpacing(0)
        self.title_label = _label(title, "shortcutDialogTitle")
        self.title_label.setMinimumHeight(31)
        self.body.addWidget(self.title_label)
        self.body.addSpacing(7)
        self.subtitle = _label(subtitle, "shortcutDialogSubtitle", wrap=True)
        self.subtitle.setMinimumHeight(20)
        self.body.addWidget(self.subtitle)
        card_layout.addWidget(self.content)
        self.footer = QFrame()
        self.footer.setObjectName("shortcutDialogFooter")
        self.buttons = QHBoxLayout(self.footer)
        self.buttons.setContentsMargins(24, 16, 24, 16)
        self.buttons.setSpacing(8)
        card_layout.addWidget(self.footer)
        self.setStyleSheet(
            button_style() + "QWidget { background: transparent; }"
            f"QLabel {{ background: transparent; border: none; color: {color('text.primary')}; font-size: 13px; }}"
            "#shortcutDialogCard { background: transparent; border: none; }"
            "#shortcutDialogTitle { font-size: 20px; font-weight: 600; }"
            f"#shortcutDialogSubtitle {{ color: {color('text.secondary')}; font-size: 12px; }}"
            "#shortcutDialogFooter { background: transparent; border: none;"
            f" border-top: 1px solid {color('stroke.card')}; border-bottom-left-radius: 11px;"
            " border-bottom-right-radius: 11px; }"
        )
        self.close_button = decorate_dialog(self).titlebar.close_button

    def keyPressEvent(self, event):
        """未按 Ctrl、Alt 或 Meta 修饰键时用回车激活聚焦按钮，忽略自动重复，其余按键交给 Qt。"""
        button = self.focusWidget()
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            and isinstance(button, QAbstractButton)
        ):
            if not event.isAutoRepeat():
                button.click()
            event.accept()
            return
        super().keyPressEvent(event)


class _Feedback(QWidget):
    """展示录制有效性、冲突操作及替换后果，并保存便于读取的合并说明。"""

    def __init__(self):
        """创建反馈标题、冲突列表和附加说明区域，预留稳定的最小高度。"""
        super().__init__()
        self.setMinimumHeight(42)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(7)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 20)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.headline = _label(wrap=True)
        self.headline.setMinimumHeight(21)
        self.headline.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.headline, 1, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        self.conflict_box = QFrame()
        self.conflict_box.setObjectName("shortcutConflicts")
        self.conflict_layout = QVBoxLayout(self.conflict_box)
        self.conflict_layout.setContentsMargins(10, 4, 10, 4)
        self.conflict_layout.setSpacing(0)
        layout.addWidget(self.conflict_box)
        self.detail_box = QWidget()
        details = QVBoxLayout(self.detail_box)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(0)
        self.detail = _label(wrap=True)
        self.detail.setMinimumHeight(21)
        details.addWidget(self.detail)
        self.shared = _label(tr("通用操作的绑定会在各支持场景中一同移除。"), wrap=True)
        self.shared.setMinimumHeight(21)
        details.addWidget(self.shared)
        layout.addWidget(self.detail_box)
        self._text = ""

    def text(self) -> str:
        """返回上次渲染的标题、冲突名称及补充说明组成的文本。"""
        return self._text

    def show_status(self, headline: str, tone="text.secondary", *, conflicts=(), detail="", shared=False, paused=False):
        """按状态重建图标、冲突列表及可选说明，延迟销毁旧冲突标签并更新布局建议。"""
        icon = (
            "actions/pause"
            if paused
            else {
                "success": "meta/check",
                "warning": "status/warning",
                "danger": "status/warning",
            }.get(tone, "status/info")
        )
        icons.apply_pixmap(self.icon, icon, 16, tone)
        self.headline.setText(headline)
        self.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {color('text.secondary')}; }}"
            f"#shortcutConflicts {{ border: 1px solid {color('stroke.card')}; border-radius: 5px; }}"
        )
        self.headline.setStyleSheet(f"color: {color(tone)}; font-size: 12px;")
        while self.conflict_layout.count():
            self.conflict_layout.takeAt(0).widget().deleteLater()
        for command in conflicts:
            label = _label(command.display_title)
            label.setContentsMargins(0, 5, 0, 5)
            label.setMinimumHeight(30)
            self.conflict_layout.addWidget(label)
        self.conflict_box.setVisible(bool(conflicts))
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.shared.setVisible(shared)
        self.detail_box.setVisible(bool(detail) or shared)
        self._text = "\n".join(
            [headline, *(command.display_title for command in conflicts), detail, self.shared.text() if shared else ""]
        )
        self.updateGeometry()


class ShortcutEditDialog(_PrototypeDialog):
    """录制并校验候选快捷键，显式保存时处理冲突替换；隐藏后解除应用级事件过滤。"""

    def __init__(self, settings: ShortcutSettings, command_id: str, parent: QWidget):
        """读取目标命令和当前绑定，构建录制区及恢复默认操作；候选键位在保存前不写入设置。"""
        self.settings = settings
        self.command = settings.command(command_id)
        subtitle = (
            tr("此操作在各支持场景中共用同一键位，确认后一起生效。")
            if self.command.group == "general"
            else tr("按下一个组合键，确认后立即生效。")
        )
        super().__init__(parent, tr("修改快捷键"), subtitle)
        self.candidate: str | None = None
        self._error = ""
        self._suspended = False
        self._composing = False
        self.saved_conflict_count = 0
        self._filter_installed = False
        self.body.addSpacing(17)
        target = QFrame()
        target.setStyleSheet(
            f"QFrame#shortcutTarget {{ background: {color('bg.base')};"
            f" border: 1px solid {color('stroke.card')}; border-radius: 5px; }}"
        )
        target.setObjectName("shortcutTarget")
        target_layout = QVBoxLayout(target)
        target_layout.setContentsMargins(12, 10, 12, 10)
        target_layout.setSpacing(7)
        target_title = _label(self.command.display_title)
        target_title.setStyleSheet("font-size: 13px; font-weight: 600;")
        target_title.setMinimumHeight(20)
        target_layout.addWidget(target_title)
        current = QHBoxLayout()
        current.setSpacing(8)
        current_label = _label(tr("当前绑定"))
        current_label.setStyleSheet(f"font-size: 11px; color: {color('text.secondary')};")
        current.addWidget(current_label)
        current.addWidget(KeyCaps(settings.binding(command_id), scale="small"))
        current.addStretch()
        target_layout.addLayout(current)
        self.body.addWidget(target)
        self.body.addSpacing(12)
        self.capture = QFrame()
        self.content.focus_target = self.capture
        self.capture.setObjectName("shortcutCapture")
        self.capture.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.capture.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.capture.setAccessibleName(tr("快捷键录制区"))
        self.capture.setMinimumHeight(148)
        capture_layout = QVBoxLayout(self.capture)
        capture_layout.setContentsMargins(10, 20, 10, 20)
        capture_layout.setSpacing(12)
        self.capture_icon = QLabel()
        self.capture_icon.setFixedSize(30, 30)
        icons.apply_pixmap(self.capture_icon, "actions/keyboard", 30, "accent")
        capture_layout.addWidget(self.capture_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.placeholder = _label(tr("按下新的按键或鼠标侧键"))
        self.placeholder.setStyleSheet("font-size: 15px; font-weight: 500;")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        capture_layout.addWidget(self.placeholder)
        self.keys = KeyCaps(scale="large", centered=True)
        capture_layout.addWidget(self.keys)
        self.caption = _label(wrap=True)
        self.caption.setMinimumHeight(17)
        self.caption.setStyleSheet(f"font-size: 11px; color: {color('text.secondary')};")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        capture_layout.addWidget(self.caption)
        self.body.addWidget(self.capture)
        self.body.addSpacing(14)
        self.feedback = _Feedback()
        self.body.addWidget(self.feedback)
        self.body.addSpacing(12)
        hint = _label(tr("Esc 取消 · Tab 移动焦点"))
        hint.setMinimumHeight(17)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"font-size: 11px; color: {color('text.tertiary')};")
        self.body.addWidget(hint)
        self.again_button = _button(tr("重新录制"), self.record_again, ghost=True)
        self.reset_button = _button(tr("恢复默认"), self.reset_candidate, ghost=True)
        self.cancel_button = _button(tr("取消"), self.reject)
        self.save_button = _button(tr("保存快捷键"), self.save, primary=True)
        self.buttons.addWidget(self.again_button)
        self.buttons.addWidget(self.reset_button)
        self.buttons.addStretch()
        self.buttons.addWidget(self.cancel_button)
        self.buttons.addWidget(self.save_button)
        QWidget.setTabOrder(self.capture, self.again_button)
        QWidget.setTabOrder(self.again_button, self.reset_button)
        QWidget.setTabOrder(self.reset_button, self.cancel_button)
        QWidget.setTabOrder(self.cancel_button, self.save_button)
        QWidget.setTabOrder(self.save_button, self.close_button)
        QWidget.setTabOrder(self.close_button, self.capture)
        self._draw_recording()

    def showEvent(self, event):
        """标记录制状态并安装应用事件过滤器，聚焦录制区，按动画设置启动图标呼吸效果。"""
        self.setProperty("shortcutRecording", True)
        QApplication.instance().installEventFilter(self)
        self._filter_installed = True
        super().showEvent(event)
        self.capture.setFocus(Qt.FocusReason.OtherFocusReason)
        # 等待按键时对图标播放呼吸动画，并遵守全局动画开关。
        if anim.is_enabled():
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            effect = QGraphicsOpacityEffect(self.capture_icon)
            self.capture_icon.setGraphicsEffect(effect)
            self._breathe = QPropertyAnimation(effect, b"opacity", self)
            self._breathe.setStartValue(1.0)
            self._breathe.setKeyValueAt(0.5, 0.4)
            self._breathe.setEndValue(1.0)
            self._breathe.setDuration(1800)
            self._breathe.setEasingCurve(QEasingCurve.Type.InOutSine)
            self._breathe.setLoopCount(-1)
            self._breathe.start()

    def hideEvent(self, event):
        """移除应用级事件过滤器、清除录制标记并停止动画，恢复正常快捷键处理。"""
        if self._filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._filter_installed = False
        self.setProperty("shortcutRecording", False)
        if hasattr(self, "_breathe"):
            self._breathe.stop()
        super().hideEvent(event)

    def _draw_recording(self):
        """按候选有效性、重复绑定、冲突及失焦状态更新反馈、保存条件与等待动画。"""
        conflicts = (
            self.settings.conflicts(self.command.id, self.candidate) if self.candidate and not self._error else []
        )
        same = self.candidate is not None and self.candidate == self.settings.binding(self.command.id)
        self.keys.set_binding(self.candidate)
        self.keys.setVisible(bool(self.candidate))
        self.placeholder.setVisible(not self.candidate)
        self.caption.setText(
            tr("已暂停录制，回到此窗口后继续")
            if self._suspended
            else tr("继续按键或按鼠标侧键可更换候选")
            if self.candidate
            else tr("侧键可单独使用，也可组合 Ctrl / Alt / Shift")
        )
        tone = "danger" if self._error else "warning" if conflicts else "accent"
        background = "bg.base" if self._error else "warning.subtle" if conflicts else "accent.subtle"
        self.capture.setStyleSheet(
            f"#shortcutCapture {{ background: {color(background)};"
            f" border: 1px solid {color(tone)}; border-radius: 8px; }}"
        )
        self.save_button.setText(tr("替换并保存") if conflicts else tr("保存快捷键"))
        self.save_button.setEnabled(bool(self.candidate) and not (self._error or same or self._suspended))
        self.reset_button.setEnabled(
            self.settings.binding(self.command.id) != self.command.default
            and self.candidate != self.command.default
            and not self._suspended
        )
        if self._suspended:
            self.feedback.show_status(tr("窗口已失焦，候选尚未保存。"), paused=True)
        elif self._error:
            self.feedback.show_status(self._error, "danger")
        elif not self.candidate:
            self.feedback.show_status(tr("录制期间不会执行快捷键对应的操作。"))
        elif same:
            self.feedback.show_status(tr("与当前绑定相同，无需保存。"))
        elif conflicts:
            self.feedback.show_status(
                tr("这个组合键已被 {count} 项操作使用", count=len(conflicts)),
                "warning",
                conflicts=conflicts,
                detail=tr(
                    "替换后，以上操作将变为<b>未绑定</b>；「{value}」将使用新按键。",
                    value=escape(self.command.display_title),
                ),
                shared=any(command.group == "general" for command in conflicts),
            )
        else:
            self.feedback.show_status(tr("可以使用，确认后立即生效"), "success")
        if hasattr(self, "_breathe"):
            if self.candidate or self._error or self._suspended:
                self._breathe.stop()
                self.capture_icon.graphicsEffect().setOpacity(1.0)
            elif self.isVisible():
                self._breathe.start()
        self.card.updateGeometry()

    def set_candidate(self, binding: str):
        """保存候选键位并按目标命令校验，随后刷新可保存状态和冲突反馈。"""
        self.candidate = binding
        self._error = validate_binding(binding, self.command)
        self._draw_recording()

    def record_again(self):
        """清空候选及校验错误并聚焦录制区，已持久化绑定不变。"""
        self.candidate = None
        self._error = ""
        self._draw_recording()
        self.capture.setFocus()

    def reset_candidate(self):
        """恢复默认操作可用时将默认键位设为待确认候选，仍需显式保存。"""
        if self.reset_button.isEnabled():
            self.set_candidate(self.command.default)
            self.capture.setFocus()

    def save(self):
        """重新检查冲突并提交候选绑定，成功后记录替换数量并接受，失败则保留窗口显示错误。"""
        if not self.save_button.isEnabled() or not self.candidate:
            return
        conflicts = self.settings.conflicts(self.command.id, self.candidate)
        if not self.settings.save_binding(self.command.id, self.candidate, replace=bool(conflicts)):
            self.feedback.show_status(self.settings.last_error, "danger")
            return
        self.saved_conflict_count = len(conflicts)
        self.accept()

    def eventFilter(self, watched, event):
        """仅处理本窗口及其后代的录制事件，保留焦点导航并拦截快捷键执行。

        失焦或输入法组合期间暂停录制，拒绝系统键组合；有效按键转为候选，Esc 取消，回车执行确认。
        """
        if watched is self and event.type() in (QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate):
            self._suspended = event.type() == QEvent.Type.WindowDeactivate
            self._draw_recording()
        if not isinstance(watched, QWidget) or not (watched is self or self.isAncestorOf(watched)):
            return False
        kind = event.type()
        if watched is self.capture and kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.content.update()
        if kind == QEvent.Type.InputMethod:
            self._composing = bool(event.preeditString())
            if self._composing:
                self.candidate = None
                self._error = tr("请结束输入法组合输入后，再录制快捷键。")
                self._draw_recording()
            return True
        if kind == QEvent.Type.MouseButtonPress:
            if event.button() not in (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton):
                return False
            if self._suspended or self._composing:
                return True
            binding = binding_from_event(event)
            if binding:
                self.set_candidate(binding)
            else:
                self.candidate = None
                self._error = tr("暂不支持这个修饰组合，请选择 Ctrl、Alt 或 Shift。")
                self._draw_recording()
            event.accept()
            return True
        if kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if kind == QEvent.Type.KeyRelease:
            if self.candidate is None and not event.modifiers():
                self._draw_recording()
            return False
        if self._suspended:
            return True
        key = event.key()
        modified = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        )
        if not modified:
            if key == Qt.Key.Key_Escape:
                self.reject()
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                return False
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if watched is self.capture:
                    self.save()
                elif isinstance(watched, QAbstractButton):
                    watched.click()
                return True
            if key == Qt.Key.Key_Space and isinstance(watched, QAbstractButton):
                return False
        if event.isAutoRepeat() or self._composing:
            return True
        if key == Qt.Key.Key_Meta or event.modifiers() & Qt.KeyboardModifier.MetaModifier:
            self.candidate = None
            self._error = tr("Windows 键组合由系统使用，请选择 Ctrl、Alt 或 Shift。")
            self._draw_recording()
            return True
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift):
            if not self.candidate:
                parts = [
                    name
                    for flag, name in (
                        (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
                        (Qt.KeyboardModifier.AltModifier, "Alt"),
                        (Qt.KeyboardModifier.ShiftModifier, "Shift"),
                    )
                    if event.modifiers() & flag
                ]
                self.keys.set_parts(parts + ["…"])
                self.keys.show()
                self.placeholder.hide()
            return True
        binding = binding_from_event(event)
        if binding:
            self.set_candidate(binding)
        else:
            self.candidate = None
            self._error = tr("暂不支持这个按键，请试试字母、数字、功能键或鼠标侧键。")
            self._draw_recording()
        return True
