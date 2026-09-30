"""更新确认进入模态循环前先暂停相关工作。"""

from __future__ import annotations

from typing import Literal

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from src.ui import icons
from src.utils.i18n import t as tr
from src.utils.logger import logger


def confirm_close_updates(parent, service) -> Literal["back", "stop", "background", "close"]:
    """显示关闭选项前冻结当前游戏的整个批次。"""
    if parent.property("mod_update_stop_confirmation"):
        return "back"
    parent.setProperty("mod_update_stop_confirmation", True)
    token = None
    result = "back"
    try:
        try:
            token = service.begin_stop_confirmation()
        except Exception as error:
            logger.warning("暂停更新以确认关闭失败: %s", error)
            QMessageBox.warning(parent, tr("暂时无法关闭更新窗口"), str(error))
            return "back"
        if not token.count:
            return "close"
        box = QMessageBox(parent)
        box.setWindowTitle(tr("关闭更新窗口"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setIconPixmap(icons.pixmap("mod-updates/warning", 40, "mod_updates.warning"))
        box.setText(tr("已暂停 {count} 项更新", count=token.count))
        box.setInformativeText(
            tr("可以终止未完成的更新，或关闭窗口并在后台更新。\n返回或转到后台后，原先手动暂停的项目会保持暂停。")
        )
        back = box.addButton(tr("返回更新"), QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton(tr("终止更新"), QMessageBox.ButtonRole.DestructiveRole)
        background = box.addButton(tr("后台更新"), QMessageBox.ButtonRole.AcceptRole)
        icons.apply_icon(back, "actions/play", "mod_updates.secondary")
        icons.apply_icon(stop, "actions/stop", "mod_updates.warning")
        icons.apply_icon(background, "actions/list-view", "mod_updates.accent")
        box.setDefaultButton(back)
        box.setEscapeButton(back)
        box.exec()
        if (not shiboken6.isValid(box)) or (not shiboken6.isValid(parent)):
            return "back"
        if box.clickedButton() is stop:
            result = "stop"
        elif box.clickedButton() is background:
            result = "background"
        return result
    finally:
        if token is not None:
            service.finish_stop_confirmation(token, confirmed=result == "stop")
        if shiboken6.isValid(parent):
            parent.setProperty("mod_update_stop_confirmation", False)


def confirm_stop_updates(parent, service, mids=None, *, exiting: bool = False) -> bool:
    """临时暂停目标集合，再停止任务或恢复原暂停状态。"""
    if parent.property("mod_update_stop_confirmation"):
        return False
    parent.setProperty("mod_update_stop_confirmation", True)
    token = None
    confirmed = False
    try:
        try:
            token = service.begin_stop_confirmation(mids, all_games=exiting)
        except Exception as error:
            logger.warning("暂停更新以确认终止失败: %s", error)
            QMessageBox.warning(parent, tr("无法终止更新"), str(error))
            return False
        if not token.count:
            return True
        box = QMessageBox(parent)
        box.setWindowTitle(tr("终止更新并退出") if exiting else tr("终止更新"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setIconPixmap(icons.pixmap("mod-updates/warning", 40, "mod_updates.warning"))
        box.setText(tr("已暂停 {count} 项更新，确定终止吗？", count=token.count))
        box.setInformativeText(tr("终止后保留旧版 Mod，已完成的更新不受影响。返回后恢复确认前的暂停状态。"))
        stop = box.addButton(
            tr("终止更新并退出") if exiting else tr("终止更新"), QMessageBox.ButtonRole.DestructiveRole
        )
        back = box.addButton(tr("返回更新"), QMessageBox.ButtonRole.RejectRole)
        icons.apply_icon(stop, "actions/stop", "mod_updates.warning")
        icons.apply_icon(back, "actions/play", "mod_updates.secondary")
        box.setDefaultButton(back)
        box.setEscapeButton(back)
        box.exec()
        if (not shiboken6.isValid(box)) or (not shiboken6.isValid(parent)):
            return False
        confirmed = box.clickedButton() is stop
        return confirmed
    finally:
        if token is not None:
            service.finish_stop_confirmation(token, confirmed=confirmed)
        if shiboken6.isValid(parent):
            parent.setProperty("mod_update_stop_confirmation", False)
