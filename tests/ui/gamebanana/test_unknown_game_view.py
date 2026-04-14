from src.ui.widgets.gb_unknown_game_view import GbUnknownGameView


def test_set_game_name_appears_in_text(qtbot):
    view = GbUnknownGameView()
    qtbot.addWidget(view)
    view.set_game_name("My Custom Game")
    assert "My Custom Game" in view.message_label.text()


def test_open_settings_button_emits_signal(qtbot):
    view = GbUnknownGameView()
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.open_settings_requested, timeout=1000):
        view.settings_btn.click()


def test_open_gamebanana_button_emits_signal(qtbot):
    view = GbUnknownGameView()
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.open_gamebanana_requested, timeout=1000):
        view.gamebanana_btn.click()
