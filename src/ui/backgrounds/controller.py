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
