"""The security kernel: orchestrates parsing, integrity, math and state.

This is the only module that ties the layers together. It returns structured,
categorical outcomes (so callers -- HTTP or tests -- assert a *result class*,
not merely "the endpoint responded") and emits fingerprint-only audit events.
"""
from __future__ import annotations

import os
import dataclasses

from .. import parsing
from ..audit import AuditEvent, Auditor
from ..state import CollectionNotFound, Store
from .envelope import (
    ShareEnvelope,
    fingerprint,
    fingerprints,
    seal,
    verify_mac,
)
from .field import FieldParams, SECP256K1_P
from .shamir import (
    RecoverResult,
    RecoverStatus,
    SplitError,
    split_secret,
    recover_from_points,
)


def new_collection_id() -> str:
    return "coll_" + os.urandom(12).hex()


def redact(envelopes) -> list[dict]:
    """Non-sensitive view of shares for diagnostics (no ys / mac / secret)."""
    return [e.public_view() for e in envelopes]


@dataclasses.dataclass
class RecoveryReport:
    request_id: str
    collection_id: str
    status: RecoverStatus
    secret: bytes | None
    accepted_xs: list[int]
    distinct_xs: list[int]
    rejected: list[dict]          # {x?, reason, detail, fingerprint?}
    used_xs: list[int]
    extra_xs: list[int]
    mismatched_xs: list[int]
    math: RecoverResult | None

    def secret_hex(self) -> str | None:
        return None if self.secret is None else self.secret.hex()


class Kernel:
    def __init__(self, store: Store, auditor: Auditor):
        self.store = store
        self.auditor = auditor
        self.field = FieldParams()

    # ------------------------------------------------------------------ #
    # Create / distribute
    # ------------------------------------------------------------------ #
    def create_collection(
        self,
        *,
        request_id: str,
        secret: bytes,
        threshold: int,
        total: int,
        collection_id: str | None = None,
    ) -> dict:
        collection_id = collection_id or new_collection_id()
        if not isinstance(secret, (bytes, bytearray)):
            raise TypeError("secret must be bytes")
        if not (1 <= threshold <= total):
            raise SplitError("require 1 <= threshold <= total")

        result = split_secret(bytes(secret), threshold, total)
        block_count = len(result.shares[0][1])
        mac_key = os.urandom(32)

        self.store.create_collection(
            collection_id=collection_id,
            threshold=threshold,
            total=total,
            block_count=block_count,
            mac_key=mac_key,
            prime=str(SECP256K1_P),
        )

        envelopes: list[ShareEnvelope] = []
        for x, ys in result.shares:
            env = ShareEnvelope(
                collection_id=collection_id,
                threshold=threshold,
                total=total,
                x=x,
                ys=tuple(ys),
                field=self.field,
            )
            env = seal(env, mac_key)
            self.store.insert_share(collection_id, x, ys, env.mac)
            envelopes.append(env)

        self.auditor.record(AuditEvent(
            request_id=request_id,
            collection_id=collection_id,
            action="create",
            verdict="ok",
            detail=f"threshold={threshold} total={total} blocks={block_count}",
            fingerprints=fingerprints(envelopes),
        ))
        return {
            "collection_id": collection_id,
            "threshold": threshold,
            "total": total,
            "block_count": block_count,
            "field": self.field.to_dict(),
            "shares": [e.to_dict() for e in envelopes],
        }

    def get_collection_bundle(self, collection_id: str) -> list[ShareEnvelope]:
        """Return all stored, MAC-verified shares for a collection (local fixture)."""
        row = self.store.get_collection(collection_id)
        import json
        mac_key = row["mac_key"]
        out: list[ShareEnvelope] = []
        for s in self.store.list_shares(collection_id):
            ys = tuple(int(v) for v in json.loads(s["ys"]))
            env = ShareEnvelope(
                collection_id=collection_id,
                threshold=row["threshold"],
                total=row["total"],
                x=s["x"],
                ys=ys,
                field=self.field,
                mac=s["mac"],
            )
            if not verify_mac(env, mac_key):
                # Stored integrity failure is a hard error, never silently used.
                raise RuntimeError(f"stored share x={s['x']} failed integrity")
            out.append(env)
        return out

    # ------------------------------------------------------------------ #
    # Recover
    # ------------------------------------------------------------------ #
    def recover(
        self,
        *,
        request_id: str,
        collection_id: str,
        submitted: list,
    ) -> RecoveryReport:
        try:
            coll = self.store.get_collection(collection_id)
        except CollectionNotFound:
            self.auditor.record(AuditEvent(
                request_id=request_id,
                collection_id=collection_id,
                action="recover",
                verdict="rejected_unknown_collection",
                detail="no such collection",
                fingerprints=[],
            ))
            raise

        mac_key = coll["mac_key"]
        threshold = coll["threshold"]
        total = coll["total"]
        block_count = coll["block_count"]

        accepted: dict[int, ShareEnvelope] = {}
        rejected: list[dict] = []

        def reject(reason, detail, fp=None, x=None):
            entry = {"reason": reason, "detail": detail, "fingerprint": fp}
            if x is not None:
                entry["x"] = x
            rejected.append(entry)

        for raw in submitted:
            # 1) structural + field parse
            try:
                env = parsing.parse_envelope(raw)
            except parsing.ShareRejection as exc:
                reject(exc.reason.value, exc.detail, exc.fp)
                continue

            # 2) bind to this collection identity / params
            try:
                parsing.check_bindings(
                    env,
                    collection_id=collection_id,
                    threshold=threshold,
                    total=total,
                    field=self.field,
                    block_count=block_count,
                )
            except parsing.ShareRejection as exc:
                reject(exc.reason.value, exc.detail, exc.fp, x=getattr(env, "x", None))
                continue

            # 3) independent integrity check (before any field math)
            if not verify_mac(env, mac_key):
                reject(
                    parsing.RejectReason.BAD_INTEGRITY.value,
                    "HMAC verification failed",
                    fingerprint(env),
                    x=env.x,
                )
                continue

            # 4) duplicate x handling -- never counted twice
            if env.x in accepted:
                existing = accepted[env.x]
                if existing.ys == env.ys:
                    reject(
                        parsing.RejectReason.DUPLICATE_X.value,
                        f"identical share for x={env.x} counted once",
                        fingerprint(env),
                        x=env.x,
                    )
                else:
                    reject(
                        parsing.RejectReason.DUPLICATE_X_CONFLICT.value,
                        f"two different MAC-valid shares share x={env.x}",
                        fingerprint(env),
                        x=env.x,
                    )
                continue

            accepted[env.x] = env

        distinct_xs = sorted(accepted)

        # 5) threshold gate (distinct, admissible shares only)
        if len(distinct_xs) < threshold:
            status = RecoverStatus.REJECTED_INSUFFICIENT
            math = None
            used_xs: list[int] = []
            extra_xs: list[int] = []
            mismatched: list[int] = []
            secret: bytes | None = None
            verdict = status.value
            detail = (
                f"{len(distinct_xs)} distinct admissible share(s) < threshold "
                f"{threshold}"
            )
        else:
            points = {x: list(accepted[x].ys) for x in distinct_xs}
            math = recover_from_points(points, threshold, block_count)
            status = math.status
            used_xs = math.used_xs
            extra_xs = math.extra_xs
            mismatched = math.mismatched_xs
            secret = math.secret
            verdict = status.value
            detail = math.detail

        self.auditor.record(AuditEvent(
            request_id=request_id,
            collection_id=collection_id,
            action="recover",
            verdict=verdict,
            detail=detail,
            fingerprints=fingerprints(accepted.values()),
        ))

        return RecoveryReport(
            request_id=request_id,
            collection_id=collection_id,
            status=status,
            secret=secret,
            accepted_xs=distinct_xs,
            distinct_xs=distinct_xs,
            rejected=rejected,
            used_xs=used_xs,
            extra_xs=extra_xs,
            mismatched_xs=mismatched,
            math=math,
        )

    # ------------------------------------------------------------------ #
    # Test / demonstration-only key-holder capability
    # ------------------------------------------------------------------ #
    def issue_share_with_value(
        self, collection_id: str, x: int, ys: tuple[int, ...]
    ) -> ShareEnvelope:
        """Produce an integrity-valid share with an *arbitrary* y value.

        Deliberately NOT exposed over HTTP. It models what a legitimate key /
        share holder who turns malicious can do: produce a MAC-valid share the
        server cannot by itself attribute. This is exactly the residual trust
        boundary demonstrated in the tests ("recovery failed != all malicious
        parties located").
        """
        row = self.store.get_collection(collection_id)
        env = ShareEnvelope(
            collection_id=collection_id,
            threshold=row["threshold"],
            total=row["total"],
            x=x,
            ys=tuple(ys),
            field=self.field,
        )
        return seal(env, row["mac_key"])
