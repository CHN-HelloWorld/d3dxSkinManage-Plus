from src.ui.widgets.gb_filter_bar import GbFilterBar


def test_search_emits_after_debounce(qtbot, temp_data_dir):
    bar = GbFilterBar()
    qtbot.addWidget(bar)
    fired = []
    bar.search_submitted.connect(lambda q: fired.append(q))
    bar.search_box.setText("gamma unit")
    qtbot.wait(400)
    assert fired == ["gamma unit"]


def test_sort_change_emits_signal(qtbot, temp_data_dir):
    bar = GbFilterBar()
    qtbot.addWidget(bar)
    fired = []
    bar.sort_changed.connect(lambda v: fired.append(v))
    bar.sort_combo.setCurrentText("最新")
    assert fired and fired[-1] == "newest"


def test_nsfw_toggle_three_states(qtbot, temp_data_dir):
    bar = GbFilterBar()
    qtbot.addWidget(bar)
    fired = []
    bar.nsfw_policy_changed.connect(lambda p: fired.append(p))
    bar.set_nsfw_policy("remove")
    bar.set_nsfw_policy("show")
    assert "remove" in fired and "show" in fired


def test_recent_searches_persist(qtbot, temp_data_dir):
    bar = GbFilterBar()
    qtbot.addWidget(bar)
    bar.add_recent_search("gamma unit")
    bar.add_recent_search("delta")
    saved = bar.recent_searches()
    assert "gamma unit" in saved
    assert "delta" in saved


def test_recent_searches_dedup(qtbot, temp_data_dir):
    bar = GbFilterBar()
    qtbot.addWidget(bar)
    bar.add_recent_search("gamma unit")
    bar.add_recent_search("gamma unit")
    assert bar.recent_searches().count("gamma unit") == 1
