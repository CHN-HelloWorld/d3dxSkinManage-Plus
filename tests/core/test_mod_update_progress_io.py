import json
import os
import threading

import pytest

from src.core.mod_update_worker import _atomic_json
from tests.helpers import registered_games as registered_games

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")


def test_progress_publish_waits_for_a_briefly_open_reader(tmp_path):
    progress = tmp_path / "progress.json"
    progress.write_text('{"phase":"verifying"}')
    entered = threading.Event()
    errors = []

    def publish():
        entered.set()
        try:
            _atomic_json(progress, {"phase": "installing"})
        except Exception as error:
            errors.append(error)

    with progress.open("rb"):
        thread = threading.Thread(target=publish)
        thread.start()
        assert entered.wait(1)
        thread.join(0.05)
        waiting_for_reader = thread.is_alive()
    thread.join(2)
    assert waiting_for_reader, "读取进度时的短暂占用不应让更新立即失败"
    assert not thread.is_alive()
    assert errors == []
    assert json.loads(progress.read_text()) == {"phase": "installing"}


def test_progress_publish_does_not_wait_forever_for_a_locked_file(tmp_path):
    progress = tmp_path / "progress.json"
    original = '{"phase":"verifying"}'
    progress.write_text(original)
    with progress.open("rb"), pytest.raises(PermissionError):
        _atomic_json(progress, {"phase": "installing"})
    assert progress.read_text() == original
