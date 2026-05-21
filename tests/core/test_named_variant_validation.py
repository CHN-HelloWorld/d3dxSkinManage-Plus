from __future__ import annotations

from dataclasses import replace

import pytest

from src.core.persist_vars import (
    ModIniSource,
    build_attribution_index,
    declaration_fingerprint,
    parse_namespace,
    parse_persist_vars,
    read_default_values,
    resolve_variant_values,
    snapshot_variables,
    validate_numeric_value,
)
from src.models.mod_variant import ModVariant, VariantVariable

_INIS = (
    ("merged.ini", "[Constants]\nglobal persist $swapvar = 0\nglobal persist $outline ; implicit zero\n"),
    ("sub/detail.ini", "namespace = author\\detail\n[Constants]\nglobal persist $color = 0.25\n"),
)
_VALUES = {
    "$\\mods\\old\\merged.ini\\swapvar": "2",
    "$\\mods\\old\\merged.ini\\outline": "1",
    "$\\author\\detail\\color": "0.5",
}


def _index(inis=_INIS, prefix="mods\\old"):
    return build_attribution_index([ModIniSource("mod-a", inis, implicit_prefix=prefix)])


def _variant(index=None):
    index = index or _index()
    return ModVariant(
        "test-id",
        "Saved",
        snapshot_variables(index, "mod-a", _VALUES),
        declaration_fingerprint=declaration_fingerprint(index, "mod-a"),
    )


def test_snapshot_contains_portable_complete_locators_and_relocates_implicit_keys():
    variant = _variant()
    records = {item.variable: item.to_dict() for item in variant.variables}
    assert records["swapvar"]["ini_path"] == "merged.ini"
    assert records["swapvar"]["namespace_mode"] == "implicit"
    assert not records["swapvar"].get("namespace")
    assert records["color"]["namespace"] == "author\\detail"

    values = resolve_variant_values(_index(prefix="mods\\moved"), "mod-a", variant)
    assert values == {
        "$\\mods\\moved\\merged.ini\\swapvar": "2",
        "$\\mods\\moved\\merged.ini\\outline": "1",
        "$\\author\\detail\\color": "0.5",
    }


def test_fingerprint_ignores_values_deployment_and_input_order_but_detects_structural_change():
    original = declaration_fingerprint(_index(), "mod-a")
    inis = tuple((rel, text.replace("= 0\n", "= 7\n")) for rel, text in reversed(_INIS))
    assert declaration_fingerprint(_index(inis, "mods\\elsewhere"), "mod-a") == original
    changed = ((_INIS[0][0], _INIS[0][1] + "global persist $extra = 0\n"), _INIS[1])
    assert declaration_fingerprint(_index(changed), "mod-a") != original


@pytest.mark.parametrize(
    "values",
    [
        {key: value for key, value in _VALUES.items() if not key.endswith("outline")},
        {**_VALUES, "$\\unknown\\key": "1"},
        {**_VALUES, "$\\MODS\\OLD\\MERGED.INI\\SWAPVAR": "2"},
    ],
)
def test_snapshot_rejects_missing_unknown_and_duplicate_normalized_keys(values):
    with pytest.raises(ValueError):
        snapshot_variables(_index(), "mod-a", values)


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "NaN",
        "inf",
        "-Infinity",
        "1e309",
        "3.5e38",
        "$other",
        "1 + 2",
        "true",
        "0x10",
        "1_000",
        "",
        "1\n2",
        1,
        True,
        None,
    ],
)
def test_values_must_be_finite_loader_numeric_literals(value):
    with pytest.raises(ValueError):
        validate_numeric_value(value)
    with pytest.raises(ValueError):
        VariantVariable("merged.ini", "swapvar", "implicit", value)


@pytest.mark.parametrize("value", ["0", "-0", "+2", "1.", ".5", "-1.2e-3", " 2.000 "])
def test_numeric_literal_spelling_is_retained(value):
    assert validate_numeric_value(value) == value.strip()


@pytest.mark.parametrize(
    "path",
    [
        "../merged.ini",
        r"sub\..\merged.ini",
        "/tmp/merged.ini",
        r"C:\mods\merged.ini",
        "C:merged.ini",
        r"\\server\share\merged.ini",
        "./merged.ini",
        "sub//merged.ini",
        "sub/merged.ini:stream",
        "bad\x00.ini",
        "sub/CON.ini",
        "sub./merged.ini",
        "merged.txt",
        "",
    ],
)
def test_variable_paths_cannot_escape_or_alias_local_files(path):
    with pytest.raises(ValueError):
        VariantVariable(path, "swapvar", "implicit", "1")


@pytest.mark.parametrize(
    "changes",
    [
        {"variable": "$swapvar"},
        {"variable": "ns\\swapvar"},
        {"namespace_mode": "other"},
        {"namespace_mode": "explicit"},
        {"namespace": "mods\\old\\merged.ini"},
    ],
)
def test_variable_locator_fields_are_strict(changes):
    payload = {"ini_path": "merged.ini", "variable": "swapvar", "namespace_mode": "implicit", "value": "1"}
    with pytest.raises(ValueError):
        VariantVariable.from_dict({**payload, **changes})


@pytest.mark.parametrize(
    "inis",
    [
        (("merged.ini", "namespace = shared\n[Constants]\nglobal persist $v = 0\nglobal persist $v = 1\n"),),
        (
            ("merged.ini", "namespace = shared\n[Constants]\nglobal persist $v = 0\n"),
            ("other.ini", "namespace = shared\n[Constants]\nglobal persist $v = 1\n"),
        ),
    ],
)
def test_duplicate_declarations_within_one_mod_lose_ownership(inis):
    index = _index(inis)
    assert index.resolve("$\\shared\\v") is None
    assert index.conflicts["$\\shared\\v"] == ("mod-a",)
    with pytest.raises(ValueError):
        snapshot_variables(index, "mod-a", {"$\\shared\\v": "1"})
    with pytest.raises(ValueError):
        declaration_fingerprint(index, "mod-a")


def test_cross_mod_conflicts_block_the_entire_snapshot():
    text = "namespace = shared\n[Constants]\nglobal persist $v = 0\n"
    index = build_attribution_index(
        [
            ModIniSource("mod-a", (("merged.ini", text),)),
            ModIniSource("mod-b", (("extra.ini", text),)),
        ]
    )
    with pytest.raises(ValueError):
        snapshot_variables(index, "mod-a", {"$\\shared\\v": "2"})


def test_encoding_alias_collision_does_not_choose_the_first_owner():
    index = build_attribution_index(
        [
            ModIniSource("mod-a", (("merged.ini", "namespace = 页\n[Constants]\nglobal persist $v = 0\n"),)),
            ModIniSource("mod-b", (("merged.ini", "namespace = ҳ\n[Constants]\nglobal persist $v = 0\n"),)),
        ]
    )
    assert index.resolve("$\\页\\v") is None
    assert index.resolve("$\\ҳ\\v") is None
    with pytest.raises(ValueError):
        snapshot_variables(index, "mod-a", {"$\\页\\v": "1"})


def test_a_unique_encoding_alias_remains_resolvable_and_portable():
    index = build_attribution_index(
        [
            ModIniSource("mod-a", (("merged.ini", "namespace = 页\n[Constants]\nglobal persist $v = 0\n"),)),
        ]
    )
    variables = snapshot_variables(index, "mod-a", {"$\\ҳ\\v": "1"})
    assert variables == (VariantVariable("merged.ini", "v", "explicit", "1", "页"),)
    variant = ModVariant("test-id", "Encoded", variables)
    assert resolve_variant_values(index, "mod-a", variant) == {"$\\页\\v": "1"}


def test_unpaired_surrogate_path_cannot_enter_a_portable_snapshot():
    with pytest.raises(ValueError):
        VariantVariable("invalid\ud800.ini", "v", "implicit", "1")


def test_unresolved_namespace_cannot_silently_shrink_a_snapshot():
    index = _index(prefix="")
    with pytest.raises(ValueError):
        snapshot_variables(index, "mod-a", {"$\\author\\detail\\color": "0.5"})


def test_resolution_rejects_missing_and_unknown_records_and_stale_fingerprint():
    index = _index()
    variant = _variant(index)
    for invalid in (
        replace(variant, variables=variant.variables[:-1]),
        replace(variant, variables=variant.variables + (VariantVariable("merged.ini", "unknown", "implicit", "0"),)),
        replace(variant, declaration_fingerprint="different"),
    ):
        with pytest.raises(ValueError):
            resolve_variant_values(index, "mod-a", invalid)


def test_optional_fingerprint_does_not_bypass_complete_locator_validation():
    variant = replace(_variant(), declaration_fingerprint="", source_content_hash="a" * 40)
    assert resolve_variant_values(_index(), "mod-a", variant) == _VALUES
    changed = (_INIS[0], ("sub/detail.ini", _INIS[1][1].replace("author\\detail", "different\\namespace")))
    with pytest.raises(ValueError):
        resolve_variant_values(_index(changed), "mod-a", variant)


def test_duplicate_records_are_rejected_even_when_path_case_differs():
    with pytest.raises(ValueError):
        ModVariant(
            "test-id",
            "Duplicate",
            (
                VariantVariable("merged.ini", "v", "implicit", "1"),
                VariantVariable("MERGED.INI", "V", "implicit", "1"),
            ),
        )


def test_defaults_come_from_body_inis_and_missing_initializer_means_zero():
    effective = tuple((rel, text.replace("= 0\n", "= 9\n").replace("= 0.25", "= 8")) for rel, text in _INIS)
    assert read_default_values(_index(effective), "mod-a", iter(_INIS)) == {
        "$\\mods\\old\\merged.ini\\swapvar": "0",
        "$\\mods\\old\\merged.ini\\outline": "0",
        "$\\author\\detail\\color": "0.25",
    }


@pytest.mark.parametrize("initializer", ["$other", "1 + 2", "nan", "1e309", ""])
def test_unknown_or_nonfinite_body_default_is_not_silently_zero(initializer):
    body = (("merged.ini", f"[Constants]\nglobal persist $v = {initializer}\n"),)
    index = _index((("merged.ini", "[Constants]\nglobal persist $v = 1\n"),))
    with pytest.raises(ValueError):
        read_default_values(index, "mod-a", body)


@pytest.mark.parametrize(
    "body",
    [_INIS[:-1], _INIS + (_INIS[0],), (_INIS[0], ("sub/detail.ini", _INIS[1][1] + "global persist $extra = 0\n"))],
)
def test_defaults_require_the_same_complete_unique_declaration_set(body):
    with pytest.raises(ValueError):
        read_default_values(_index(), "mod-a", body)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("\ufeff[Constants]\nglobal persist $v = 3\n", "$\\mods\\old\\merged.ini\\v"),
        ("\ufeffnamespace = author\n[Constants]\nglobal persist $v = 3\n", "$\\author\\v"),
    ],
)
def test_utf8_bom_does_not_hide_first_namespace_or_constants(text, key):
    body = (("merged.ini", text),)
    index = _index(body)
    variables = snapshot_variables(index, "mod-a", {key: "4"})
    assert len(variables) == 1
    assert read_default_values(index, "mod-a", body) == {key: "3"}


@pytest.mark.parametrize(
    "header", ["[Constants] ; persisted choices", "[Constants] trailing text [Ignored]", "[Constants"]
)
def test_loader_section_titles_cannot_hide_missing_snapshot_variables(header):
    body = (
        ("first.ini", "namespace = first\n[Constants]\nglobal persist $a = 0\n"),
        ("second.ini", f"namespace = second\n{header}\nglobal persist $b = 3\n"),
    )
    index = _index(body)

    with pytest.raises(ValueError):
        snapshot_variables(index, "mod-a", {"$\\first\\a": "1"})
    values = {"$\\first\\a": "1", "$\\second\\b": "2"}
    variant = ModVariant("test-id", "Complete", snapshot_variables(index, "mod-a", values))
    assert resolve_variant_values(index, "mod-a", variant) == values
    assert read_default_values(index, "mod-a", body) == {"$\\first\\a": "0", "$\\second\\b": "3"}


def test_commented_next_section_ends_constants_for_parsing_and_defaults():
    text = "[Constants]\n  global persist $v = 3 ; keep\n[KeySwap] ; unrelated section\nglobal persist $v = 7\n"
    index = _index((("merged.ini", text),))
    assert index.owner == {"$\\mods\\old\\merged.ini\\v": "mod-a"}
    assert parse_persist_vars(text) == ["v"]
    assert read_default_values(index, "mod-a", (("merged.ini", text),)) == {"$\\mods\\old\\merged.ini\\v": "3"}


@pytest.mark.parametrize(
    "preamble",
    [
        "namespace = first\nnamespace = second\n",
        "namespace = second\nnamespace = SECOND\n",
    ],
)
def test_duplicate_namespaces_follow_the_loaders_final_preamble_assignment(preamble):
    text = preamble + "[Constants]\nglobal persist $v = 3\n"
    body = (("merged.ini", text),)
    index = _index(body)
    assert parse_namespace(text).casefold() == "second"
    variables = snapshot_variables(index, "mod-a", {"$\\second\\v": "4"})
    assert variables == (VariantVariable("merged.ini", "v", "explicit", "4", "second"),)
    assert read_default_values(index, "mod-a", body) == {"$\\second\\v": "3"}


def test_namespace_after_commented_section_is_not_a_preamble_assignment():
    text = "namespace = first\n[Constants] ; preamble ended\nnamespace = second\nglobal persist $v = 3\n"
    assert parse_namespace(text) == "first"
    assert read_default_values(_index((("merged.ini", text),)), "mod-a", (("merged.ini", text),)) == {
        "$\\first\\v": "3"
    }


@pytest.mark.parametrize("last_assignment", ["namespace =", 'namespace = ""', "namespace = ; empty explicit namespace"])
def test_final_empty_explicit_namespace_cannot_fall_back_to_a_previous_or_implicit_one(last_assignment):
    text = f"namespace = first\n{last_assignment}\n[Constants]\nglobal persist $v = 3\n"
    body = (("merged.ini", text),)
    index = _index(body)
    assert index.resolve("$\\first\\v") is None
    assert index.resolve("$\\mods\\old\\merged.ini\\v") is None
    with pytest.raises(ValueError):
        snapshot_variables(index, "mod-a", {})
    with pytest.raises(ValueError):
        read_default_values(index, "mod-a", body)
