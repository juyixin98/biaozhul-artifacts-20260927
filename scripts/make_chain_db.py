"""Build a small committed chain DB used by the chain-replay verification.

Creates three valid blocks (issuance, a fee-paying transfer, and an
intra-block back-reference spend) so the chain replay has multiple blocks and
an intra-block dependency to rebuild.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utxo_ledger.encoding import Outpoint, encode_block  # noqa: E402
from utxo_ledger.node import LedgerNode  # noqa: E402
from utxo_ledger.storage import SqliteStore  # noqa: E402

from tests.fixtures import (  # noqa: E402
    FixtureBuilder,
    coinbase_tx,
    named_key,
    transfer_tx,
)


def build(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    alice, bob, carol = named_key("alice"), named_key("bob"), named_key("carol")
    fb = FixtureBuilder()
    store = SqliteStore(path)
    node = LedgerNode(store)

    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    assert node.submit_raw_block(encode_block(g), 1).accepted

    t1 = transfer_tx(
        [(Outpoint(g.transactions[0].txid, 0), alice.public_bytes)],
        [(100_000, bob.public_bytes), (899_000, alice.public_bytes)],
        {0: alice},
    )  # fee 1000
    b2 = fb.append([coinbase_tx(2, [(1_001_000, alice.public_bytes)]), t1])
    assert node.submit_raw_block(encode_block(b2), 2).accepted

    t2 = transfer_tx(
        [(Outpoint(t1.txid, 0), bob.public_bytes)],
        [(99_500, carol.public_bytes)],
        {0: bob},
    )  # fee 500, spends an intra-block... here a committed output from block 2
    b3 = fb.append([coinbase_tx(3, [(1_000_500, alice.public_bytes)]), t2])
    assert node.submit_raw_block(encode_block(b3), 3).accepted

    print(
        f"built chain at {path}: tip={store.tip_height} "
        f"{store.tip_hash.hex()[:16]}... utxos={store.utxo_count()}"
    )
    store.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    args = p.parse_args()
    build(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
