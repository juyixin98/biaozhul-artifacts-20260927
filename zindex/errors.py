"""Error taxonomy.

Each error has a stable ``category`` string returned in the API envelope so
clients (and tests) can assert the *failure class*, not just a status code.
Unreadable chunks are not raised as errors: a query succeeds but is reported
under ``uncertainties`` with category ``chunk_unreadable``.
"""
from __future__ import annotations


class ZIndexError(Exception):
    category = "internal_error"
    http_status = 500

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"category": self.category, "message": self.message, "details": self.details}


class SchemaNotFound(ZIndexError):
    category = "schema_not_found"
    http_status = 404


class SchemaExists(ZIndexError):
    category = "schema_exists"
    http_status = 409


class InvalidSchema(ZIndexError):
    category = "invalid_schema"
    http_status = 400


class InvalidCoordinate(ZIndexError):
    category = "invalid_coordinate"
    http_status = 400


class InvalidBox(ZIndexError):
    category = "invalid_box"
    http_status = 400


class ChunkNotFound(ZIndexError):
    category = "chunk_not_found"
    http_status = 404


class SchemaBusy(ZIndexError):
    """Raised when a destructive replace races with an in-flight writer."""

    category = "schema_busy"
    http_status = 409
