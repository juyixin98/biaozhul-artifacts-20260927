"""Stand-alone proof verifier.

This verifier recomputes the root from the proof alone — it never opens the
node store and never calls back into the code that produced the proof.  It
serves as the in-process independent checker; ``independent_tests/`` contains
a second, separately written verifier used by the test suite.

Verification results are a verdict plus a reason category, never a bare
bool, so callers (and logs) can tell *why* something was rejected.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..crypto.encoding import HASH_BYTES, KEY_BITS, bit_at
from ..crypto.hashing import empty_at, hash_branch, hash_leaf


class Verdict(str, enum.Enum):
    VALID = "valid"                      # proof accepted
    MALFORMED = "malformed"              # structure cannot even be parsed
    KEY_MISMATCH = "key_mismatch"        # membership terminal binds another key
    PREFIX_MISMATCH = "prefix_mismatch"  # non-membership leaf on a different path
    TERMINAL_INVALID = "terminal_invalid"  # exists flag contradicts terminal
    STEP_INVALID = "step_invalid"        # bad depth/run/bits while expanding
    ROOT_MISMATCH = "root_mismatch"      # recomputed root != claimed root


# Reasons are stable strings; the mapping above is exhaustive by construction.
_FAILURE_CATEGORIES = {v.value for v in Verdict if v is not Verdict.VALID}


@dataclass(frozen=True)
class VerificationResult:
    verdict: Verdict
    reason: str

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.VALID


def _err(verdict: Verdict, reason: str) -> VerificationResult:
    return VerificationResult(verdict, reason)


@dataclass(frozen=True)
class _ExpandedStep:
    depth: int
    bit: int
    sibling: bytes


def _is_32_hex(s: str) -> bool:
    if not isinstance(s, str) or len(s) != HASH_BYTES * 2:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def _expand_steps(proof: dict, queried_key: bytes, terminal_depth: int) -> Tuple[Optional[List[_ExpandedStep]], Optional[VerificationResult]]:
    raw = proof.get("steps")
    if not isinstance(raw, list):
        return None, _err(Verdict.MALFORMED, "steps must be a list")

    expanded: List[_ExpandedStep] = []
    covered = 0
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or entry.get("kind") not in ("sibling", "empty_run"):
            return None, _err(Verdict.MALFORMED, f"step[{index}] has unknown kind")
        depth = entry.get("depth")
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
            return None, _err(Verdict.STEP_INVALID, f"step[{index}] bad depth")

        kind = entry["kind"]
        if kind == "sibling":
            if depth != covered:
                return None, _err(Verdict.STEP_INVALID, f"step[{index}] gap/overlap at depth {depth}")
            sib_hex = entry.get("sibling_hash")
            if not _is_32_hex(sib_hex):
                return None, _err(Verdict.MALFORMED, f"step[{index}] sibling_hash must be 32-byte hex")
            if len(entry) != 3:
                return None, _err(Verdict.MALFORMED, f"step[{index}] has unexpected fields")
            expanded.append(_ExpandedStep(depth, bit_at(queried_key, depth), bytes.fromhex(sib_hex)))
            covered += 1
        else:
            length = entry.get("length")
            if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
                return None, _err(Verdict.STEP_INVALID, f"step[{index}] run length must be positive int")
            if depth != covered:
                return None, _err(Verdict.STEP_INVALID, f"step[{index}] gap/overlap at depth {depth}")
            if len(entry) != 3:
                return None, _err(Verdict.MALFORMED, f"step[{index}] has unexpected fields")
            for offset in range(length):
                d = depth + offset
                # A run asserts: at every covered level the off-path subtree
                # is the empty subtree rooted one level below.
                expanded.append(_ExpandedStep(d, bit_at(queried_key, d), empty_at(d + 1)))
            covered += length

    if covered != terminal_depth:
        return None, _err(
            Verdict.STEP_INVALID,
            f"steps cover depth 0..{covered - 1} but terminal is at {terminal_depth}",
        )
    if terminal_depth > KEY_BITS:
        return None, _err(Verdict.STEP_INVALID, "terminal_depth beyond key width")
    return expanded, None


def verify_proof(proof: dict) -> VerificationResult:
    """Verify a serialized proof dict.  Pure: only hashes + the proof itself."""
    if not isinstance(proof, dict):
        return _err(Verdict.MALFORMED, "proof must be an object")
    if proof.get("version") != "smt-v1":
        return _err(Verdict.MALFORMED, "unsupported proof version")

    root_hex = proof.get("root")
    key_hex = proof.get("key")
    if not _is_32_hex(root_hex):
        return _err(Verdict.MALFORMED, "root must be 32-byte hex")
    if not _is_32_hex(key_hex):
        return _err(Verdict.MALFORMED, "key must be 32-byte hex")
    claimed_root = bytes.fromhex(root_hex)
    queried_key = bytes.fromhex(key_hex)

    exists = proof.get("exists")
    if not isinstance(exists, bool):
        return _err(Verdict.MALFORMED, "exists must be boolean")

    terminal_depth = proof.get("terminal_depth")
    if not isinstance(terminal_depth, int) or isinstance(terminal_depth, bool):
        return _err(Verdict.MALFORMED, "terminal_depth must be int")
    if not 0 <= terminal_depth <= KEY_BITS:
        return _err(Verdict.STEP_INVALID, "terminal_depth out of range")

    terminal = proof.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("kind") not in ("leaf", "empty"):
        return _err(Verdict.MALFORMED, "terminal kind must be leaf or empty")

    # ----- terminal commitment -----
    if terminal["kind"] == "empty":
        if exists:
            return _err(Verdict.TERMINAL_INVALID, "exists=true but terminal is empty")
        if len(terminal) != 1:
            return _err(Verdict.MALFORMED, "empty terminal has unexpected fields")
        current = empty_at(terminal_depth)
    else:
        tkey_hex = terminal.get("key")
        tval_hex = terminal.get("value")
        if not _is_32_hex(tkey_hex) or not isinstance(tval_hex, str):
            return _err(Verdict.MALFORMED, "leaf terminal needs 32-byte key and hex value")
        try:
            tvalue = bytes.fromhex(tval_hex)
        except ValueError:
            return _err(Verdict.MALFORMED, "leaf value is not hex")
        if len(terminal) != 3:
            return _err(Verdict.MALFORMED, "leaf terminal has unexpected fields")
        tkey = bytes.fromhex(tkey_hex)
        if exists:
            if tkey != queried_key:
                return _err(Verdict.KEY_MISMATCH, "membership proof terminal binds a different key")
        else:
            if tkey == queried_key:
                return _err(
                    Verdict.PREFIX_MISMATCH,
                    "non-membership terminal leaf carries the queried key",
                )
            # The diverging leaf must share the queried key's path for the
            # first terminal_depth bits, otherwise it cannot witness absence.
            for d in range(terminal_depth):
                if bit_at(tkey, d) != bit_at(queried_key, d):
                    return _err(
                        Verdict.PREFIX_MISMATCH,
                        f"terminal leaf diverges at depth {d}, before claimed terminal {terminal_depth}",
                    )
        current = hash_leaf(tkey, tvalue)

    # ----- expand compressed path (must be expandable) -----
    steps, bad = _expand_steps(proof, queried_key, terminal_depth)
    if bad is not None:
        return bad

    # ----- fold back to the root (proof binds root + key + depth) -----
    for step in reversed(steps):
        if step.bit == 0:
            current = hash_branch(current, step.sibling)
        else:
            current = hash_branch(step.sibling, current)

    if current != claimed_root:
        return _err(Verdict.ROOT_MISMATCH, "recomputed root does not match claimed root")
    return VerificationResult(Verdict.VALID, "accepted")
