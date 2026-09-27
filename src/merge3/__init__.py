"""Three-way structure-preserving text merge backend."""

from .merge import (
    MergeEngine,
    MergeInputError,
    ResolutionError,
    three_way_merge,
)
from .model import (
    ConflictBlock,
    ConflictType,
    Edit,
    EditKind,
    MergeResult,
    Region,
)

__all__ = [
    "MergeEngine",
    "MergeInputError",
    "ResolutionError",
    "three_way_merge",
    "ConflictBlock",
    "ConflictType",
    "Edit",
    "EditKind",
    "MergeResult",
    "Region",
]

__version__ = "1.0.0"
