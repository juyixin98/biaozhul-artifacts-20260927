"""Synthetic local fixtures. No real accounts or business data."""

from __future__ import annotations

V = "2026-09-01"


def stmt(sid, effect, principal, action, resource, condition=None):
    s = {"Sid": sid, "Effect": effect, "Principal": principal,
         "Action": action, "Resource": resource}
    if condition:
        s["Condition"] = condition
    return s


def policy(*statements):
    return {"Version": V, "Statement": list(statements)}


# --- baseline pairs ----------------------------------------------------------
EMPTY = policy()  # everything default-denied

PHOTO_V1 = policy(stmt("read-photos", "Allow", ["alice"], ["s3:GetObject"], ["photos/*"]))
PHOTO_V2_EXPAND = policy(
    stmt("read-photos", "Allow", ["alice", "bob"], ["s3:GetObject"], ["photos/*"]),
)

# overlapping prefixes: new rule extends coverage deeper under an existing prefix
# AND opens a second, disjoint prefix.
OVERLAP_V1 = policy(stmt("a", "Allow", ["alice"], ["s3:GetObject"], ["photos/2026/"]))
OVERLAP_V2 = policy(
    stmt("a", "Allow", ["alice"], ["s3:GetObject"], ["photos/2026/"]),
    stmt("b", "Allow", ["alice"], ["s3:GetObject"], ["photos/2026/private/*"]),
    stmt("c", "Allow", ["alice"], ["s3:GetObject"], ["billing/*"]),
)

# negation condition: allow only departments != external; unknown dept is UNKNOWN.
NEG_V1 = EMPTY
NEG_V2 = policy(stmt(
    "not-external", "Allow", ["alice"], ["s3:GetObject"], ["docs/*"],
    {"StringNotEquals": {"department": ["external"]}},
))

# unknown key via negated Numeric: age unknown must not become "allowed"
NEG_NUM_V1 = EMPTY
NEG_NUM_V2 = policy(stmt(
    "under-limit", "Allow", ["alice"], ["s3:GetObject"], ["docs/*"],
    {"NumericLessThan": {"age": [18]}},
))

# IfExists variant: missing attribute is explicitly part of the hypothesis
IF_EXISTS_V1 = EMPTY
IF_EXISTS_V2 = policy(stmt(
    "mfa-or-nokey", "Allow", ["alice"], ["s3:GetObject"], ["docs/*"],
    {"BoolIfExists": {"mfa": [True]}},
))

# unrelated rule change: modifications concern action alice never has on these
# resources; the (alice, s3:GetObject, photos/*) region must be unchanged.
UNRELATED_V1 = policy(
    stmt("read", "Allow", ["alice"], ["s3:GetObject"], ["photos/*"]),
    stmt("admin", "Allow", ["alice"], ["s3:DeleteObject"], ["admin/*"]),
)
UNRELATED_V2 = policy(
    stmt("read", "Allow", ["alice"], ["s3:GetObject"], ["photos/*"]),
    stmt("admin2", "Deny", ["alice"], ["s3:DeleteObject"], ["admin/*"]),
)

# explicit deny override: adding a Deny over part of an Allow contracts.
DENY_OVERRIDE_V1 = policy(stmt("all", "Allow", ["alice"], ["s3:GetObject"], ["photos/*"]))
DENY_OVERRIDE_V2 = policy(
    stmt("all", "Allow", ["alice"], ["s3:GetObject"], ["photos/*"]),
    stmt("secret", "Deny", ["alice"], ["s3:GetObject"], ["photos/secret/*"]),
)

# default deny -> explicit deny (tightening, but not an expansion)
TIGHTEN_V1 = EMPTY
TIGHTEN_V2 = policy(stmt("block", "Deny", ["alice"], ["s3:GetObject"], ["photos/*"]))

# IP condition boundary: 10.0.0.0/30 == {.0,.1,.2,.3}
IP_V1 = EMPTY
IP_V2 = policy(stmt(
    "office", "Allow", ["alice"], ["s3:GetObject"], ["docs/*"],
    {"IpAddress": {"source_ip": ["10.0.0.0/30"]}},
))

# Numeric <= boundary
NUM_V1 = EMPTY
NUM_V2 = policy(stmt(
    "age", "Allow", ["alice"], ["s3:GetObject"], ["docs/*"],
    {"NumericLessThanEquals": {"age": [18]}},
))

# malformed documents
BAD_GLOB = policy(stmt("bad", "Allow", ["alice"], ["s3:GetObject"], ["photos/**/x"]))
BAD_VERSION = {"Version": "2000-01-01", "Statement": []}
BAD_EFFECT = policy(stmt("bad", "Maybe", ["alice"], ["s3:GetObject"], ["photos/*"]))
BAD_NO_RESOURCE = {"Version": V, "Statement": [
    {"Sid": "x", "Effect": "Allow", "Principal": ["alice"], "Action": ["s3:GetObject"]}]}
