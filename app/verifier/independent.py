"""Independent disclosure verifier.

DELIBERATELY STANDALONE: this module imports nothing from ``app.crypto`` or
``app.domain``. It re-derives canonical encoding, the commitment hash and the
Merkle fold using only the Python standard library, so the service kernel
cannot make itself "look correct" by sharing code with the checker. The
formula it implements is the published protocol (docs/DESIGN.md §2); frozen
test vectors in tests/test_commitment_vectors.py pin both implementations to
fixed digest literals.

Verdict codes are *specific* — a failure is never flattened into success:

    VALID
    MALFORMED_PACKAGE       structure / version / hex errors
    COMMITMENT_MISMATCH     recomputed field commitment != claimed commitment
    PROOF_INVALID           Merkle path does not reach the claimed root
    ROOT_MISMATCH           recomputed whole-tree root != package root
    CELL_SET_MISMATCH       manifest cell set does not match the disclosed set
                            (duplicate, gap, reorder, identity substitution)

A separate, reference-assisted ``classify_item`` distinguishes WRONG_SALT,
WRONG_VALUE and FIELD_IDENTITY_MISMATCH when the caller can supply the
expected commitment (e.g. the trusted service reading its own database).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import struct
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

COMMITMENT_DOMAIN = b"audit-commit-v1"
MERKLE_DOMAIN = b"audit-merkle-v1"
DISCLOSURE_VERSION = "audit-disclosure-v1"

SHA256_HEX_LEN = 64


class VerifyStatus:
    VALID = "VALID"
    MALFORMED_PACKAGE = "MALFORMED_PACKAGE"
    COMMITMENT_MISMATCH = "COMMITMENT_MISMATCH"
    PROOF_INVALID = "PROOF_INVALID"
    ROOT_MISMATCH = "ROOT_MISMATCH"
    CELL_SET_MISMATCH = "CELL_SET_MISMATCH"
    WRONG_SALT = "WRONG_SALT"
    WRONG_VALUE = "WRONG_VALUE"
    FIELD_IDENTITY_MISMATCH = "FIELD_IDENTITY_MISMATCH"


# ---------------------------------------------------------------------------
# Independent canonical encoder (mirrors app/domain/types.py by specification)
# ---------------------------------------------------------------------------

_TYPE_TAGS = {
    "string": 0x10,
    "int": 0x11,
    "decimal": 0x12,
    "bool": 0x13,
    "date": 0x14,
    "timestamp": 0x15,
    "null": 0x1F,
}
MISSING_PAYLOAD = b"\xff\xff"


class IndependentEncodeError(ValueError):
    pass


def _lp(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _encode_value(field_type: str, value: Any, state: str) -> bytes:
    if state == "missing":
        return MISSING_PAYLOAD
    if field_type not in _TYPE_TAGS:
        raise IndependentEncodeError(f"unknown field type: {field_type!r}")
    # Explicit NULL is valid for every declared type and has one tag.
    if value is None:
        return bytes([_TYPE_TAGS["null"]])

    tag = bytes([_TYPE_TAGS[field_type]])
    if field_type == "null":
        raise IndependentEncodeError("declared null field requires value None")

    if field_type == "string":
        if not isinstance(value, str):
            raise IndependentEncodeError("string requires str")
        return tag + _lp(unicodedata.normalize("NFC", value).encode("utf-8"))
    if field_type == "int":
        if isinstance(value, bool):
            raise IndependentEncodeError("int rejects bool")
        if not isinstance(value, int):
            raise IndependentEncodeError("int requires int")
        if not (-(2**63) <= value < 2**63):
            raise IndependentEncodeError("int out of range")
        return tag + struct.pack(">q", value)
    if field_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise IndependentEncodeError("decimal requires str/int")
        try:
            dec = Decimal(str(value))
        except InvalidOperation as exc:
            raise IndependentEncodeError("bad decimal") from exc
        if not dec.is_finite():
            raise IndependentEncodeError("decimal not finite")
        sign, digits, exponent = dec.as_tuple()
        coeff = 0
        for d in digits:
            coeff = coeff * 10 + d
        if sign:
            coeff = -coeff
        if not (-1_000_000 <= exponent <= 0) or not (-(2**63) <= coeff < 2**63):
            raise IndependentEncodeError("decimal out of supported range")
        return tag + struct.pack(">qi", coeff, -exponent)
    if field_type == "bool":
        if not isinstance(value, bool):
            raise IndependentEncodeError("bool requires bool")
        return tag + (b"\x01" if value else b"\x00")
    if field_type == "date":
        if not isinstance(value, str):
            raise IndependentEncodeError("date requires ISO str")
        try:
            d = _dt.date.fromisoformat(value)
        except ValueError as exc:
            raise IndependentEncodeError("bad ISO date") from exc
        return tag + struct.pack(">hhh", d.year, d.month, d.day)
    if field_type == "timestamp":
        if not isinstance(value, str):
            raise IndependentEncodeError("timestamp requires ISO str")
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            m = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise IndependentEncodeError("bad ISO timestamp") from exc
        if m.tzinfo is None or m.utcoffset() is None:
            raise IndependentEncodeError("timestamp must be timezone-aware")
        micros = int((m.astimezone(_dt.timezone.utc) -
                      _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc))
                     // _dt.timedelta(microseconds=1))
        return tag + struct.pack(">q", micros)
    raise IndependentEncodeError(f"unsupported type {field_type!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Independent commitment + Merkle formulas
# ---------------------------------------------------------------------------

def _u64(n: int) -> bytes:
    return struct.pack(">Q", n)


def field_commitment(record_index, field_position, field_name, encoded, salt):
    material = (
        COMMITMENT_DOMAIN
        + _lp(field_name.encode("utf-8"))
        + _u64(record_index)
        + _u64(field_position)
        + _lp(encoded)
        + _u64(len(salt))
        + salt
    )
    return hashlib.sha256(material).hexdigest()


def _merkle_leaf(commit_hex: str) -> bytes:
    return hashlib.sha256(
        MERKLE_DOMAIN + b"\x00" + _lp(bytes.fromhex(commit_hex))
    ).digest()


def _merkle_node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(MERKLE_DOMAIN + b"\x01" + _lp(left) + _lp(right)).digest()


EMPTY_ROOT = hashlib.sha256(MERKLE_DOMAIN + b"empty").hexdigest()


def fold_merkle_path(commit_hex: str, path: list[dict]) -> str:
    digest = _merkle_leaf(commit_hex)
    for step in path:
        side = step["side"]
        sib = bytes.fromhex(step["hash_hex"])
        if len(sib) != 32:
            raise ValueError("sibling must be 32 bytes")
        digest = _merkle_node(digest, sib) if side == "right" else _merkle_node(sib, digest)
    return digest.hex()


def root_from_commitments(commit_hexes: list[str]) -> str:
    if not commit_hexes:
        return EMPTY_ROOT
    level = [_merkle_leaf(c) for c in commit_hexes]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_merkle_node(level[i], level[i + 1]))
            else:
                nxt.append(_merkle_node(level[i], level[i]))
        level = nxt
    return level[0].hex()


# ---------------------------------------------------------------------------
# Package verification
# ---------------------------------------------------------------------------

@dataclass
class ItemResult:
    record_index: int
    field_name: str
    verdict: str
    detail: dict = field(default_factory=dict)


@dataclass
class VerifyReport:
    verdict: str
    root_hex: str | None
    recomputed_root_hex: str | None
    items: list[ItemResult]
    errors: list[str]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "valid": self.verdict == VerifyStatus.VALID,
            "root_hex": self.root_hex,
            "recomputed_root_hex": self.recomputed_root_hex,
            "items": [
                {"record_index": it.record_index, "field_name": it.field_name,
                 "verdict": it.verdict, "detail": it.detail}
                for it in self.items
            ],
            "errors": self.errors,
        }

    @property
    def is_valid(self) -> bool:
        return self.verdict == VerifyStatus.VALID


def _is_hex32(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == SHA256_HEX_LEN
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def verify_package(package: Any, *,
                   progress: Callable[[str], None] | None = None) -> VerifyReport:
    """Verify a disclosure package independently. No trusted state required."""
    say = progress or (lambda _msg: None)

    def malformed(msg: str) -> VerifyReport:
        say(f"FAIL malformed: {msg}")
        return VerifyReport(VerifyStatus.MALFORMED_PACKAGE, None, None, [], [msg])

    if not isinstance(package, dict):
        return malformed("package is not an object")
    if package.get("schema_version") != DISCLOSURE_VERSION:
        return malformed(f"unsupported schema_version: {package.get('schema_version')!r}")
    if package.get("commitment_schema_version") != "audit-commit-v1":
        return malformed("unsupported commitment_schema_version")
    if package.get("merkle_schema_version") != "audit-merkle-v1":
        return malformed("unsupported merkle_schema_version")

    root_hex = package.get("root_hex")
    if not _is_hex32(root_hex):
        return malformed("root_hex missing or not 32-byte hex")
    manifest = package.get("manifest")
    cells = manifest.get("cells") if isinstance(manifest, dict) else None
    items = package.get("disclosed")
    if not isinstance(cells, list) or not cells or not isinstance(items, list) or not items:
        return malformed("manifest.cells and disclosed must be non-empty lists")

    # ---- parse + bind the full cell set (detects insertion/reordering) ----
    parsed_cells: list[dict] = []
    seen_identities: set[tuple[int, str]] = set()
    seen_positions: set[tuple[int, int]] = set()
    for expected_index, cell in enumerate(cells):
        ident = (cell.get("record_index"), cell.get("field_name"))
        pos = (cell.get("record_index"), cell.get("field_position"))
        leaf_index = cell.get("leaf_index")
        commit = cell.get("commitment_hex")
        if (not isinstance(ident[0], int) or isinstance(ident[0], bool)
                or not isinstance(ident[1], str)
                or not isinstance(pos[1], int) or leaf_index != expected_index
                or not _is_hex32(commit)):
            return malformed(f"manifest cell {expected_index} malformed")
        if ident in seen_identities or pos in seen_positions:
            return VerifyReport(
                VerifyStatus.CELL_SET_MISMATCH, root_hex, None, [],
                [f"duplicate cell identity/position at {expected_index}"])
        seen_identities.add(ident)
        seen_positions.add(pos)
        parsed_cells.append({"record_index": ident[0], "field_name": ident[1],
                             "field_position": pos[1], "commitment_hex": commit.lower()})

    commitment_by_ident = {(c["record_index"], c["field_name"]): c["commitment_hex"]
                           for c in parsed_cells}

    # ---- verify each disclosed item against its claimed identity ----------
    item_reports: list[ItemResult] = []
    recomputed_commits: dict[tuple[int, str], str] = {}
    claimed_by_ident: dict[tuple[int, str], str] = {}
    disclosed_idents: set[tuple[int, str]] = set()

    for item in items:
        ident = _item_identity(item)
        if ident is None:
            return malformed("disclosed item missing record_index/field_name")
        if ident in disclosed_idents:
            return VerifyReport(
                VerifyStatus.CELL_SET_MISMATCH, root_hex, None, item_reports,
                [f"duplicate disclosed item {ident}"])
        disclosed_idents.add(ident)

        claimed_commit = item.get("commitment_hex")
        path = item.get("merkle_path")
        if not _is_hex32(claimed_commit) or not isinstance(path, list):
            return malformed(f"item {ident} has bad commitment or path")
        if commitment_by_ident.get(ident) != claimed_commit.lower():
            item_reports.append(ItemResult(ident[0], ident[1],
                                           VerifyStatus.FIELD_IDENTITY_MISMATCH,
                                           {"reason": "item identity not bound in manifest"}))
            say(f"FAIL {ident}: identity not bound in manifest")
            continue

        try:
            salt = bytes.fromhex(item.get("salt_hex", "")) if item.get("salt_hex") else b""
            encoded = _encode_value(item["field_type"], item.get("value"),
                                    item.get("state", "present"))
            recomputed = field_commitment(
                item["record_index"], item["field_position"], item["field_name"],
                encoded, salt)
        except (IndependentEncodeError, ValueError, KeyError, TypeError) as exc:
            item_reports.append(ItemResult(ident[0], ident[1],
                                           VerifyStatus.MALFORMED_PACKAGE,
                                           {"reason": str(exc)}))
            say(f"FAIL {ident}: malformed ({exc})")
            continue

        claimed_by_ident[ident] = claimed_commit.lower()
        if recomputed != claimed_commit.lower():
            recomputed_commits[ident] = recomputed
            item_reports.append(ItemResult(
                ident[0], ident[1], VerifyStatus.COMMITMENT_MISMATCH,
                {"claimed_commitment_hex": claimed_commit.lower(),
                 "recomputed_commitment_hex": recomputed}))
            say(f"FAIL {ident}: commitment mismatch")
            continue

        try:
            folded = fold_merkle_path(claimed_commit.lower(), path)
        except (ValueError, KeyError, TypeError) as exc:
            item_reports.append(ItemResult(ident[0], ident[1],
                                           VerifyStatus.MALFORMED_PACKAGE,
                                           {"reason": f"bad path: {exc}"}))
            continue
        if folded != root_hex.lower():
            item_reports.append(ItemResult(
                ident[0], ident[1], VerifyStatus.PROOF_INVALID,
                {"folded_root_hex": folded, "claimed_root_hex": root_hex.lower()}))
            say(f"FAIL {ident}: proof does not reach root")
            continue

        item_reports.append(ItemResult(ident[0], ident[1], VerifyStatus.VALID,
                                       {"commitment_hex": recomputed}))
        say(f"OK   {ident}: commitment + merkle path verified")

    # ---- recompute the whole-tree root from the complete manifest --------
    recomputed_root = root_from_commitments(
        [c["commitment_hex"] for c in parsed_cells])
    overall = _overall_verdict(item_reports)
    if overall == VerifyStatus.VALID and recomputed_root != root_hex.lower():
        say(f"FAIL root: manifest root {recomputed_root} != {root_hex.lower()}")
        return VerifyReport(VerifyStatus.ROOT_MISMATCH, root_hex.lower(), recomputed_root,
                            item_reports,
                            ["recomputed whole-tree root does not match package root"])
    if overall != VerifyStatus.VALID:
        return VerifyReport(overall, root_hex.lower(), recomputed_root, item_reports,
                            ["one or more disclosed items failed"])
    say(f"OK   package valid; root {recomputed_root}")
    return VerifyReport(VerifyStatus.VALID, root_hex.lower(), recomputed_root,
                        item_reports, [])


def _item_identity(item: Any) -> tuple[int, str] | None:
    if not isinstance(item, dict):
        return None
    ri = item.get("record_index")
    name = item.get("field_name")
    if isinstance(ri, int) and not isinstance(ri, bool) and isinstance(name, str):
        return ri, name
    return None


def _overall_verdict(items: list[ItemResult]) -> str:
    if any(it.verdict == VerifyStatus.MALFORMED_PACKAGE for it in items):
        return VerifyStatus.MALFORMED_PACKAGE
    if any(it.verdict == VerifyStatus.FIELD_IDENTITY_MISMATCH for it in items):
        return VerifyStatus.FIELD_IDENTITY_MISMATCH
    if any(it.verdict == VerifyStatus.COMMITMENT_MISMATCH for it in items):
        return VerifyStatus.COMMITMENT_MISMATCH
    if any(it.verdict == VerifyStatus.PROOF_INVALID for it in items):
        return VerifyStatus.PROOF_INVALID
    return VerifyStatus.VALID


# ---------------------------------------------------------------------------
# Reference-assisted per-item diagnosis
# ---------------------------------------------------------------------------

def classify_item(item: Any, reference: dict) -> str:
    """Classify a commitment failure using the trusted reference commitment.

    ``reference`` = {"record_index", "field_position", "field_name",
                     "commitment_hex", "field_type", "state"}

    Priority: identity tampering > encoding/value tampering > salt tampering.
    """
    if not isinstance(item, dict):
        return VerifyStatus.MALFORMED_PACKAGE
    identity_changed = (
        item.get("record_index") != reference["record_index"]
        or item.get("field_name") != reference["field_name"]
        or item.get("field_position") != reference["field_position"]
    )
    if identity_changed:
        return VerifyStatus.FIELD_IDENTITY_MISMATCH
    try:
        salt = bytes.fromhex(item["salt_hex"]) if item.get("salt_hex") else b""
        encoded = _encode_value(item["field_type"], item.get("value"),
                                item.get("state", "present"))
        recomputed_with_claim = field_commitment(
            item["record_index"], item["field_position"], item["field_name"],
            encoded, salt)
    except (IndependentEncodeError, ValueError, KeyError, TypeError):
        return VerifyStatus.MALFORMED_PACKAGE

    if recomputed_with_claim == reference["commitment_hex"]:
        return VerifyStatus.VALID

    # Recompute with the reference salt and with the reference encoded value
    # to localise the tampering.
    ref_salt = bytes.fromhex(reference.get("salt_hex", "")) if reference.get("salt_hex") else b""
    with_ref_salt = field_commitment(
        item["record_index"], item["field_position"], item["field_name"],
        encoded, ref_salt)
    if with_ref_salt == reference["commitment_hex"]:
        return VerifyStatus.WRONG_SALT
    return VerifyStatus.WRONG_VALUE


def verify_json_file(path: str) -> VerifyReport:
    with open(path, "r", encoding="utf-8") as fh:
        package = json.load(fh)
    return verify_package(package)
