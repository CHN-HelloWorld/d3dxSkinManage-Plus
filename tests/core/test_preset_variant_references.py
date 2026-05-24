import pytest

from src.models.preset import Preset

MID = "a" * 32
VID = "b" * 32


def preset():
    return Preset(id="p", name="组合", game_id="g", character_mods={"c": [MID]})


def test_roundtrip_stores_references_without_variable_snapshots():
    p = preset()
    p.mod_variants = {MID: VID}
    encoded = p.to_dict()
    assert encoded["mod_variants"] == {MID: VID}
    decoded = Preset.from_dict(encoded)
    assert decoded.mod_variants == {MID: VID}
    assert "variables" not in encoded


def test_older_preset_defaults_to_mod_default():
    encoded = preset().to_dict()
    encoded.pop("mod_variants", None)
    assert Preset.from_dict(encoded).mod_variants.get(MID) is None


def test_remove_member_removes_reference_but_not_other_members():
    p = preset()
    p.add_mod_for_character("other", "c" * 32)
    p.mod_variants = {MID: VID, "c" * 32: "d" * 32}
    p.remove_mod(MID)
    assert p.mod_variants == {"c" * 32: "d" * 32}
    p.remove_character("other")
    assert p.mod_variants == {}


@pytest.mark.parametrize("mapping", [{MID: 2}, {MID: ""}, {"not-a-member": VID}, []])
def test_invalid_reference_data_is_rejected(mapping):
    encoded = preset().to_dict()
    encoded["mod_variants"] = mapping
    with pytest.raises(ValueError):
        Preset.from_dict(encoded)


def test_add_member_initializes_default_and_retains_existing_selection():
    p = preset()
    p.mod_variants = {MID: VID}
    p.add_mod_for_character("c", MID)
    p.add_mod_for_character("other", "c" * 32)
    assert p.mod_variants == {MID: VID, "c" * 32: None}
