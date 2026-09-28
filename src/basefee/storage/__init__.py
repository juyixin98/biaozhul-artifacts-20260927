"""Durable SQLite index storage for accepted blocks and receipts."""

from .store import Store, StorageError

__all__ = ["Store", "StorageError"]
