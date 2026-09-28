"""Pure cryptographic kernel.

Nothing in this package performs I/O, reads configuration, or imports a web
framework. Functions are deterministic given their inputs so they can be
cross-checked by an independent standard-library-only verifier.
"""
