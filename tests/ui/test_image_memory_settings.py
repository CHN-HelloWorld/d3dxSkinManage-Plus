import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QLabel, QMessageBox

from src.core.config import Config
from src.utils.image_cache_budget import image_cache_budget


@pytest.fixture
def memory_settings_environment(temp_data_dir, monkeypatch):
    from src.ui import image_memory_runtime

    previous = image_cache_budget.snapshot().configured_bytes
    monkeypatch.setattr(image_memory_runtime, "recommended_image_cache_mb", lambda: 384)
    yield
    image_cache_budget.set_budget(previous)
    image_cache_budget.clear()


def test_open_auto_then_manual_then_restore_preserves_manual_preference(
    qtbot,
    memory_settings_environment,
):
    from src.ui.widgets.image_memory_settings import ImageMemorySettings

    config = Config()
    config.set("images.memory_cache_mb", 512)
    config.set("general.theme", "light")
    before = config._config_path.read_bytes()
    controls = ImageMemorySettings(config)
    qtbot.addWidget(controls)
    assert controls.memory_input.isEnabled()
    assert controls.auto_button.isChecked()
    assert config._config_path.read_bytes() == before
    assert image_cache_budget.snapshot().configured_bytes == 384 * 1024**2

    controls.memory_input.setText("1024")
    controls.memory_input.editingFinished.emit()
    config.reload()
    assert config.get("images.memory_cache_mb") == 1024
    assert config.get("images.memory_cache_mode") == "manual"
    assert not controls.auto_button.isChecked()
    assert image_cache_budget.snapshot().configured_bytes == 1024 * 1024**2

    qtbot.mouseClick(controls.auto_button, Qt.MouseButton.LeftButton)
    config.reload()
    assert config.get("images.memory_cache_mode") == "auto"
    assert config.get("images.memory_cache_mb") == 1024
    assert config.get("general.theme") == "light"
    reopened = ImageMemorySettings(config)
    qtbot.addWidget(reopened)
    assert reopened.auto_button.isChecked()
    assert reopened.memory_input.value() == 384
    assert config.get("images.memory_cache_mb") == 1024


def test_reducing_ram_limit_releases_cache_now_and_clear_keeps_visible_pixels(
    qtbot,
    memory_settings_environment,
):
    from src.ui.widgets import local_preview_cache
    from src.ui.widgets.image_memory_settings import ImageMemorySettings

    config = Config()
    config.set("images", {"memory_cache_mode": "manual", "memory_cache_mb": 768})
    config.set("gamebanana.image_cache_max_mb", 999)
    controls = ImageMemorySettings(config)
    qtbot.addWidget(controls)
    label = QLabel()
    qtbot.addWidget(label)
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    label.setPixmap(QPixmap.fromImage(image))
    # 通过计费元数据模拟大图像，无需实际分配两百 MiB。
    local_preview_cache._images.put("large", image, cost_bytes=200 * 1024**2)
    controls.memory_input.setText("64")
    controls.memory_input.editingFinished.emit()
    assert local_preview_cache._images.bytes_used == 0
    local_preview_cache._rendered_images.put("small", image)
    assert image_cache_budget.snapshot().bytes_used > 0
    qtbot.mouseClick(controls.clear_button, Qt.MouseButton.LeftButton)
    assert image_cache_budget.snapshot().bytes_used == 0
    assert label.pixmap().toImage().pixelColor(0, 0) == QColor("red")
    assert config.get("gamebanana.image_cache_max_mb") == 999
    assert "0.0" in controls.usage_label.text()


def test_save_failure_keeps_previous_runtime_and_preferences(
    qtbot,
    memory_settings_environment,
    monkeypatch,
):
    from src.core import config as config_module
    from src.ui.widgets.image_memory_settings import ImageMemorySettings

    config = Config()
    controls = ImageMemorySettings(config)
    qtbot.addWidget(controls)
    before = config._config_path.read_bytes()

    def cannot_write(*_args, **_kwargs):
        raise OSError("read-only test settings")

    monkeypatch.setattr(config_module, "write_json_atomic", cannot_write)
    controls.memory_input.setText("1024")
    controls.memory_input.editingFinished.emit()
    assert config._config_path.read_bytes() == before
    assert config.get("images.memory_cache_mode") == "auto"
    assert controls.auto_button.isChecked()
    assert controls.memory_input.value() == 384
    assert image_cache_budget.snapshot().configured_bytes == 384 * 1024**2
    assert controls.error_label.text()


def test_pressure_displays_temporary_limit_and_restoration_target_separately(
    qtbot,
    memory_settings_environment,
    monkeypatch,
):
    from src.ui.widgets.image_memory_settings import ImageMemorySettings
    from src.utils.image_cache_budget import ImageBudgetSnapshot

    config = Config()
    config.set("images", {"memory_cache_mode": "manual", "memory_cache_mb": 512})
    snapshot = ImageBudgetSnapshot(512 * 1024**2, 128 * 1024**2, 32 * 1024**2, 1, 1, True, ())
    monkeypatch.setattr(image_cache_budget, "snapshot", lambda: snapshot)
    controls = ImageMemorySettings(config)
    qtbot.addWidget(controls)
    assert hasattr(controls, "limit_label"), "The usage pane must show the effective limit separately"
    assert "128" in controls.limit_label.text()
    assert "512" in controls.pressure_label.text()
    assert controls.memory_input.value() == 512
    assert config.get("images.memory_cache_mb") == 512


def test_settings_reset_reloads_ram_controls_and_runtime(qtbot, memory_settings_environment, monkeypatch):
    from src.ui.pages.settings import SettingsPage

    config = Config()
    config.set("images", {"memory_cache_mode": "manual", "memory_cache_mb": 1024})
    page = SettingsPage()
    qtbot.addWidget(page)
    assert not page._image_memory_settings.auto_button.isChecked()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    page._reset_settings()
    assert page._image_memory_settings.auto_button.isChecked()
    assert image_cache_budget.snapshot().configured_bytes == 384 * 1024**2
    assert config.get("images.memory_cache_mb") == 768


@pytest.mark.parametrize("retype", [False, True])
def test_memory_recommendation_becomes_manual_only_after_edit(qtbot, memory_settings_environment, retype):
    from src.ui.widgets.image_memory_settings import ImageMemorySettings

    config = Config()
    controls = ImageMemorySettings(config)
    qtbot.addWidget(controls)
    controls.show()
    controls.activateWindow()
    changes = []
    controls.settings_changed.connect(lambda: changes.append(True))
    control = controls.memory_input
    control.setFocus()
    if control.isEnabled():
        qtbot.waitUntil(control.hasFocus)
    if retype:
        control.selectAll()
        qtbot.keyClicks(control, "384")
    qtbot.keyClick(control, Qt.Key.Key_Tab)
    assert config.get("images.memory_cache_mode") == ("manual" if retype else "auto")
    assert controls.auto_button.isChecked() is not retype
    assert changes == ([True] if retype else [])


def test_runtime_applies_startup_and_manual_budget_without_faking_pressure_samples(
    qtbot,
    temp_data_dir,
    monkeypatch,
):
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    from src.ui import image_memory_runtime
    from src.utils.image_cache_budget import ImageCacheBudget

    samples = []

    def sample():
        samples.append(True)
        return 16 * 1024**3, 300 * 1024**2

    budget = ImageCacheBudget()
    monkeypatch.setattr(image_memory_runtime, "image_cache_budget", budget)
    monkeypatch.setattr(image_memory_runtime, "recommended_image_cache_mb", lambda: 384)
    monkeypatch.setattr(image_memory_runtime, "available_memory", sample)
    config = Config()
    owner = QLabel()
    qtbot.addWidget(owner)
    monitor = image_memory_runtime.ImageMemoryMonitor(config, owner)
    assert budget.snapshot().configured_bytes == 384 * 1024**2
    config.set("images", {"memory_cache_mode": "manual", "memory_cache_mb": 512})
    for _ in range(6):
        monitor.apply_settings()
    assert budget.snapshot().configured_bytes == 512 * 1024**2
    assert len(samples) == 1
    assert not budget.under_pressure
    monitor.refresh()
    monitor.refresh()
    assert budget.under_pressure
    assert config.get("images.memory_cache_mb") == 512
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(monitor)
