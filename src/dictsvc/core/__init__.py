"""Execution kernel: pure, adapter-independent dictionary merge/remap."""
from .decode import (
    BatchDecode,
    Mismatch,
    RoundtripReport,
    decode_encoding,
    original_rows,
    verify_roundtrip,
)
from .encode import encode_run
from .errors import (
    CardinalityOverflow,
    DictSvcError,
    DuplicateBatchId,
    DuplicateDictionaryValue,
    DictionaryContainsNull,
    IndexOutOfRange,
    RequestMalformed,
    RunConflict,
    RunNotFound,
    UnsupportedValueType,
)
from .model import BatchInput, BatchRemap, BatchStats, GlobalEncoding
from .policy import NULL_SENTINEL, SORT_POLICY, WIDTHS, capacity

__all__ = [
    "encode_run", "decode_encoding", "verify_roundtrip", "original_rows",
    "BatchDecode", "Mismatch", "RoundtripReport",
    "BatchInput", "BatchRemap", "BatchStats", "GlobalEncoding",
    "DictSvcError", "RequestMalformed", "DictionaryContainsNull",
    "IndexOutOfRange", "DuplicateBatchId", "UnsupportedValueType",
    "DuplicateDictionaryValue", "CardinalityOverflow",
    "RunConflict", "RunNotFound",
    "NULL_SENTINEL", "SORT_POLICY", "WIDTHS", "capacity",
]
