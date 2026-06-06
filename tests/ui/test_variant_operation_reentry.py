from threading import Event

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from src.core.library_protection import LibraryOperationBusy
from src.core.mod_variant_store import ModVariantStore
from src.ui.operation_check import run_file_check
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import env as env


@pytest.mark.parametrize("command", ["rename_mod", "update_mod", "rename_node"])
def test_queued_variant_cannot_enter_while_mod_command_checks_files(qapp, env, command):
    mm, mod, _ = enable(env)
    variant = save(env)
    owner = QWidget()
    mm._operation_trusted = False
    done = Event()
    entered = False
    observed = {}

    def queued_variant():
        try:
            mm.variants.rename_variant(mod.mod_id, variant.variant_id, "QueuedVariant")
        except Exception as error:
            observed["error"] = error
        finally:
            done.set()

    def runner(work):
        nonlocal entered
        if entered:
            return work()
        entered = True
        QTimer.singleShot(0, queued_variant)

        def held_check():
            assert done.wait(3), "queued callback did not run"
            return work()

        return run_file_check(held_check, owner)

    mm.operation_check_runner = runner
    actions = {
        "rename_mod": lambda: mm.rename_mod(mod.mod_id, "OuterRename", sync_folder=False),
        "update_mod": lambda: mm.update_mod(mm._index.get_mod(mod.mod_id)),
        "rename_node": lambda: mm.update_character(env["char"].id, "OuterNodeRename"),
    }
    assert actions[command]()
    assert done.is_set()
    assert isinstance(observed.get("error"), LibraryOperationBusy)
    body = mm._index.get_mod(mod.mod_id).path
    assert ModVariantStore(body).get(variant.variant_id).name == variant.name
    owner.deleteLater()
