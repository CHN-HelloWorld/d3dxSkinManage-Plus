import json


def test_update_worker_localizes_results_in_the_requested_language(tmp_path, monkeypatch):
    from src.core import mod_update_worker
    from src.utils import i18n

    languages = tmp_path / "config/languages"
    locale = languages / "de-DE"
    locale.mkdir(parents=True)
    (locale / "strings.json").write_text(
        json.dumps(
            {
                "messages": {"文件已准备": "Dateien vorbereitet"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog = i18n.I18n(directory=languages)
    monkeypatch.setattr(i18n, "_i18n", catalog)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"language": "de-DE", "work_root": "."}), encoding="utf-8")
    (tmp_path / "start").touch()
    monkeypatch.setattr(
        mod_update_worker,
        "prepare_update",
        lambda plan, report: {
            "message": i18n.t("文件已准备"),
        },
    )
    assert mod_update_worker.main(str(plan)) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["result"]["message"] == "Dateien vorbereitet"


def test_background_renderer_uses_parent_language_without_reloading_each_frame(tmp_path, monkeypatch, qtbot):
    from src.ui import theme
    from src.ui.backgrounds import render_worker
    from src.utils import i18n

    languages = tmp_path / "config/languages"
    locale = languages / "en-US"
    locale.mkdir(parents=True)
    (locale / "strings.json").write_text(
        json.dumps(
            {
                "messages": {"背景已关闭": "Background disabled"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog = i18n.I18n(directory=languages)
    monkeypatch.setattr(i18n, "_i18n", catalog)
    monkeypatch.setattr(render_worker, "attach_child", lambda *args: None)
    monkeypatch.setattr(render_worker, "position_child", lambda *args: None)
    canvas = render_worker.VideoCanvas(1)
    previous_theme = theme.current_theme()
    qtbot.addWidget(canvas)
    states = []
    canvas.controller.state_changed.connect(lambda state, message: states.append(message))
    message = dict(
        language="en-US", parent=42, rect=[0, 0, 100, 100], size=[100, 100], theme="light", options={"source": "off"}
    )
    try:
        canvas.configure(message)
        assert "Background disabled" in states

        def unexpected_reload():
            raise AssertionError("Language packs must not be read on every video frame")

        monkeypatch.setattr(catalog, "_discover", unexpected_reload)
        for _ in range(5):
            canvas.configure(message)
    finally:
        canvas.dispose()
        theme.set_theme(previous_theme)
