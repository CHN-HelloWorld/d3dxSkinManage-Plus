from src.ui.widgets.gb_browse_view import GbBrowseView


def test_has_filter_breadcrumb_and_grid_stack(qtbot):
    view = GbBrowseView()
    qtbot.addWidget(view)
    assert view.filter_bar is not None
    assert view.breadcrumb is not None
    assert view.grid is not None
    assert view.stack is not None


def test_set_loading_state_switches_stack(qtbot):
    view = GbBrowseView()
    qtbot.addWidget(view)
    view.show()
    view.show_loading()
    assert view.stack.currentWidget() is view.loading_state
    view.show_empty()
    assert view.stack.currentWidget() is view.empty_state
    view.show_grid()
    assert view.stack.currentWidget() is view.grid
