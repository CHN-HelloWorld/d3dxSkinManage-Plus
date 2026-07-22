import pytest
from PySide6.QtCore import Qt

from src.core.config import Config
from src.ui.pages.settings import SettingsPage
from tests.core.test_deploy_concurrency import hardware as hardware


@pytest.mark.parametrize(
    ("key", "input_name", "category", "section"),
    [
        ("mods.deploy_concurrency", "_deploy_concurrency_input", 0, "management"),
        ("import.parallel_workers", "_parallel_spin", 3, "import"),
        ("export.parallel_workers", "_export_parallel_spin", 3, "export"),
        ("gamebanana.update_check.concurrency", "_upd_concurrency_input", 4, "mod-updates"),
    ],
)
@pytest.mark.parametrize(
    ("typed", "commit", "expected"),
    [
        ("17", Qt.Key.Key_Tab, 16),
        ("0", Qt.Key.Key_Return, 1),
    ],
)
def test_keyboard_commit_bounds_and_persists_worker_count(
    qtbot,
    temp_data_dir,
    hardware,
    key,
    input_name,
    category,
    section,
    typed,
    commit,
    expected,
):
    hardware(8, 16)
    config = Config()
    config.set(key, 12)
    page = SettingsPage()
    qtbot.addWidget(page)
    page.resize(1120, 800)
    page._switch_settings_section(category)
    page._stack.currentWidget().select_section(section)
    page.show()
    page.activateWindow()
    control = getattr(page, input_name)
    control.setFocus()
    qtbot.waitUntil(control.hasFocus)
    control.selectAll()
    qtbot.keyClicks(control, typed)
    qtbot.keyClick(control, commit)

    assert control.value() == expected
    assert control.text() == str(expected)
    config.reload()
    assert config.get(key) == expected
