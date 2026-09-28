"""Independent slashing-evidence checker.

ZERO imports from ``ffg_slash`` by design — this package re-implements the
protocol from ``docs/protocol.md`` using only the standard library and the
``cryptography`` primitive, so its verdict is an independent recomputation.
"""

VERSION = "1.0.0-independent"
