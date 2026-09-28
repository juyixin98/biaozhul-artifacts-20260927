"""Offline replay: parse a JSON bundle of captured headers/certificates into
typed ``(Header, Certificate, Committee|None)`` items for batch application.

Bundle format::

    {
      "items": [
        {"header": {...}, "certificate": {...}, "next_committee": null|{...}},
        ...
      ]
    }

Parsing failures are INPUT errors carrying the offending batch index; they
never touch trusted state.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple

from .config import KernelConfig
from .errors import Code, LightClientError
from .types import Certificate, Committee, Header


def parse_bundle(
    obj: Any, config: KernelConfig
) -> List[Tuple[Header, Certificate, Optional[Committee]]]:
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        raise LightClientError(
            Code.MALFORMED_HEADER,
            "replay bundle must be an object with an 'items' list",
        )
    raw_items = obj["items"]
    if not raw_items:
        raise LightClientError(
            Code.MALFORMED_HEADER, "replay bundle must contain at least one item"
        )
    if len(raw_items) > config.max_batch_size:
        raise LightClientError(
            Code.BATCH_TOO_LARGE,
            f"replay bundle size {len(raw_items)} exceeds limit "
            f"{config.max_batch_size}",
            details={"size": len(raw_items), "limit": config.max_batch_size},
        )
    parsed: List[Tuple[Header, Certificate, Optional[Committee]]] = []
    for i, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise LightClientError(
                Code.MALFORMED_HEADER,
                f"items[{i}] must be an object",
                details={"index": i},
            )
        try:
            header = Header.from_dict(item.get("header"))
            certificate = Certificate.from_dict(item.get("certificate"))
            nc_raw = item.get("next_committee")
            next_committee = (
                None
                if nc_raw is None
                else Committee.from_dict(
                    nc_raw, max_size=config.committee_max_size
                )
            )
        except LightClientError as exc:
            exc.details.setdefault("index", i)
            raise
        parsed.append((header, certificate, next_committee))
    return parsed


def load_bundle_file(
    path: str, config: KernelConfig
) -> List[Tuple[Header, Certificate, Optional[Committee]]]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            obj = json.load(fh)
        except json.JSONDecodeError as exc:
            raise LightClientError(
                Code.MALFORMED_HEADER,
                f"replay file {path!r} is not valid JSON: {exc}",
                details={"path": path, "line": exc.lineno, "column": exc.colno},
            )
    return parse_bundle(obj, config)
