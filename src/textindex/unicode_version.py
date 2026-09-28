"""Pinned Unicode data version.

The segmentation library ``grapheme`` 0.6.0 ships Unicode 13.0.0 data.
Everything the service stores is bound to this version string:
index blobs, document metadata and API responses.  An index built under a
different version is rejected at load time rather than silently misread.
"""

import grapheme

#: Pinned data version of the segmentation library (semver string).
GRAPHEME_LIB_VERSION = "0.6.0"

#: Unicode character-database version used by the segmentation data.
UNICODE_VERSION = grapheme.UNICODE_VERSION  # "13.0.0"

#: Storage format version for serialized index blobs (our own format).
BLOB_FORMAT_VERSION = 1

#: Human-readable identity string embedded in blobs and /version.
DATA_VERSION_IDENTITY = f"grapheme-{GRAPHEME_LIB_VERSION}/unicode-{UNICODE_VERSION}"
