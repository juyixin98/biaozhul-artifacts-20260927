"""Schema construction and explicit rejection of unsupported types."""
import pytest

from app.kernel.levels import encode_records
from app.kernel.schema import (
    SchemaError, UnsupportedLogicalTypeError, build_schema, leaf_columns,
)

from .fixtures import UNSUPPORTED_SCHEMAS


def test_max_levels_for_two_level_nested_list():
    root = build_schema({
        "fields": [{"name": "ids", "type": "list", "repetition": "optional",
                    "element": {"type": "list",
                                "element": {"type": "int32"}}}]})
    leaf = leaf_columns(root)[0]
    # outer list optional(+1), outer repeated(+1), inner list optional(+1),
    # inner repeated(+1), optional leaf(+1) => max DL 5; max RL 2.
    assert leaf.max_definition_level == 5
    assert leaf.max_repetition_level == 2


def test_simple_list_max_levels():
    root = build_schema({
        "fields": [{"name": "tags", "type": "list", "repetition": "optional",
                    "element": {"type": "string"}}]})
    leaf = leaf_columns(root)[0]
    # optional outer list(+1), repeated group(+1), optional string leaf(+1)
    assert leaf.max_definition_level == 3
    assert leaf.max_repetition_level == 1


@pytest.mark.parametrize("desc", UNSUPPORTED_SCHEMAS)
def test_unsupported_types_are_explicitly_rejected(desc):
    with pytest.raises(UnsupportedLogicalTypeError) as exc:
        build_schema(desc)
    assert "not supported" in str(exc.value)


def test_unknown_type_rejected():
    with pytest.raises(UnsupportedLogicalTypeError):
        build_schema({"fields": [{"name": "x", "type": "quantum_float"}]})


def test_missing_field_name_rejected():
    with pytest.raises(SchemaError):
        build_schema({"fields": [{"type": "int32"}]})


def test_required_null_rejected_at_encode():
    root = build_schema({"fields": [
        {"name": "id", "type": "int64", "repetition": "required"}]})
    with pytest.raises(ValueError):
        encode_records(root, [{"id": None}])
