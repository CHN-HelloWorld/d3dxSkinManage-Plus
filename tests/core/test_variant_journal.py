from src.core.variant_journal import RuntimePatch


def test_roundtrip_preserves_file_operation_reference():
    patch = RuntimePatch(file_operation_id="a" * 32, applied=True)
    assert RuntimePatch.from_json(patch.to_json()) == patch


def test_empty_patch_is_detected_and_serializable():
    empty = RuntimePatch()
    assert empty.is_empty
    assert RuntimePatch.from_json(empty.to_json()).is_empty


def test_applied_patch_is_not_empty():
    assert not RuntimePatch(file_operation_id="a" * 32, applied=True).is_empty


def test_from_json_tolerates_invalid_input_without_creating_a_reference():
    for raw in ("", None, "{ not json", "[]", '"a string"', "123"):
        assert RuntimePatch.from_json(raw).is_empty
