"""Independent reference verifier -- STANDARD LIBRARY ONLY.

This module deliberately imports nothing from ``app`` and uses only
``hashlib``/``hmac``/``re``/``datetime`` from the Python standard library. It
exists so the test suite has a second, independently written implementation of
the protocol whose answers are not produced by the code under test.

The wire constants (labels, type names, framing) are duplicated here on
purpose: if someone changes a label in one implementation, cross-checks fail
instead of silently agreeing.

The public entry point :func:`independent_verify` returns
``(ok, category, reason, steps)`` where category strings intentionally match
the service's public failure categories.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re

PROTOCOL = "audit-commit-v1"
TYPES = {"text", "int", "bool", "decimal", "date", "timestamp"}
STATES = {"present", "null", "missing"}

_LBL_FIELD = f"{PROTOCOL}|field-commitment"
_LBL_FIELD_LEAF = f"{PROTOCOL}|field-tree-leaf"
_LBL_FIELD_NODE = f"{PROTOCOL}|field-tree-node"
_LBL_FIELD_EMPTY = f"{PROTOCOL}|field-tree-empty"
_LBL_RECORD_LEAF = f"{PROTOCOL}|record-tree-leaf"
_LBL_RECORD_NODE = f"{PROTOCOL}|record-tree-node"
_LBL_RECORD_EMPTY = f"{PROTOCOL}|record-tree-empty"

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class IVError(Exception):
    def __init__(self, category: str, reason: str):
        super().__init__(reason)
        self.category = category


def _H(label: str, algo: str, *parts: bytes) -> bytes:
    tag = label.encode()
    buf = len(tag).to_bytes(2, "big") + tag + len(parts).to_bytes(2, "big")
    for p in parts:
        buf += len(p).to_bytes(2, "big") + p
    return hashlib.new(algo, buf).digest()


def _unhex(name: str, v) -> bytes:
    if not isinstance(v, str):
        raise IVError("PROOF_MALFORMED", f"{name} must be hex string")
    try:
        return bytes.fromhex(v)
    except ValueError:
        raise IVError("PROOF_MALFORMED", f"{name} is not hex")


def _encode_present(ftype: str, value):
    if ftype == "text":
        if not isinstance(value, str):
            raise IVError("TYPE_ENCODING_ERROR", "text needs string")
        return value.encode()
    if ftype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise IVError("TYPE_ENCODING_ERROR", "int needs JSON integer")
        return str(value).encode()
    if ftype == "bool":
        if not isinstance(value, bool):
            raise IVError("TYPE_ENCODING_ERROR", "bool needs boolean")
        return b"1" if value else b"0"
    if ftype == "decimal":
        if isinstance(value, bool) or isinstance(value, float):
            raise IVError("TYPE_ENCODING_ERROR", "decimal needs string")
        if isinstance(value, int):
            return str(value).encode()
        if not isinstance(value, str):
            raise IVError("TYPE_ENCODING_ERROR", "decimal needs string")
        s = value.strip()
        if not re.fullmatch(r"[+-]?(\d+\.\d+|\d+|\.\d+)", s):
            raise IVError(
                "TYPE_ENCODING_ERROR", "decimal must be fixed-point literal"
            )
        sign, body = ("", s)
        if body[0] in "+-":
            sign, body = ("-" if body[0] == "-" else ""), body[1:]
        ip, _, fp = body.partition(".")
        ip = ip.lstrip("0") or "0"
        fp = fp.rstrip("0")
        if ip == "0" and not fp:
            sign = ""
        return (sign + ip + (("." + fp) if fp else "")).encode()
    if ftype == "date":
        if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
            raise IVError("TYPE_ENCODING_ERROR", "date needs YYYY-MM-DD")
        try:
            d = _dt.date.fromisoformat(value)
        except ValueError:
            raise IVError("TYPE_ENCODING_ERROR", "date not real")
        return d.isoformat().encode()
    if ftype == "timestamp":
        if not isinstance(value, str):
            raise IVError("TYPE_ENCODING_ERROR", "timestamp needs string")
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise IVError("TYPE_ENCODING_ERROR", "timestamp not ISO8601")
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise IVError("TYPE_ENCODING_ERROR", "timestamp needs timezone")
        u = dt.astimezone(_dt.timezone.utc)
        frac = "" if u.microsecond == 0 else f".{u.microsecond:06d}".rstrip("0")
        return (u.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z").encode()
    raise IVError("TYPE_ENCODING_ERROR", f"unknown type {ftype}")


def _field_commitment(algo, batch_id, rec_idx, pos, path, ftype, state, value, salt):
    parts = [
        batch_id.encode(),
        int(rec_idx).to_bytes(8, "big"),
        int(pos).to_bytes(8, "big"),
        path.encode(),
        ftype.encode(),
        state.encode(),
    ]
    if state == "present":
        parts.append(_encode_present(ftype, value))
        parts.append(salt if salt is not None else b"")
        if salt is None:
            raise IVError("IDENTITY_MISMATCH", "present needs salt")
    elif state == "null":
        parts.append(b"")
        parts.append(salt if salt is not None else b"")
    else:
        parts.append(b"")
        parts.append(b"")
    return _H(_LBL_FIELD, algo, *parts)


def _leaf(label, algo, idx, count, node):
    return _H(
        label, algo, int(idx).to_bytes(8, "big"), int(count).to_bytes(8, "big"), node
    )


def _node(label, algo, left, right):
    return _H(label, algo, left, right)


def _verify_path(kind, algo, index, count, leaf, siblings, root):
    leaf_l, node_l, _ = {
        "field": (_LBL_FIELD_LEAF, _LBL_FIELD_NODE, _LBL_FIELD_EMPTY),
        "record": (_LBL_RECORD_LEAF, _LBL_RECORD_NODE, _LBL_RECORD_EMPTY),
    }[kind]
    if index < 0 or count <= 0 or index >= count:
        raise IVError("IDENTITY_MISMATCH", f"{kind} index out of range")
    expected = (count - 1).bit_length()
    if len(siblings) != expected:
        raise IVError(
            "MERKLE_PATH_MISMATCH",
            f"{kind} path length {len(siblings)} != {expected}",
        )
    cur = _leaf(leaf_l, algo, index, count, leaf)
    pos, remaining = index, count
    for lvl, sib in enumerate(siblings):
        if pos % 2 == 0:
            padded = pos + 1 >= remaining
            if padded:
                if sib is not None:
                    raise IVError(
                        "MERKLE_PATH_MISMATCH", f"{kind} lvl{lvl} null marker required"
                    )
                cur = _node(node_l, algo, cur, cur)
            else:
                if sib is None:
                    raise IVError(
                        "MERKLE_PATH_MISMATCH", f"{kind} lvl{lvl} missing sibling"
                    )
                cur = _node(node_l, algo, cur, sib)
        else:
            if sib is None:
                raise IVError(
                    "MERKLE_PATH_MISMATCH", f"{kind} lvl{lvl} needs left sibling"
                )
            cur = _node(node_l, algo, sib, cur)
        pos //= 2
        remaining = (remaining + 1) // 2
    if not hmac.compare_digest(cur, root):
        raise IVError(
            "MERKLE_PATH_MISMATCH",
            f"{kind} root mismatch {cur.hex()[:12]} vs {root.hex()[:12]}",
        )


def _siblings(raw):
    if not isinstance(raw, list):
        raise IVError("PROOF_MALFORMED", "siblings not a list")
    return [None if x is None else _unhex("sibling", x) for x in raw]


def independent_verify(proof, trusted_root_hex, expected_path=None, expected_record=None):
    """Return ``(ok, category_or_None, reason, steps_list)``."""
    steps = []
    try:
        trusted = _unhex("trusted_root", trusted_root_hex)
        steps.append("trusted_root_decoded")
        if not isinstance(proof, dict):
            raise IVError("PROOF_MALFORMED", "proof not object")
        if proof.get("protocol_version") != PROTOCOL:
            raise IVError("PROOF_MALFORMED", "bad protocol version")
        algo = proof.get("digest")
        if algo not in ("sha256", "sha384", "sha512"):
            raise IVError("PROOF_MALFORMED", "bad digest")
        proof_root = _unhex("batch_root_hex", proof.get("batch_root_hex"))
        if len(proof_root) != len(trusted):
            raise IVError("ROOT_MISMATCH", "root length differs")
        steps.append("root_decoded")

        claim = proof.get("claim")
        ft = proof.get("field_tree")
        rt = proof.get("record_tree")
        if not (isinstance(claim, dict) and isinstance(ft, dict) and isinstance(rt, dict)):
            raise IVError("PROOF_MALFORMED", "missing proof sections")
        ri, pos = claim.get("record_index"), claim.get("position")
        path, ftype, state = claim.get("path"), claim.get("type"), claim.get("state")
        # Accept the service wire name field_type as well as a short alias.
        if ftype is None:
            ftype = claim.get("field_type")
        claimed_commit = _unhex("claim.commitment_hex", claim.get("commitment_hex"))
        if (
            isinstance(ri, bool) or not isinstance(ri, int)
            or isinstance(pos, bool) or not isinstance(pos, int)
            or not isinstance(path, str) or not path
            or ftype not in TYPES or state not in STATES
            or ri < 0 or pos < 0
        ):
            raise IVError("PROOF_MALFORMED", "malformed claim identity")
        if expected_path is not None and path != expected_path:
            raise IVError("IDENTITY_MISMATCH", "path differs from requested")
        if expected_record is not None and ri != expected_record:
            raise IVError("IDENTITY_MISMATCH", "record differs from requested")
        steps.append("identity_checked")

        fc, rc = ft.get("leaf_count"), rt.get("leaf_count")
        if (
            fc != proof.get("field_count") or rc != proof.get("record_count")
            or not isinstance(fc, int) or not isinstance(rc, int)
            or fc <= 0 or rc <= 0 or pos >= fc or ri >= rc
        ):
            raise IVError("IDENTITY_MISMATCH", "count/index inconsistency")
        steps.append("bounds_checked")

        reveal = proof.get("reveal", "ABSENT")
        if state == "present":
            if not isinstance(reveal, dict):
                raise IVError("PROOF_MALFORMED", "present needs reveal")
            value = reveal.get("value")
            salt = _unhex("salt", reveal.get("salt_hex"))
        elif state == "null":
            if not isinstance(reveal, dict) or reveal.get("value") is not None:
                raise IVError("COMMITMENT_MISMATCH", "null reveal wrong")
            sh = reveal.get("salt_hex")
            value, salt = None, (_unhex("salt", sh) if sh else None)
        else:
            if reveal != "ABSENT" and reveal is not None:
                raise IVError("PROOF_MALFORMED", "missing must not reveal")
            value, salt = None, None
        steps.append("reveal_decoded")

        commit = _field_commitment(
            algo, proof["batch_id"], ri, pos, path, ftype, state, value, salt
        )
        if not hmac.compare_digest(commit, claimed_commit):
            raise IVError(
                "COMMITMENT_MISMATCH",
                "value/salt/identity do not hash to claimed commitment",
            )
        steps.append("commitment_matches")
        record_root = _unhex("record_root", ft.get("record_root_hex"))
        _verify_path("field", algo, pos, fc, claimed_commit, _siblings(ft.get("siblings_hex")), record_root)
        steps.append("field_path_ok")
        _verify_path(
            "record", algo, ri, rc, record_root, _siblings(rt.get("siblings_hex")), proof_root
        )
        steps.append("record_path_ok")
        if not hmac.compare_digest(proof_root, trusted):
            raise IVError(
                "ROOT_MISMATCH", "embedded root is not the trusted batch root"
            )
        steps.append("root_accepted")
        return True, None, "accepted by independent verifier", steps
    except IVError as e:
        return False, e.category, str(e), steps
    except Exception as e:  # explicit: unknown failure is never an accept
        return False, "INTERNAL_ERROR", f"independent verifier fault: {e!r}", steps
