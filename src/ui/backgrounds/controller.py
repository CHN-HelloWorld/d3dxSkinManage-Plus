"""分别管理已选择来源、可用来源和原生播放状态。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImage

from src.ui import theme
from src.ui.backgrounds.scene import SceneSource
from src.utils.i18n import t as tr


class BackgroundController(QObject):
    """协调静态图片、视频和场景背景的配置、播放状态及资源回收。

    界面线程通过状态和预览信号接收结果；代次标识用于丢弃已切换来源的异步回调。
    """

    state_changed = Signal(str, str)
    active_changed = Signal(bool)
    preview_ready = Signal(str, QImage)

    def __init__(self, overlay, parent=None):
        """绑定覆盖层，初始化来源对象、退休资源列表和用于播放及尺寸防抖的定时器。"""
        super().__init__(parent or overlay)
        self.overlay = overlay
        self.options = {}
        self.active = False
        self.state, self.message = "off", ""
        self._key = None
        self._scene = None
        self._retiring = []
        self._player = None
        self._video_pump = None
        self._native_presenter = None
        self._native_ready = False
        self._paused = self._suspended = self._disposed = False
        self._engine_valid = False
        self._generation = 0
        self._video_preview_generation = -1
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._resize_scene)

    def _status(self, state, message=""):
        """仅在状态或提示内容变化时保存状态并发出 state_changed 信号。"""
        if (state, message) != (self.state, self.message):
            self.state, self.message = state, message
            self.state_changed.emit(state, message)

    def _set_active(self, active):
        """同步覆盖层启用状态，并在有效背景状态变化时发出 active_changed 信号。"""
        self.overlay.set_enabled(active)
        if active != self.active:
            self.active = active
            self.active_changed.emit(active)

    def apply(self, options: dict, *, engine_valid=False):
        """应用背景选项，并在来源身份变化时停止旧资源、清空动态帧后启动新来源。

        静态图片异步加载后校验代次；动态来源只有素材和引擎可用时才启动，其余选项原位更新。
        """
        if self._disposed:
            return
        self.options = dict(options)
        self._engine_valid = engine_valid
        source = options.get("source", "off")
        path = options.get({"image": "path", "video": "video_path", "scene": "scene_path"}.get(source, ""), "")
        key = (
            source,
            path,
            options.get("engine_path", "") if source == "scene" else "",
            engine_valid if source == "scene" else True,
        )
        changed = key != self._key
        if changed:
            self._stop_sources()
            if source in ("video", "scene") or self.overlay._dynamic:
                self.overlay.reset_dynamic_frames()
            self._key = key
            self._paused = False
        scrim = options.get("scrim_light", 30) if theme.current_theme() == "light" else options.get("scrim_dark", 45)
        blur, fit = options.get("blur", 60), options.get("fit", "cover")
        if source in ("off", "image"):
            self.overlay.configure(path if source == "image" else "", scrim, blur, fit)
            self._set_active(source == "image" and bool(path) and self.overlay._orig is not None)
            generation = self._generation
            if source == "image" and path:
                self._status("loading", tr("正在读取图片…"))
                self.overlay.when_ready(lambda: self._image_ready(generation))
            else:
                self._status("off", tr("背景已关闭") if source == "off" else tr("请选择图片。"))
            return
        self.overlay.configure_dynamic(scrim, blur, fit)
        if not path or (source == "scene" and not engine_valid):
            self._set_active(False)
            message = (
                tr("请先配置有效的 Wallpaper Engine 程序。")
                if source == "scene" and not engine_valid
                else tr("请选择素材。")
            )
            self._status("off", message)
            return
        if changed:
            self._set_active(False)
            if not self._suspended:
                self._start_dynamic()
        if self._player:
            self._player.setVolume(options.get("volume", 0) / 100)
        if self._native_presenter:
            self._native_presenter.configure(options)
        if self._scene:
            self._scene.set_fps(options.get("fps", 30))
            self._scene.set_volume(options.get("volume", 0))
        self._timer.setInterval(round(1000 / options.get("fps", 30)))
        self.resize()

    def _image_ready(self, generation):
        """忽略过期图片加载回调，根据原图是否可用更新背景启用状态与加载结果。"""
        if generation != self._generation or self._disposed:
            return
        valid = self.overlay._orig is not None
        self._set_active(valid)
        self._status("playing" if valid else "error", tr("图片已加载") if valid else tr("无法读取图片，请重新选择。"))

    def _start_dynamic(self):
        """按当前来源创建场景捕获、原生视频呈现器或视频解码及转换线程。

        暂停、挂起和已销毁状态禁止启动；创建失败通过当前代次的错误处理回收资源。
        """
        if self._disposed or self._paused or self._suspended:
            return
        source = self.options.get("source")
        if source == "scene" and not self._engine_valid:
            return
        self._generation += 1
        generation = self._generation
        self._status("loading", tr("正在加载背景…"))
        try:
            if source == "scene":
                scene = SceneSource(self.options["engine_path"], self.options["scene_path"], self)
                self._scene = scene
                scene.error.connect(lambda message: self._error(generation, message) if scene is self._scene else None)
                scene.set_volume(self.options.get("volume", 0))
                scene.start(self._scene_size(), self.options.get("fps", 30))
            elif source == "video":
                path = Path(self.options["video_path"])
                if not path.is_file():
                    raise ValueError(tr("视频文件不存在，请重新选择。"))
                from src.ui.backgrounds.video_presenter import create_presenter

                presenter = create_presenter(self.overlay, self)
                if presenter is not None:
                    self._native_presenter = presenter
                    presenter.state_changed.connect(
                        lambda state, text: self._native_status(presenter, generation, state, text)
                    )
                    presenter.preview_ready.connect(lambda image: self._native_preview(presenter, generation, image))
                    presenter.configure(self.options)
                    return
                from src.ui.backgrounds.video_playback import VideoPlayback
                from src.ui.backgrounds.video_pump import VideoFramePump
                from src.ui.worker_lifecycle import manage_page_worker

                player = self._player = VideoPlayback(
                    QUrl.fromLocalFile(str(path.resolve())), self.options.get("volume", 0) / 100
                )
                manage_page_worker(self, player)
                player.error.connect(lambda text: self._error(generation, text))
                pump = self._video_pump = VideoFramePump(
                    player.take_frame, self._video_size(), self.options.get("fit", "cover"), self.options.get("fps", 30)
                )
                manage_page_worker(self, pump)
                pump.available.connect(self._pump_ready)
                player.start()
                pump.start()
            else:
                return
            if self._scene:
                self._timer.start(round(1000 / self.options.get("fps", 30)))
        except Exception as exc:
            self._error(generation, str(exc))

    def _native_status(self, presenter, generation, state, message):
        """接收当前原生呈现器的状态，过滤旧来源通知并保留暂停期间的首帧就绪信息。"""
        if presenter is not self._native_presenter or generation != self._generation or self._disposed:
            return
        if state == "error":
            self._error(generation, message)
        elif state == "playing":
            # 暂停或最小化时仍保留首帧就绪状态，工作线程无需重复通知。
            self._native_ready = True
            self._restore_native_status()
        elif state == "loading":
            self._native_ready = False
            if not self._paused:
                self._status(state, message)

    def _restore_native_status(self):
        """在原生首帧已就绪且未暂停、未挂起时恢复背景启用与播放状态。"""
        if self._native_ready and not self._paused and not self._suspended:
            self._set_active(True)
            self._status("playing", tr("正在播放"))

    def _native_preview(self, presenter, generation, image):
        """将当前原生呈现器的预览图与视频路径一起转发，忽略过期或已销毁对象的通知。"""
        if presenter is self._native_presenter and generation == self._generation and not self._disposed:
            self.preview_ready.emit(self.options.get("video_path", ""), image)

    def _tick(self):
        """按显示节拍取出场景图像；暂停、挂起或销毁后不消费帧。"""
        if self._disposed or self._suspended or self._paused:
            return
        if self._scene:
            self._present(self._scene.take_frame())

    @Slot()
    def _pump_ready(self):
        """仅消费当前视频转换线程的最新结果，并转交统一的视频完成处理。"""
        pump = self.sender()
        if pump is not self._video_pump:
            return
        result = pump.take_result()
        if result is not None:
            _, image, error = result
            self._video_ready(self._generation, image, error)

    @Slot(int, QImage, str)
    def _video_ready(self, generation, image, error):
        """接收视频转换结果，过滤过期帧并在每个来源代次首次成功时生成预览。

        转换错误关闭当前来源，有效图像提交给覆盖层显示。
        """
        if generation != self._generation or self._disposed or self._suspended or self._paused:
            return
        if error:
            self._error(generation, error)
        else:
            if not image.isNull() and self._video_preview_generation != generation:
                self._video_preview_generation = generation
                preview = image.scaled(
                    176, 120, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
                )
                self.preview_ready.emit(self.options.get("video_path", ""), preview)
            self._present(image)

    def _present(self, image):
        """将非空动态图像提交到覆盖层，并将背景标记为有效且正在播放。"""
        if image is not None and not image.isNull():
            self.overlay.set_dynamic_frame(image)
            self._set_active(True)
            self._status("playing", tr("正在播放"))

    def _error(self, generation, message):
        """对当前来源的错误停止全部播放资源、禁用背景并发布错误提示。"""
        if generation != self._generation or self._disposed:
            return
        self._stop_sources()
        self._set_active(False)
        self._status("error", message or tr("背景加载失败，请重试。"))

    def _stop_scene(self):
        """请求场景捕获停止，并持有对象直到清理完成后再延迟销毁 Qt 对象。"""
        scene, self._scene = self._scene, None
        if scene is not None:
            self._retiring.append(scene)

            def finished():
                """从退休列表移除已清理的场景对象并安排 Qt 延迟销毁。"""
                if scene in self._retiring:
                    self._retiring.remove(scene)
                    scene.deleteLater()

            scene.finished.connect(finished)
            scene.stop()
            if scene.is_closed:
                finished()

    def _stop_sources(self):
        """使旧回调失效，停止计时和原生呈现，并异步回收场景、帧转换与解码线程。

        仍在运行的对象保存在退休列表中，直到完成信号确认可以销毁。
        """
        self._generation += 1
        self._native_ready = False
        if self._native_presenter is not None:
            presenter, self._native_presenter = self._native_presenter, None
            presenter.stop()
        self._timer.stop()
        self._resize_timer.stop()
        self._stop_scene()
        if self._video_pump is not None:
            pump, self._video_pump = self._video_pump, None
            self._retiring.append(pump)

            def pump_finished():
                """在视频转换线程结束后移除保留引用并安排 Qt 对象销毁。"""
                if pump in self._retiring:
                    self._retiring.remove(pump)
                    pump.deleteLater()

            pump.finished.connect(pump_finished)
            pump.stop()
            if not pump.isRunning():
                pump_finished()
        if self._player is not None:
            player, self._player = self._player, None
            self._retiring.append(player)

            def finished():
                """在视频解码线程结束后移除保留引用并安排 Qt 对象销毁。"""
                if player in self._retiring:
                    self._retiring.remove(player)
                    player.deleteLater()

            player.finished.connect(finished)
            player.stop()
            if not player.isRunning():
                finished()

    def playback(self, action):
        """执行重试、暂停或恢复操作，并同步原生呈现器、帧转换线程和场景捕获。

        静态图片仅支持重新读取；动态暂停保留现有画面，恢复时遵守窗口挂起状态。
        """
        if not self._disposed and self.options.get("source") == "image" and action == "retry":
            self.overlay.configure("", 0, 0, "cover")
            self.apply(self.options, engine_valid=self._engine_valid)
            return
        if self._disposed or self.options.get("source") not in ("video", "scene"):
            return
        if action == "retry":
            self._stop_sources()
            self._paused = False
            self._start_dynamic()
        elif action == "pause":
            self._paused = True
            if self._native_presenter:
                self._native_presenter.playback("pause")
            if self._video_pump:
                self._video_pump.pause(True)
            self._timer.stop()
            if self._player:
                self._player.pause()
            self._stop_scene()
            self._status("paused", tr("已暂停"))
        elif action == "resume":
            self._paused = False
            if self._native_presenter:
                self._native_presenter.playback("resume")
                self._restore_native_status()
            elif not self._suspended:
                if self._player:
                    self._player.play()
                    if self._video_pump:
                        self._video_pump.pause(False)
                else:
                    self._start_dynamic()

    def set_suspended(self, suspended: bool):
        """随窗口可见性挂起或恢复动态来源，并保持用户主动暂停的优先级。"""
        if self._suspended == suspended or self._disposed:
            return
        self._suspended = suspended
        if self._native_presenter:
            self._native_presenter.set_suspended(suspended)
            self._restore_native_status()
            return
        if suspended:
            if self._video_pump:
                self._video_pump.pause(True)
            self._timer.stop()
            if self._player:
                self._player.pause()
            self._stop_scene()
        elif not self._paused:
            if self._player:
                self._player.play()
                if self._video_pump:
                    self._video_pump.pause(False)
            elif self.options.get("source") == "scene" and self._engine_valid and self.options.get("scene_path"):
                self._start_dynamic()
            elif self.options.get("source") == "video" and self.options.get("video_path"):
                self._start_dynamic()

    def _scene_size(self):
        """根据覆盖层逻辑尺寸、设备像素比和场景缩放倍数返回至少一像素的捕获尺寸。"""
        factor = self.overlay.devicePixelRatioF() * self.options.get("scene_scale", 2)
        return max(1, round(self.overlay.width() * factor)), max(1, round(self.overlay.height() * factor))

    def resize(self):
        """更新原生呈现和视频转换目标尺寸，并将场景窗口调整合并到防抖定时器。"""
        if not self._disposed:
            if self._native_presenter:
                self._native_presenter.refresh()
            if self._video_pump:
                self._video_pump.configure(
                    self._video_size(), self.options.get("fit", "cover"), self.options.get("fps", 30)
                )
            self._resize_timer.start(250)

    def _video_size(self):
        """返回覆盖层按设备像素比换算且不小于一像素的视频目标尺寸。"""
        ratio = self.overlay.devicePixelRatioF()
        return max(1, round(self.overlay.width() * ratio)), max(1, round(self.overlay.height() * ratio))

    def _resize_scene(self):
        """将当前覆盖层对应的捕获尺寸提交给活动场景来源。"""
        if self._scene:
            self._scene.resize(self._scene_size())

    def dispose(self):
        """标记控制器已销毁并停止所有来源，使后续配置和异步结果不再生效。"""
        self._disposed = True
        self._stop_sources()
