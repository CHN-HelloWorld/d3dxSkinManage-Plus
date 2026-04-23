from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from src.ui.main_window import MainWindow


class _Manager:
    def __init__(self, state: str, reason: str = "") -> None:
        self._state = state
        self.index_schema_error = reason

    def index_needs_recovery(self) -> str:
        return self._state


class _Window:
    """只带自检分流所需成员的替身，方法本体仍是 MainWindow 的真实实现。"""

    def __init__(self, state: str, reason: str = "") -> None:
        self._mod_manager = _Manager(state, reason)
        self.calls: list[str] = []

    def _prompt_index_schema_mismatch(self) -> None:
        self.calls.append("incompatible")

    def _prompt_index_recovery(self) -> None:
        self.calls.append("corrupt")

    def _prompt_index_rebuild(self) -> None:
        self.calls.append("stale")

    def _recover_index_by_quarantine(self, title: str) -> None:
        self.calls.append(f"quarantine:{title}")


def test_incompatible_schema_routes_to_its_own_prompt():
    window = _Window("incompatible", "存在旧表 presets")

    MainWindow._maybe_recover_index(window)

    assert window.calls == ["incompatible"]


def test_corrupt_and_stale_routing_is_unchanged():
    corrupt, stale, healthy = _Window("corrupt"), _Window("stale"), _Window("")

    MainWindow._maybe_recover_index(corrupt)
    MainWindow._maybe_recover_index(stale)
    MainWindow._maybe_recover_index(healthy)

    assert corrupt.calls == ["corrupt"]
    assert stale.calls == ["stale"]
    assert healthy.calls == []


def test_schema_mismatch_prompt_shows_reason_without_modifying_data(monkeypatch):
    shown: list[str] = []

    def fake_warning(_parent, _title, text, *_args):
        shown.append(text)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    window = _Window("incompatible", "存在旧表 presets；索引版本 0 不是当前版本 1")

    MainWindow._prompt_index_schema_mismatch(window)

    assert "存在旧表 presets" in shown[0]
    assert "原数据库和源 Mod 文件均已保留" in shown[0]
    assert window.calls == [], "确认提示不得触发隔离或重建"


def test_schema_mismatch_never_treats_a_dialog_result_as_rebuild_permission(monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Yes),
    )
    window = _Window("incompatible", "缺少表 game_deployment_state")

    MainWindow._prompt_index_schema_mismatch(window)

    assert window.calls == []


class _GamesService:
    def __init__(self, games: list[object]) -> None:
        self.games = games


class _RebuildWindow:
    """零游戏时的重建提示替身：只带两条重建路径需要的成员。"""

    def __init__(self, games: list[object], *, reports: dict | None = None) -> None:
        self._game_service = _GamesService(games)
        self._mod_manager = self
        self.rebuilt = False
        self._reports = reports if reports is not None else {}

    # ModManager 侧
    def rebuild_all_games(self):
        self.rebuilt = True
        return self._reports

    def recover_corrupt_index(self):
        self.rebuilt = True
        return {"quarantined": "mod_index.db.corrupt-x", "reports": self._reports}

    @property
    def current_game_id(self) -> str:
        return ""

    # MainWindow 侧（重建后刷新，本测试不关心）
    def _load_characters(self, *_a, **_k):
        raise RuntimeError("不该走到刷新")

    def _refresh_overview_mods(self):
        raise RuntimeError("不该走到刷新")

    def _invalid_metadata_summary(self, _invalid):
        return ""


def test_stale_rebuild_prompt_reports_no_games_instead_of_rebuilding(monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda _p, _t, text, *_a: shown.append(text)),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *_a, **_k: pytest.fail("零游戏时不该再问是否重建")),
    )
    window = _RebuildWindow([])

    MainWindow._prompt_index_rebuild(window)

    assert "没有可重建的游戏" in shown[0]
    assert not window.rebuilt, "零游戏时不得调用 rebuild_all_games"


def test_quarantine_recovery_does_nothing_when_no_games(qtbot, monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda _p, _t, text, *_a: shown.append(text)),
    )
    window = _RebuildWindow([])

    MainWindow._recover_index_by_quarantine(window, "索引结构不兼容")

    assert "没有可重建的游戏" in shown[0]
    assert "已隔离" not in shown[0]
    assert not window.rebuilt, "零游戏时不得隔离旧库、也不得新建空库"
