"""Strict hex helpers for tests (32-byte roots/pubkeys must keep leading zeros)."""


def x32(h: str) -> bytes:
    """Strict 0x-hex -> 32 bytes; raises if a leading nibble was dropped."""
    if not isinstance(h, str) or not h.startswith("0x"):
        raise AssertionError(f"not a 0x-hex string: {h!r}")
    raw = bytes.fromhex(h[2:])
    if len(raw) != 32:
        raise AssertionError(
            f"{h[:18]}… decoded to {len(raw)} bytes, expected 32 (leading zero?)"
        )
    return raw


def base_signers(golden):
    """(committee_dict, {pubkey_bytes: signing_key}, [pubkey_bytes]) for the
    checkpoint committee, built with the oracle so keys track sorted pks."""
    import oracle as o

    n = len(golden["checkpoint"]["committee"]["members"])
    committee, key_map = o.make_committee([(i, 10) for i in range(n)])
    pks = [bytes.fromhex(m["public_key"][2:]) for m in committee["members"]]
    return committee, key_map, pks
