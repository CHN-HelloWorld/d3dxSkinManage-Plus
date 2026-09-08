from src.ui.pages.overview import OverviewPage


def test_check_updates_button_emits(qtbot):
    page = OverviewPage()
    qtbot.addWidget(page)
    with qtbot.waitSignal(page.check_updates_requested, timeout=1000):
        page._check_updates_btn.click()


def test_set_updatable_stores_ids(qtbot):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_updatable({"a", "b"})
    assert page._updatable_ids == {"a", "b"}
