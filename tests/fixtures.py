"""Hand-authored synthetic fixtures.

CRITICAL: every expected answer in this file is written out as an explicit
string literal (or constructed with trivial Python concatenation of the
author's own fixture lines).  It is **never** derived from the merge engine
under test — tests would otherwise only assert that the implementation
agrees with itself.  Each case documents what the human author expects and
which acceptance rule the expectation comes from.

Fixture texts deliberately include:

* moved similar paragraphs (``p1..p6``) that tempt a naive aligner to match
  the wrong occurrence;
* repeated lines (``r r r r``) that defeat unique-anchor alignment;
* same-point inserts from both sides;
* delete-vs-modify contention;
* mixed / missing terminators so a silent EOL rewrite would be caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MergeCase:
    case_id: str
    description: str
    base: str
    local: str
    remote: str
    #: exact expected automatic result when no conflict exists, else None
    expected_auto: Optional[str]
    #: list of expected conflict type values in order; empty for auto cases
    expected_conflict_types: tuple[str, ...] = ()
    #: {choice: exact rebuilt text} for the first conflict, where meaningful
    expected_resolutions: Optional[dict[str, str]] = None


# --------------------------------------------------------------------------- #
# 1. Disjoint edits + moved similar paragraphs                                 #
# --------------------------------------------------------------------------- #

MOVED_BASE = "p1\np2\np3\np4\np5\np6\n"
# Local swaps the two similar leading paragraphs.
MOVED_LOCAL = "p2\np1\np3\np4\np5\np6\n"
# Remote edits a paragraph far away (p6) and adds a brand-new line at the end.
MOVED_REMOTE = "p1\np2\np3\np4\np5\nP6SIX\ntail\n"
# Expected: the move (p2,p1) AND remote's independent edit both survive,
# with remote's "tail" after P6SIX.  No conflict: the changed regions are
# disjoint.  Written by hand.
MOVED_EXPECTED = "p2\np1\np3\np4\np5\nP6SIX\ntail\n"

CASE_MOVE_DISJOINT = MergeCase(
    "move_disjoint",
    "local moves two similar paragraphs; remote edits an unrelated paragraph",
    MOVED_BASE, MOVED_LOCAL, MOVED_REMOTE,
    expected_auto=MOVED_EXPECTED,
)

# --------------------------------------------------------------------------- #
# 2. Repeated lines: both sides edit the *same logical occurrence* differently #
#    -> deterministic alignment must still flag the divergence rather than     #
#    silently scattering the edits across the run.                            #
# --------------------------------------------------------------------------- #

DUP_BASE = "r\nr\nr\nr\n"
DUP_LOCAL = "r\nL1\nr\nr\n"     # change occurrence #2 (line index 1)
DUP_REMOTE = "r\nr\nR1\nr\n"    # change occurrence #3 (line index 2)

CASE_DUP_DISJOINT = MergeCase(
    "duplicate_lines_disjoint",
    "two distinct occurrences inside a run of identical lines, edited "
    "separately: positions are disjoint and both edits apply",
    DUP_BASE, DUP_LOCAL, DUP_REMOTE,
    # Occurrence positions are known to the human author; Myers aligns the
    # run deterministically (delete-before-insert tie break), yielding both
    # edits.  Expected output written literally:
    expected_auto="r\nL1\nR1\nr\n",
)

DUP_SAME_BASE = "r\nr\nr\nr\n"
DUP_SAME_LOCAL = "r\nX\nr\nr\n"
DUP_SAME_REMOTE = "r\nY\nr\nr\n"

CASE_DUP_SAME = MergeCase(
    "duplicate_lines_same_spot",
    "both sides replace the same occurrence in an identical-line run with "
    "different content -> divergent_modify conflict, not a silent pick",
    DUP_SAME_BASE, DUP_SAME_LOCAL, DUP_SAME_REMOTE,
    expected_auto=None,
    expected_conflict_types=("divergent_modify",),
    expected_resolutions={"local": DUP_SAME_LOCAL, "remote": DUP_SAME_REMOTE,
                          "base": DUP_SAME_BASE},
)

# --------------------------------------------------------------------------- #
# 3. Same-point inserts                                                        #
# --------------------------------------------------------------------------- #

PT_BASE = "h1\nh2\nh3\n"
PT_LOCAL_SAME = "INS-L\nh1\nh2\nh3\n"
PT_REMOTE_SAME = "INS-R\nh1\nh2\nh3\n"
PT_LOCAL_OTHER = "h1\nh2\nh3\nEND\n"   # insert at a different point (end)

CASE_SAME_POINT_CONFLICT = MergeCase(
    "same_point_insert_conflict",
    "both sides insert different lines at the same top boundary",
    PT_BASE, PT_LOCAL_SAME, PT_REMOTE_SAME,
    expected_auto=None,
    expected_conflict_types=("same_point_insert",),
    expected_resolutions={
        "local": PT_LOCAL_SAME,
        "remote": PT_REMOTE_SAME,
        "base": PT_BASE,
        "local_then_remote": "INS-L\nINS-R\nh1\nh2\nh3\n",
        "remote_then_local": "INS-R\nINS-L\nh1\nh2\nh3\n",
    },
)

CASE_SAME_POINT_DISJOINT = MergeCase(
    "point_inserts_different_boundaries",
    "local inserts at top boundary, remote at end boundary: disjoint -> both",
    PT_BASE, PT_LOCAL_SAME, PT_LOCAL_OTHER,
    expected_auto="INS-L\nh1\nh2\nh3\nEND\n",
)

CASE_SAME_POINT_IDENTICAL = MergeCase(
    "same_point_insert_identical",
    "both insert the same line at the same point: taken exactly once",
    PT_BASE, PT_LOCAL_SAME, PT_LOCAL_SAME,
    expected_auto=PT_LOCAL_SAME,
)

# --------------------------------------------------------------------------- #
# 4. Delete vs modify contention                                               #
# --------------------------------------------------------------------------- #

DM_BASE = "alpha\nbeta\ngamma\ndelta\n"
DM_LOCAL_DELETE = "alpha\ngamma\ndelta\n"            # deletes beta
DM_REMOTE_MODIFY = "alpha\nBETA!\ngamma\ndelta\n"   # changes beta

CASE_DELETE_MODIFY = MergeCase(
    "delete_modify",
    "local deletes beta while remote changes it -> delete_modify conflict",
    DM_BASE, DM_LOCAL_DELETE, DM_REMOTE_MODIFY,
    expected_auto=None,
    expected_conflict_types=("delete_modify",),
    expected_resolutions={
        "local": DM_LOCAL_DELETE,     # deletion wins
        "remote": DM_REMOTE_MODIFY,   # modification wins
        "base": DM_BASE,              # revert the region
    },
)

DM_REMOTE_DELETE = "alpha\ngamma\ndelta\n"
DM_LOCAL_MODIFY = "alpha\nbeta-beta\ngamma\ndelta\n"

CASE_DELETE_MODIFY_REVERSED = MergeCase(
    "delete_modify_other_side",
    "remote deletes beta while local changes it (mirror of above)",
    DM_BASE, DM_LOCAL_MODIFY, DM_REMOTE_DELETE,
    expected_auto=None,
    expected_conflict_types=("delete_modify",),
    expected_resolutions={"local": DM_LOCAL_MODIFY,
                          "remote": DM_REMOTE_DELETE, "base": DM_BASE},
)

# --------------------------------------------------------------------------- #
# 5. Terminator integrity: CRLF / LF / CR / no trailing newline                #
# --------------------------------------------------------------------------- #

EOL_BASE = "one\r\ntwo\r\nthree\r\n"
EOL_LOCAL = "one\r\nTWO\r\nthree\r\n"
EOL_REMOTE = "one\r\ntwo\r\nthree\r\nfour\r\n"
EOL_EXPECTED = "one\r\nTWO\r\nthree\r\nfour\r\n"

CASE_CRLF_PRESERVED = MergeCase(
    "crlf_preserved",
    "all CRLF; merge must keep CRLF everywhere (no silent LF conversion)",
    EOL_BASE, EOL_LOCAL, EOL_REMOTE,
    expected_auto=EOL_EXPECTED,
)

NOTAIL_BASE = "a\nb\nc"
NOTAIL_LOCAL = "a\nb\nc\nd"       # append a new last line, still no newline
NOTAIL_REMOTE = "A\nb\nc"         # edit the first line, still no newline
NOTAIL_EXPECTED = "A\nb\nc\nd"

CASE_NO_TRAILING_NEWLINE = MergeCase(
    "no_trailing_newline_preserved",
    "none of the documents end with a newline; disjoint edits, result must "
    "not gain a trailing newline",
    NOTAIL_BASE, NOTAIL_LOCAL, NOTAIL_REMOTE,
    expected_auto=NOTAIL_EXPECTED,
)

# Trailing-newline contention: one side changes the terminator of the last
# line while the other edits that same line; both rewrite the identical
# unterminated span, so the core reports divergence instead of guessing.
TAIL_BASE = "a\nb\nc"
TAIL_LOCAL = "a\nb\nC"          # edit the unterminated last line, no newline
TAIL_REMOTE = "a\nb\nCextra"    # append into the same unterminated last line
CASE_TRAILING_NEWLINE_CONTENTION = MergeCase(
    "trailing_last_line_contention",
    "both sides rewrite the identical unterminated final span differently "
    "(no trailing newline anywhere); the core must flag divergence, never "
    "silently synthesize a newline to split the difference",
    TAIL_BASE, TAIL_LOCAL, TAIL_REMOTE,
    expected_auto=None,
    expected_conflict_types=("divergent_modify",),
    expected_resolutions={"local": TAIL_LOCAL, "remote": TAIL_REMOTE,
                          "base": TAIL_BASE},
)

MIXED_BASE = "u\nv\r\nw\n"
MIXED_LOCAL = "U\nv\r\nw\n"
MIXED_REMOTE = "u\nv\r\nw\nz\r\n"
MIXED_EXPECTED = "U\nv\r\nw\nz\r\n"

CASE_MIXED_EOL_PRESERVED = MergeCase(
    "mixed_eol_preserved",
    "mixed LF and CRLF in one document; each terminator keeps its form",
    MIXED_BASE, MIXED_LOCAL, MIXED_REMOTE,
    expected_auto=MIXED_EXPECTED,
)

# --------------------------------------------------------------------------- #
# 6. Divergent modification and partial overlap                                #
# --------------------------------------------------------------------------- #

DIV_BASE = "l1\nl2\nl3\nl4\nl5\n"
DIV_LOCAL = "l1\nL2\nl3\nl4\nl5\n"
DIV_REMOTE = "l1\nR2\nl3\nl4\nl5\n"

CASE_DIVERGENT = MergeCase(
    "divergent_modify",
    "identical span replaced differently by each side",
    DIV_BASE, DIV_LOCAL, DIV_REMOTE,
    expected_auto=None,
    expected_conflict_types=("divergent_modify",),
    expected_resolutions={"local": DIV_LOCAL, "remote": DIV_REMOTE,
                          "base": DIV_BASE},
)

OVL_BASE = "l1\nl2\nl3\nl4\nl5\n"
OVL_LOCAL = "l1\nX\nY\nl4\nl5\n"   # replaces l2,l3
OVL_REMOTE = "l1\nl2\nZ\nl4\nl5\n"  # replaces l3 (overlaps half of local span)

CASE_PARTIAL_OVERLAP = MergeCase(
    "partial_overlap",
    "local changes lines [1,3), remote changes line 2: overlap without "
    "coinciding -> partial_overlap, no proportional splicing",
    OVL_BASE, OVL_LOCAL, OVL_REMOTE,
    expected_auto=None,
    expected_conflict_types=("partial_overlap",),
    expected_resolutions={"local": OVL_LOCAL, "remote": OVL_REMOTE,
                          "base": OVL_BASE},
)

# --------------------------------------------------------------------------- #
# 7. Insertion strictly inside a range the other side replaces                 #
# --------------------------------------------------------------------------- #

INR_BASE = "a\nb\nc\nd\n"
INR_LOCAL = "a\nB\nC\nd\n"            # replace b,c
INR_REMOTE = "a\nb\nINS\nc\nd\n"      # insert a line between b and c

CASE_INSERT_RANGE = MergeCase(
    "insert_strictly_inside_range",
    "remote inserts at a boundary strictly inside local's replaced span",
    INR_BASE, INR_LOCAL, INR_REMOTE,
    expected_auto=None,
    expected_conflict_types=("insert_range",),
    expected_resolutions={"local": INR_LOCAL, "remote": INR_REMOTE,
                          "base": INR_BASE},
)

# --------------------------------------------------------------------------- #
# 8. Independent-edit consistency (the "inverse" oracle property)              #
# --------------------------------------------------------------------------- #

# On a clean auto-merge, applying remote's edit to local (via a merge where
# the new base is local and remote stays remote) must reproduce the merged
# text, and symmetrically.  Expected text is the same MOVED_EXPECTED.


ALL_CASES: tuple[MergeCase, ...] = (
    CASE_MOVE_DISJOINT,
    CASE_DUP_DISJOINT,
    CASE_DUP_SAME,
    CASE_SAME_POINT_CONFLICT,
    CASE_SAME_POINT_DISJOINT,
    CASE_SAME_POINT_IDENTICAL,
    CASE_DELETE_MODIFY,
    CASE_DELETE_MODIFY_REVERSED,
    CASE_CRLF_PRESERVED,
    CASE_NO_TRAILING_NEWLINE,
    CASE_TRAILING_NEWLINE_CONTENTION,
    CASE_MIXED_EOL_PRESERVED,
    CASE_DIVERGENT,
    CASE_PARTIAL_OVERLAP,
    CASE_INSERT_RANGE,
)
