"""Error taxonomy for the MP4 timeline service.

Every failure the parser or kernel can raise carries a stable ``category``
string.  The API layer maps these categories to HTTP responses and the job
store persists them, so a failed job is never reported as a generic success.
"""


class MP4Error(Exception):
    """Base class for all expected, classified failures."""

    category = "mp4_error"

    def __init__(self, message: str, *, box: str | None = None, offset: int | None = None):
        super().__init__(message)
        self.box = box
        self.offset = offset


class BoxOutOfBoundsError(MP4Error):
    """A box declares a length that exceeds its parent box or the file."""

    category = "box_out_of_bounds"


class MalformedBoxError(MP4Error):
    """A box header or table is structurally invalid (truncated, bad count...)."""

    category = "malformed_box"


class MissingBoxError(MP4Error):
    """A box required by the restricted profile is absent."""

    category = "missing_required_box"


class UnsupportedLayoutError(MP4Error):
    """Fragmented MP4 (moof/mvex/mfra) — outside the restricted profile."""

    category = "unsupported_fragmented_layout"


class UnsupportedEncryptionError(MP4Error):
    """Encrypted media (encv/enca/tenc/sinf) — outside the restricted profile."""

    category = "unsupported_encryption"


class UnsupportedFeatureError(MP4Error):
    """A legal-but-unsupported construct (e.g. edit rate != 1)."""

    category = "unsupported_feature"


class TableConsistencyError(MP4Error):
    """Sample tables contradict each other or point outside the media data."""

    category = "table_consistency"


class InputError(MP4Error):
    """The requested input file is missing, unreadable, or too large."""

    category = "input_error"
