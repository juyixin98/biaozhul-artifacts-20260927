"""Generate the synthetic demo config and a feed that exercises every branch.

The produced feed (in order) contains:
  * enough honest 0->1 / 1->2 votes to justify epoch 1 and finalize epoch 0
  * a duplicated valid vote (identical re-transmission)
  * a forged-signature vote (illegal, rejected, no evidence)
  * a real double vote by charlie at epoch 2
  * a real surround vote by bravo (1->4 around 2->3)
  * a vote by delta at epoch 3 after membership rotated delta out (inactive)
  * one malformed line
"""

from __future__ import annotations

import json
from pathlib import Path

from ffg_slash.crypto import derive_seed, keypair_from_seed, sign_vote
from ffg_slash.models import Vote

CHAIN_ID = 4242
GENESIS = b"\x11" * 32


def key(label):
    seed = derive_seed(label)
    _, pub = keypair_from_seed(seed)
    return seed, pub


def vote(label, s, t, source_root=None, target_root=None, chain_id=CHAIN_ID,
         corrupt=False):
    seed, pub = key(label)
    source_root = source_root or (GENESIS if s == 0 else bytes([s & 0xFF]) * 32)
    target_root = target_root or bytes([t & 0xFF]) * 32
    sig = sign_vote(seed, chain_id=chain_id, validator_pubkey=pub,
                    source_epoch=s, source_root=source_root,
                    target_epoch=t, target_root=target_root)
    if corrupt:
        sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
    return Vote(chain_id, pub, s, source_root, t, target_root, sig).to_envelope()


def build_feed() -> list[dict]:
    items: list[dict] = []

    # honest supermajority 0 -> 1 (alpha, bravo, charlie); block root 0x01..
    for label in ("alpha", "bravo", "charlie"):
        items.append(vote(label, 0, 1, source_root=GENESIS,
                          target_root=b"\x01" * 32))
    # delta votes 1 -> 2 too, giving 3/4 on the next consecutive link
    for label in ("alpha", "bravo", "charlie"):
        items.append(vote(label, 1, 2, source_root=b"\x01" * 32,
                          target_root=b"\x02" * 32))

    # duplicate re-transmission of alpha's first vote
    items.append(items[0])

    # forged signature attempt trying to create a fake second target at epoch 1
    items.append(vote("delta", 0, 1, source_root=GENESIS,
                      target_root=b"\xDE" * 32, corrupt=True))

    # REAL double vote: charlie already voted 0->1(root 0x01); now root 0x77
    items.append(vote("charlie", 0, 1, source_root=GENESIS,
                      target_root=b"\x77" * 32))

    # REAL surround by bravo: outer 1->4 around inner 2->3
    items.append(vote("bravo", 1, 4, source_root=b"\x01" * 32,
                      target_root=b"\x04" * 32))
    items.append(vote("bravo", 2, 3, source_root=b"\x02" * 32,
                      target_root=b"\x03" * 32))

    # delta left after epoch 2: target epoch 3 vote must be rejected inactive
    items.append(vote("delta", 2, 3, source_root=b"\x02" * 32,
                      target_root=b"\x03" * 32))
    return items


def main(out: str = "data/demo_feed.jsonl") -> None:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    items = build_feed()
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")
        fh.write("{not valid json but handled as malformed}\n")
    print(f"wrote {len(items)} votes + 1 malformed line to {path}")


if __name__ == "__main__":
    main()
