import pytest

from tests.core.test_variant_default_capture import captured_default
from tests.core.test_variant_preset_switch import env as env


@pytest.mark.parametrize("initializer,captured", [("0", "5"), ("1 + 1", "2")])
def test_captured_default_has_save_action_without_false_notice(qtbot, env, initializer, captured):
    from src.ui.widgets.mod_variant_panel import ModVariantPanel

    mm, mod = captured_default(env, initializer, captured)
    panel = ModVariantPanel()
    qtbot.addWidget(panel)
    panel.set_state(mm.variants.get_state(mod.mod_id), [])
    panel.show()
    assert panel.save_button.isEnabled()
    assert not panel.notice.isVisible()
    assert panel.notice.text() == ""
