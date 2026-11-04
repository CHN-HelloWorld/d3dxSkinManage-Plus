from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from src.models.mod import Mod, ModStatus
from src.ui.mod_variant_controller import _VariantDialog
from src.ui.pages.overview import OverviewPage
from tests.ui.test_mod_variant_panel import row, state, variant
from tests.ui.test_transient_ui_reuse import _flush

pytestmark = pytest.mark.usefixtures("temp_data_dir", "native_qt_teardown")


class VariantService:
    """文件事务由核心测试覆盖，界面测试使用其状态契约。"""

    def __init__(self, enabled=True):
        self.mod = Mod(
            mod_id="mod-one",
            game_id="game_a",
            name="Outfit",
            status=ModStatus.ENABLED if enabled else ModStatus.DISABLED,
        )
        self.snapshot = state(enabled=enabled, can_save=enabled)
        self.saved = [variant("first", "晨光"), variant("other", "夜色")]
        self.applied = []
        self.reference_reads = 0
        self.save_failure = ""
        self.defaults_used = []

    def get_state(self, mod_id):
        assert mod_id == "mod-one"
        return dict(self.snapshot)

    def list_variants(self, mod_id):
        assert mod_id == "mod-one"
        return list(self.saved)

    def prepare_task(self, mod_id, *, command="read", variant_id=None, name="", use_defaults=False):
        task = SimpleNamespace(
            game_id=self.mod.game_id,
            mod_id=mod_id,
            mutating=command != "read",
            command=command,
            variant_id=variant_id,
            state=None,
            variants=[],
            result=None,
            publish=lambda manager: None,
            release=lambda: None,
        )

        def run():
            if command == "apply":
                task.result = self.apply_variant(mod_id, variant_id)
            elif command in {"save", "update"}:
                task.result = self.save_variant(mod_id, name, variant_id=variant_id, use_defaults=use_defaults)
            elif command == "copy":
                task.result = self.copy_variant(mod_id, variant_id, name)
            task.state, task.variants = self.get_state(mod_id), self.list_variants(mod_id)

        task.run = run
        return task

    def references(self, mod_id, variant_id):
        self.reference_reads += 1
        return [SimpleNamespace(id="daily", name="日常搭配"), SimpleNamespace(id="night", name="摄影组合")]

    def save_variant(self, mod_id, name, *, variant_id=None, use_defaults=False):
        self.defaults_used.append(use_defaults)
        if self.save_failure and not use_defaults:
            raise ValueError(self.save_failure)
        result = variant(variant_id or "created", name.strip())
        self.saved = [item for item in self.saved if item.variant_id != result.variant_id] + [result]
        self.snapshot.update(selected_variant_id=result.variant_id, status="clean")
        return result

    def copy_variant(self, mod_id, variant_id, name=None):
        saved = variant("copy", name)
        self.saved.append(saved)
        return saved

    def apply_variant(self, mod_id, variant_id):
        self.applied.append((mod_id, variant_id))
        self.snapshot.update(selected_variant_id=variant_id, status="clean")


def harness(qtbot, *, enabled=True):
    from src.ui.mod_variant_controller import ModVariantController

    service = VariantService(enabled)
    manager = SimpleNamespace(variants=service, current_game_id="game_a", get_mod=lambda mid: service.mod)
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1024, 680)
    enable_seen = []
    controller = ModVariantController(
        page, manager, enable_callback=lambda mid: enable_seen.append((mid, service.snapshot["selected_variant_id"]))
    )
    page._selected_mod = service.mod
    page._selected_ids = {service.mod.mod_id}
    page._show_mod_panel(service.mod)
    page._detail_tabs.setCurrentIndex(2)
    page.show()
    qtbot.waitUntil(lambda: not controller._busy)
    return page, controller, service, enable_seen


def test_save_dialog_refreshes_variant_without_reloading_preview(qtbot, monkeypatch):
    page, controller, service, _ = harness(qtbot)

    def unrelated_reload(*args, **kwargs):
        pytest.fail("Saving a variant reloaded the image or hotkeys")

    monkeypatch.setattr(page, "_load_mod_panel_preview", unrelated_reload)
    monkeypatch.setattr(page, "_build_keys_panel", unrelated_reload)
    qtbot.mouseClick(page._variant_panel.save_button, Qt.MouseButton.LeftButton)
    dialog = controller._dialog
    dialog.name_input.setText("清晨")
    qtbot.mouseClick(dialog.confirm_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller._busy)
    assert service.snapshot["selected_variant_id"] == "created"
    assert "清晨" in page._panel_variant_label._full_text
    assert page._variant_panel._pending_id == "created"
    assert controller._dialog is None


def test_update_lists_references_and_delete_does_not(qtbot):
    page, controller, service, _ = harness(qtbot)
    page._variant_panel.update_requested.emit("mod-one", "other")
    dialog = controller._dialog
    assert {label.text() for label in dialog.findChildren(QLabel, "variantReference")} == {"日常搭配", "摄影组合"}
    dialog.reject()
    references_before = service.reference_reads
    page._variant_panel.delete_requested.emit("mod-one", "other")
    dialog = controller._dialog
    assert not dialog.findChildren(QLabel, "variantReference")
    assert service.reference_reads == references_before
    assert "Mod 默认" in dialog.description.text()
    dialog.reject()


def test_repeated_variant_dialogs_release_closed_widgets(qapp, qtbot):
    page, controller, _service, _ = harness(qtbot)
    dialogs = []

    for _ in range(4):
        page._variant_panel.save_requested.emit("mod-one")
        dialog = controller._dialog
        dialogs.append(dialog)
        dialog.reject()
        _flush(qapp)

    assert controller._dialog is None
    assert not page.findChildren(_VariantDialog)
    assert all(not shiboken6.isValid(dialog) for dialog in dialogs)


def test_copy_selects_new_row_without_applying_it(qtbot):
    page, controller, service, _ = harness(qtbot)
    page._variant_panel.copy_requested.emit("mod-one", "other")
    dialog = controller._dialog
    dialog.name_input.setText("夜色副本")
    qtbot.mouseClick(dialog.confirm_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller._busy)
    assert service.snapshot["selected_variant_id"] == "first"
    assert page._variant_panel._pending_id == "copy"
    assert not service.applied


def test_enable_first_prepares_private_choice_then_uses_existing_enable(qtbot):
    page, controller, service, enable_seen = harness(qtbot, enabled=False)
    page._variant_panel.enable_requested.emit("mod-one", "other")
    qtbot.waitUntil(lambda: not controller._busy)
    assert service.applied == [("mod-one", "other")]
    assert enable_seen == [("mod-one", "other")]
    assert not service.mod.is_enabled  # 控制器不得绕过部署流程。


@pytest.mark.parametrize("enabled", [True, False])
def test_left_click_applies_once_and_refreshes_current_variant(qtbot, enabled):
    page, controller, service, enable_seen = harness(qtbot, enabled=enabled)
    panel = page._variant_panel
    button = row(panel, "other").findChild(QPushButton, "variantSelect")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller._busy)

    assert service.applied == [("mod-one", "other")]
    assert service.snapshot["selected_variant_id"] == "other"
    assert row(panel, "other").property("selected") is True
    assert row(panel, "other").property("inUse") is True
    assert row(panel, "first").property("selected") is False
    assert "夜色" in page._panel_variant_label._full_text
    assert enable_seen == []


def test_failed_click_keeps_active_variant(qtbot, monkeypatch):
    page, controller, service, _ = harness(qtbot)
    panel = page._variant_panel

    def cannot_apply(*_args):
        raise OSError("无法写入用户偏好")

    monkeypatch.setattr(service, "apply_variant", cannot_apply)
    qtbot.mouseClick(row(panel, "other").findChild(QPushButton, "variantSelect"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller._busy)
    assert service.snapshot["selected_variant_id"] == "first"
    assert row(panel, "first").property("selected") is True
    assert row(panel, "other").property("selected") is False
    assert "无法写入" in panel.notice.text()


def test_switch_progress_follows_task_lifecycle(qtbot, monkeypatch):
    from threading import Event

    from src.ui.widgets.ring_progress import RingProgressButton

    page, controller, service, _ = harness(qtbot)
    panel = page._variant_panel
    release = Event()
    apply_variant = service.apply_variant

    def delayed_apply(mod_id, variant_id):
        if not release.wait(5):
            raise TimeoutError("The test did not release the variant task")
        if outcome == "failure":
            raise OSError("无法写入用户偏好")
        apply_variant(mod_id, variant_id)

    monkeypatch.setattr(service, "apply_variant", delayed_apply)
    for variant_id, outcome in (("other", "success"), (None, "failure"), (None, "context"), ("other", "close")):
        release.clear()
        target = row(panel, variant_id)
        before = target.geometry()
        selected = panel._pending_id
        try:
            qtbot.mouseClick(target.select_button, Qt.MouseButton.LeftButton)
            ring = target.findChild(RingProgressButton, "variantSwitchProgress")
            assert ring is not None and ring.isVisible()
            assert ring._indeterminate and ring._spin_timer.isActive()
            assert ring.geometry().center() == target.radio.geometry().center()
            assert target.geometry() == before
            assert row(panel, selected).property("selected") is True
            assert all(
                not item.findChild(RingProgressButton).isVisible()
                for item in panel._rows.values()
                if item is not target
            )
            if outcome == "context":
                page._hide_panel()
            elif outcome == "close":
                controller.close()
            if outcome in {"context", "close"}:
                assert not ring.isVisible() and not ring._indeterminate
        finally:
            release.set()
            qtbot.waitUntil(lambda: not controller.running)
        assert not ring.isVisible() and not ring._spin_timer.isActive()
        if outcome in {"success", "failure"}:
            assert panel._pending_id == (variant_id if outcome == "success" else selected)
        elif outcome == "context":
            page._selected_mod = service.mod
            page._show_mod_panel(service.mod)
            page._detail_tabs.setCurrentIndex(2)
            qtbot.waitUntil(lambda: not controller.running)


def test_missing_keys_requires_explicit_default_fill_retry(qtbot):
    page, controller, service, _ = harness(qtbot)
    service.save_failure = "未能读取完整的游戏内配置，请先在游戏中保存配置后重试"
    page._variant_panel.save_requested.emit("mod-one")
    dialog = controller._dialog
    dialog.name_input.setText("完整快照")
    qtbot.mouseClick(dialog.confirm_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller._busy)
    assert dialog.isVisible()
    assert service.defaults_used == [False]
    assert dialog.error_label.text() == service.save_failure
    assert dialog.defaults_button.isVisible()
    qtbot.mouseClick(dialog.defaults_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller._busy)
    assert service.defaults_used == [False, True]
    assert service.snapshot["selected_variant_id"] == "created"


@pytest.mark.parametrize("message", ["无法读取默认变量值", "变量声明冲突", "配置文件缺失", "快照包含未知变量键"])
def test_other_failures_do_not_offer_default_fill(qtbot, message):
    page, controller, service, _ = harness(qtbot)
    service.save_failure = message
    page._variant_panel.save_button.click()
    dialog = controller._dialog
    dialog.confirm_button.click()
    qtbot.waitUntil(lambda: not controller._busy)
    assert dialog.error_label.text() == message
    assert not dialog.defaults_button.isVisible()
    assert service.defaults_used == [False]


def test_context_switch_invalidates_open_dialog(qtbot):
    page, controller, service, _ = harness(qtbot)
    page._variant_panel.save_requested.emit("mod-one")
    dialog = controller._dialog
    page._hide_panel()
    assert not dialog.isVisible()
    dialog.confirm_button.click()
    assert not service.defaults_used


def test_foreign_state_event_does_not_touch_current_tab(qtbot, monkeypatch):
    page, controller, service, _ = harness(qtbot)

    def unrelated(*args, **kwargs):
        pytest.fail("A foreign state event queried the selected Mod")

    monkeypatch.setattr(service, "get_state", unrelated)
    controller.handle_core_event("variant_state_changed", mod_id="other", game_id="game_a")
    controller.handle_core_event("variant_state_changed", mod_id="mod-one", game_id="other-game")
    assert page._variant_panel.mod_id == "mod-one"
