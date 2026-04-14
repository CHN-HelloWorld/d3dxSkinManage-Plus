"""通过活动目录身份跟踪已配置的部署根。"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from src.core.deployment_root import DeploymentRootResolution, DeploymentRootTracker
from src.utils.i18n import t as tr
from src.utils.logger import logger


class DeploymentRootWatch(QObject):
    """轮询各游戏的部署根身份，报告丢失并在空闲时持久化目录重映射。"""

    relocated = Signal(str, str, str)
    invalid = Signal(str, str)
    available = Signal(str)
    failed = Signal(str, str)

    def __init__(self, manager, games, *, busy, parent=None):
        """绑定游戏与忙碌状态提供者，跟踪已有部署根并启动一秒轮询。"""
        super().__init__(parent)
        self.manager, self.games, self.busy = manager, games, busy
        self.tracker = DeploymentRootTracker()
        self._known_games = set()
        self._invalid = {}
        self._errors = {}
        self._closed = False
        self.sync_games()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    def sync_games(self, *, selected_game_id=""):
        """用户保存的显式选择可接受此前缺失的目录。"""
        current = set()
        for game in self.games.games:
            current.add(game.id)
            if game.id == selected_game_id:
                self.tracker.forget(game.id)
            self.tracker.track(game.id, game.mods_path)
            if self._invalid.get(game.id) != game.mods_path:
                self._invalid.pop(game.id, None)
        for game_id in self._known_games - current:
            self.tracker.forget(game_id)
            self._invalid.pop(game_id, None)
            self._errors.pop(game_id, None)
        self._known_games = current

    def check_current(self):
        """解析当前游戏的部署根；未选择游戏时返回未配置状态。"""
        game = self.games.get_current_game()
        if game is None:
            return DeploymentRootResolution("unset", "", "")
        return self._check(game)

    def poll(self):
        """轮询所有游戏的部署根，关闭后不再检查。"""
        if self._closed:
            return
        for game in self.games.games:
            self._check(game)

    def _check(self, game):
        """解析部署根的当前位置，去重报告丢失、恢复和重映射结果。

        事务忙碌时延后重映射；先保存游戏配置，再更新部署索引，索引失败时尝试恢复原配置。"""
        result = self.tracker.resolve(game.id, game.mods_path)
        if result.status in {"current", "unset"}:
            if self._invalid.pop(game.id, None) is not None:
                self.available.emit(game.id)
            return result
        if result.status == "missing":
            if self._invalid.get(game.id) != game.mods_path:
                self._invalid[game.id] = game.mods_path
                self.invalid.emit(game.id, game.mods_path)
            return result
        if self.busy():
            return result

        old, new = game.mods_path, result.path
        saved = False
        try:
            # 先保存权威根路径，数据库投影提交前退出时仍可按 Mod 身份恢复位置。
            self.games.rebase_deploy_dir(game.id, old, new)
            saved = True
            self.manager.rebase_deployments(game.id, old, new)
        except Exception as error:
            message = str(error)
            if saved:
                try:
                    self.games.rebase_deploy_dir(game.id, new, old)
                except Exception as rollback_error:
                    message += tr("；恢复原目录配置失败：{rollback_error}", rollback_error=rollback_error)
            failure = (new, message)
            if self._errors.get(game.id) != failure:
                self._errors[game.id] = failure
                logger.exception("更新部署目录配置失败: %s -> %s", old, new)
                self.failed.emit(game.id, message)
            return result
        self.tracker.track(game.id, new)
        self._invalid.pop(game.id, None)
        self._errors.pop(game.id, None)
        logger.info("部署目录已自动重映射: %s -> %s", old, new)
        self.relocated.emit(game.id, old, new)
        return self.tracker.resolve(game.id, new)

    def close(self):
        """停止轮询并释放部署根跟踪器持有的资源。"""
        self._closed = True
        self._timer.stop()
        self.tracker.close()
