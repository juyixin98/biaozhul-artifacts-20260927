"""Standalone in-process smoke demo (no web server, no pytest).

Run:  python scripts/smoke.py
Shows: split, successful verified recovery, and categorical failures.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import Auditor  # noqa: E402
from app.core.kernel import Kernel  # noqa: E402
from app.core.shamir import RecoverStatus  # noqa: E402
from app.state import Store  # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp()
    store = Store(os.path.join(tmp, "smoke.db"))
    kernel = Kernel(store, Auditor(store, to_stderr=True))

    secret = b"the quick brown fox"
    created = kernel.create_collection(
        request_id="smoke_create", secret=secret, threshold=3, total=5,
        collection_id="coll_smoke",
    )
    shares = created["shares"]

    print("\n# 1) recover with exactly threshold (unverifiable)")
    r = kernel.recover(request_id="smoke_t", collection_id="coll_smoke",
                       submitted=[shares[0], shares[2], shares[4]])
    print("status:", r.status.value, "secret:", r.secret)
    assert r.status == RecoverStatus.RECOVERED_UNVERIFIABLE and r.secret == secret

    print("\n# 2) recover with > threshold (verified)")
    r = kernel.recover(request_id="smoke_v", collection_id="coll_smoke",
                       submitted=shares)
    print("status:", r.status.value, "extra:", r.extra_xs, "secret:", r.secret)
    assert r.status == RecoverStatus.RECOVERED_VERIFIED

    print("\n# 3) below threshold")
    r = kernel.recover(request_id="smoke_low", collection_id="coll_smoke",
                       submitted=shares[:2])
    print("status:", r.status.value)
    assert r.status == RecoverStatus.REJECTED_INSUFFICIENT

    print("\nALL SMOKE CHECKS PASSED")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
