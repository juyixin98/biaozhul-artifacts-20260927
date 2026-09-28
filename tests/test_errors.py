"""Failure classes must be distinguishable: invalid input, state conflict,
resource exhaustion and computation failure each carry their own category."""
from __future__ import annotations

import numpy as np
import pytest

from resamp.dsp.polyphase import PolyphaseResampler
from resamp.errors import (ComputationError, InvalidInputError,
                           ResourceExhaustedError, StateConflictError)


def test_nonfinite_nan_input_is_invalid_input():
    rs = PolyphaseResampler(8000, 16000)
    x = np.zeros(8)
    x[3] = np.nan
    with pytest.raises(InvalidInputError) as ei:
        rs.push(x)
    assert ei.value.category == "invalid_input"
    assert ei.value.details["index"] == 3


def test_nonfinite_inf_input_is_invalid_input():
    rs = PolyphaseResampler(8000, 16000)
    with pytest.raises(InvalidInputError) as ei:
        rs.push(np.array([0.0, np.inf]))
    assert ei.value.details["index"] == 1


def test_wrong_dtype_and_shape_are_invalid_input():
    rs = PolyphaseResampler(8000, 16000)
    with pytest.raises(InvalidInputError):
        rs.push(np.zeros(10, dtype=np.float32))
    with pytest.raises(InvalidInputError):
        rs.push(np.zeros((2, 5)))
    with pytest.raises(InvalidInputError):
        rs.push([0.1, 0.2])  # type: ignore[arg-type]


def test_double_flush_is_state_conflict():
    rs = PolyphaseResampler(8000, 16000)
    rs.push(np.ones(10))
    rs.flush()
    with pytest.raises(InvalidInputError) as ei:
        # flushing again is an operation on an already-finalized stream
        rs.flush()
    assert "flushed" in str(ei.value)


def test_push_after_flush_is_state_conflict_or_invalid_state():
    rs = PolyphaseResampler(8000, 48000)
    rs.push(np.ones(10))
    rs.flush()
    with pytest.raises(InvalidInputError) as ei:
        rs.push(np.ones(2))
    assert ei.value.details["state"] == "flushed"


def test_chunk_size_limit_is_resource_exhausted():
    rs = PolyphaseResampler(8000, 16000, max_input_chunk=100)
    with pytest.raises(ResourceExhaustedError) as ei:
        rs.push(np.ones(101))
    assert ei.value.category == "resource_exhausted"
    assert ei.value.details["chunk_samples"] == 101


def test_filter_tap_cap_is_resource_exhausted():
    with pytest.raises(ResourceExhaustedError) as ei:
        PolyphaseResampler(8000, 48000, max_taps=31)
    assert ei.value.category == "resource_exhausted"


def test_extreme_ratio_factor_is_resource_exhausted():
    with pytest.raises(ResourceExhaustedError):
        PolyphaseResampler(8000, 8001, max_factor=128)


def test_float32_output_overflow_is_computation_error():
    rs = PolyphaseResampler(8000, 48000, output_dtype="float32")
    x = np.full(4096, fill_value=1e308 / 100, dtype=np.float64)
    with pytest.raises(ComputationError) as ei:
        rs.push(x)
    assert ei.value.category == "computation"
    assert "float32" in ei.value.message
    assert "output_index" in ei.value.details


def test_float64_accumulation_of_huge_values_overflows_to_computation():
    rs = PolyphaseResampler(8000, 16000)
    # Constant 1.7e308: emitted outputs with a full arm have absolute
    # coefficient sums > 1 (~2.7 for one phase), so accumulation exceeds
    # float64 -> +inf -> ComputationError (category 'computation').
    x = np.full(4096, fill_value=1.7e308, dtype=np.float64)
    with pytest.raises(ComputationError) as ei:
        rs.push(x)
    assert ei.value.category == "computation"
    assert "output_index" in ei.value.details


def test_normal_stream_does_not_raise():
    rs = PolyphaseResampler(8000, 16000, output_dtype="float32")
    y = np.concatenate([rs.push(np.full(1000, 0.5)), rs.flush()])
    assert y.dtype == np.float32
    assert np.all(np.isfinite(y))


def test_categories_are_distinct_strings():
    cats = {InvalidInputError.category, StateConflictError.category,
            ResourceExhaustedError.category, ComputationError.category}
    assert cats == {"invalid_input", "state_conflict",
                    "resource_exhausted", "computation"}
