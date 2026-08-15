from tests.ui.gamebanana.test_main_window_nav import main_window as main_window


def test_expanded_navigation_keeps_its_original_drag_limit(main_window, qtbot):
    window = main_window
    window.show()
    qtbot.wait(20)
    splitter = window._main_splitter
    splitter.moveSplitter(1, 1)
    before = splitter.sizes()[0]
    assert before > 80
    for _ in range(3):
        window._toggle_nav_collapse()
        window._toggle_nav_collapse()
        splitter.moveSplitter(1, 1)
        assert splitter.sizes()[0] >= before


def test_immediate_animation_reversal_finishes_expanded(main_window, qtbot):
    from src.ui import anim

    window = main_window
    window.show()
    qtbot.wait(20)
    before = window._nav_container.width()
    anim.set_enabled(True)
    try:
        window._toggle_nav_collapse()
        window._toggle_nav_collapse()
        qtbot.wait(500)
        assert not window._nav_collapsed
        assert window._nav_container.width() >= before
        assert window._nav_container.minimumWidth() != window._nav_container.maximumWidth()
    finally:
        anim.set_enabled(False)
