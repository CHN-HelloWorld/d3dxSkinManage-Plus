import json
import wave
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QListWidget, QStackedWidget, QWidget
from shiboken6 import isValid

pytestmark = pytest.mark.usefixtures("native_qt_teardown")


@pytest.fixture
def story_directory(tmp_path, monkeypatch):
    from src.utils import project_story

    directory = tmp_path / "story"
    directory.mkdir()
    # 使用本地静音 PCM 验证真实多媒体后端，不依赖视频编码、网络或可听声音。
    with wave.open(str(directory / "film.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 8000 * 8)
    (directory / "story.json").write_text(
        json.dumps({"video": "film.wav", "title": "Project story"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(project_story, "get_project_story_dir", lambda: directory)
    return directory


def test_about_card_stays_idle_and_opens_replaced_media_inline(qtbot, story_directory):
    from src.ui.pages.about import AboutPage
    from src.ui.widgets.project_story_card import ProjectStoryCard

    page = AboutPage()
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    assert not page.findChildren(QMediaPlayer)
    replacement = story_directory / "replacement.wav"
    replacement.write_bytes((story_directory / "film.wav").read_bytes())
    (story_directory / "story.json").write_text(
        json.dumps({"video": "replacement.wav", "title": "Replacement"}),
        encoding="utf-8",
    )
    card = page.findChild(ProjectStoryCard)
    card.click()
    assert not page.findChildren(QDialog), "The film must stay inside the About page"
    first = page._story_player
    assert first is not None and first.isVisible()
    assert first.window() is page
    assert not card.isVisible()
    assert Path(first.player.source().toLocalFile()) == replacement
    assert first.title_label.text() == "Replacement"
    card.click()
    assert page._story_player is first
    assert len(page.findChildren(QMediaPlayer)) == 1
    first.close_button.click()
    assert page._story_player is None
    assert card.isVisible()


def test_cover_reload_reads_changed_pixels_at_the_same_path(qtbot, story_directory):
    from src.ui.widgets.project_story_card import ProjectStoryCard

    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(story_directory / "cover.png"))
    card = ProjectStoryCard()
    qtbot.addWidget(card)
    card.resize(900, 250)
    card.show()
    first = card.cover.grab().toImage()
    assert first.pixelColor(first.width() // 2, first.height() // 2).red() > 200
    image.fill(QColor("blue"))
    assert image.save(str(story_directory / "cover.png"))
    card.reload()
    second = card.cover.grab().toImage()
    assert second.pixelColor(second.width() // 2, second.height() // 2).blue() > 200


def test_card_keeps_title_hierarchy_and_readable_play_action_with_app_styles(qtbot, qapp, story_directory):
    from src.ui import theme
    from src.ui.widgets.project_story_card import ProjectStoryCard
    from src.utils.i18n import t as tr

    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet(theme.render_qss("dark"))
        card = ProjectStoryCard()
        qtbot.addWidget(card)
        card.resize(620, 250)
        card.show()
        qapp.processEvents()
        action = next(label for label in card.findChildren(QLabel) if label.text() == tr("播放短片"))
        assert action.width() >= action.fontMetrics().horizontalAdvance(action.text())
        assert card.title_label.font().pixelSize() > card.subtitle_label.font().pixelSize()
    finally:
        qapp.setStyleSheet(previous)


@pytest.mark.parametrize("close_method", ["collapse", "escape", "hide", "retheme", "close"])
def test_inline_playback_controls_and_exit_routes_release_media(qtbot, qapp, story_directory, close_method):
    from src.ui.pages.about import AboutPage

    page = AboutPage()
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    page._story_card.click()
    view = page._story_player
    player, audio = view.player, view.audio
    qtbot.waitUntil(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState, timeout=5000)
    qtbot.waitUntil(lambda: player.isSeekable() and player.duration() == 8000, timeout=5000)
    view.play_button.click()
    assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    view.seek_slider.setValue(3000)
    assert player.position() == 3000
    view.volume_slider.setValue(25)
    assert audio.volume() == pytest.approx(0.25)
    view.mute_button.click()
    assert audio.isMuted()
    view.mute_button.click()
    assert not audio.isMuted() and audio.volume() == pytest.approx(0.25)
    view.play_button.click()
    assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    if close_method == "escape":
        qtbot.keyClick(view, Qt.Key.Key_Escape)
    elif close_method == "collapse":
        view.close_button.click()
    else:
        getattr(page, close_method)()
    assert page._story_player is None
    # 重建关于页同步销毁旧内容，其他退出路径延迟销毁；两者都须释放解码器和文件。
    if isValid(player):
        assert player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        assert player.source().isEmpty()
        assert player.videoOutput() is None and player.audioOutput() is None
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(player) and not isValid(audio)
    # Windows 会拒绝重命名仍被解码器锁定的文件。
    (story_directory / "film.wav").rename(story_directory / "replaceable.wav")


@pytest.mark.parametrize("initial_fullscreen", [False, True])
def test_fullscreen_uses_same_window_and_returns_inline(qtbot, qapp, story_directory, initial_fullscreen):
    from src.ui.pages.about import AboutPage

    page = AboutPage()
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.showFullScreen() if initial_fullscreen else page.show()
    page._story_card.click()
    view = page._story_player
    player = view.player
    qtbot.waitUntil(lambda: player.isSeekable(), timeout=5000)
    player.pause()
    player.setPosition(3000)
    original_parent = view.parentWidget()
    original_geometry = page.geometry()
    original_horizontal_maximum = page._scroll.horizontalScrollBar().maximum()
    visible_windows = {widget for widget in QApplication.topLevelWidgets() if widget.isVisible()}

    view.fullscreen_button.click()
    qapp.processEvents()
    assert page.isFullScreen()
    assert view.window() is page
    assert view.geometry() == page.rect()
    assert {widget for widget in QApplication.topLevelWidgets() if widget.isVisible()} == visible_windows
    assert player.position() == 3000
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert page.isFullScreen() == initial_fullscreen
    assert page._story_player is view and view.isVisible()
    assert view.parentWidget() is original_parent
    assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    assert player.position() == 3000
    assert page.geometry() == original_geometry
    assert page._scroll.horizontalScrollBar().maximum() == original_horizontal_maximum
    view.close_button.click()


def test_leaving_about_during_fullscreen_stops_and_restores_the_main_window(qtbot, story_directory):
    from src.ui.pages.about import AboutPage

    page = AboutPage()
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    page._story_card.click()
    view = page._story_player
    player = view.player
    view.fullscreen_button.click()
    page.retheme()
    assert not page.isFullScreen()
    assert page._story_player is None
    assert not isValid(player)
    assert page._story_card.isVisible()


def test_escape_handles_the_film_before_the_host_navigation_shortcut(qtbot, story_directory):
    from src.ui.pages.about import AboutPage

    page = AboutPage()
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    navigations = []
    host_escape = QShortcut(QKeySequence("Escape"), page, activated=lambda: navigations.append(True))
    assert host_escape.isEnabled()
    page.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is page)
    page._story_card.click()
    view = page._story_player
    view.fullscreen_button.click()
    qtbot.keyClick(view.close_button, Qt.Key.Key_Escape)
    assert not page.isFullScreen()
    assert page._story_player is view
    qtbot.keyClick(view.play_button, Qt.Key.Key_Escape)
    assert page._story_player is None
    assert not navigations


@pytest.mark.parametrize("close_method", ["button", "escape"])
@pytest.mark.parametrize("fullscreen", [False, True])
def test_film_focus_changes_keep_about_selected(qtbot, story_directory, close_method, fullscreen):
    from src.ui.pages.about import AboutPage

    host = QWidget()
    qtbot.addWidget(host)
    navigation = QListWidget()
    navigation.addItem("Games")
    stack = QStackedWidget()
    stack.addWidget(QWidget())
    page = AboutPage()
    stack.addWidget(page)
    layout = QHBoxLayout(host)
    layout.addWidget(navigation)
    layout.addWidget(stack)
    navigation.currentRowChanged.connect(lambda row: stack.setCurrentIndex(0) if row >= 0 else None)
    host.resize(1200, 780)
    host.show()
    host.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is host)
    navigation.setCurrentRow(-1)
    stack.setCurrentWidget(page)
    page._story_card.setFocus()
    page._story_card.click()
    assert stack.currentWidget() is page
    view = page._story_player
    if fullscreen:
        view.fullscreen_button.click()
        assert stack.currentWidget() is page
        qtbot.keyClick(view, Qt.Key.Key_Escape)
        assert stack.currentWidget() is page and view.isVisible()
    view.close_button.setFocus()
    if close_method == "escape":
        qtbot.keyClick(view.close_button, Qt.Key.Key_Escape)
    else:
        view.close_button.click()
    assert stack.currentWidget() is page
    assert navigation.currentRow() == -1
    assert page._story_card.hasFocus()


def test_missing_media_shows_inline_error_and_can_return_to_the_cover(qtbot, story_directory):
    from src.ui.pages.about import AboutPage

    (story_directory / "film.wav").unlink()
    page = AboutPage()
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    page._story_card.click()
    view = page._story_player
    assert view.status_label.isVisible() and view.status_label.text()
    assert not view.play_button.isEnabled()
    assert view.player.source().isEmpty()
    assert view.window() is page
    view.close_button.click()
    assert page._story_card.isVisible()
