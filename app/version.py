# Audit batch selective-disclosure service — package version
__version__ = "1.0.0"

# Protocol versions. Bump on any wire-format change so proofs stay version-bound.
# Included in: every commitment, every Merkle node, disclosure packages, audit logs.
COMMITMENT_SCHEMA_VERSION = "audit-commit-v1"
MERKLE_SCHEMA_VERSION = "audit-merkle-v1"
DISCLOSURE_SCHEMA_VERSION = "audit-disclosure-v1"
