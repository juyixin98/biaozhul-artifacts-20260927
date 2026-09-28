"""Typed error categories surfaced verbatim in API responses."""

from __future__ import annotations


class ZClusterError(Exception):
    """Base class. ``category`` is a stable machine-readable failure class."""

    category = "internal_error"
    http_status = 500


class NotInitializedError(ZClusterError):
    category = "not_initialized"
    http_status = 409


class AlreadyInitializedError(ZClusterError):
    category = "already_initialized"
    http_status = 409


class SchemaValidationError(ZClusterError):
    category = "schema_validation_error"
    http_status = 400


class CoordinateError(ZClusterError):
    category = "coordinate_out_of_domain"
    http_status = 400


class QueryValidationError(ZClusterError):
    category = "query_validation_error"
    http_status = 400


class BudgetError(ZClusterError):
    category = "budget_error"
    http_status = 400
