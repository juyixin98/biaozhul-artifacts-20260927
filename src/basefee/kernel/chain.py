"""In-process chain assembly: genesis, parent linkage and accepted-block store.

This is the in-memory side; durable indexing lives in :mod:`basefee.storage`.
The chain object enforces that blocks arrive connected to the current head and
that each block's declared base fee equals the recurrence output of the parent
(the kernel performs the same check against parent parameters).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..params import PARAMS
from ..encoding import serialization as ser
from ..encoding.hexutil import hex_to_bytes
from .execution import ChainState, execute_block, BURN_ADDRESS, COINBASE_ADDRESS
from .models import Block, ExecutedBlock

ZERO_HASH = "0x" + "00" * 32


@dataclass
class HeadInfo:
    number: int
    block_hash: str
    base_fee: int
    gas_limit: int
    gas_used: int
    next_base_fee: int


class Chain:
    def __init__(self, state: Optional[ChainState] = None):
        self.state = state or ChainState()
        self.state.balances.setdefault(BURN_ADDRESS, 0)
        self.state.balances.setdefault(COINBASE_ADDRESS, 0)
        self._blocks: dict[int, ExecutedBlock] = {}
        self._hashes: dict[str, int] = {}
        # Genesis identity is the conventional all-zero hash (it has no
        # parent and is configuration, not a produced block).
        self._genesis_hash = ZERO_HASH
        self.head = HeadInfo(
            number=PARAMS.genesis_number,
            block_hash=ZERO_HASH,
            base_fee=PARAMS.genesis_base_fee,
            gas_limit=PARAMS.genesis_gas_limit,
            gas_used=PARAMS.genesis_gas_used,
            # Genesis's "next" fee is the recurrence applied to genesis itself.
            next_base_fee=PARAMS.genesis_base_fee,
        )

    @staticmethod
    def _hash_header(parent_hash: str, number: int, base_fee: int,
                     gas_limit: int, gas_used: int, tx_hashes_hex: list[str]) -> str:
        raw = ser.block_header_hash(
            parent_hash=hex_to_bytes(parent_hash),
            number=number, base_fee_per_gas=base_fee,
            gas_limit=gas_limit, gas_used=gas_used,
            tx_hashes=[hex_to_bytes(h) for h in tx_hashes_hex],
        )
        return "0x" + raw.hex()

    def genesis_hash(self) -> str:
        return self._genesis_hash

    def parent_context(self) -> dict:
        return {
            "parent_number": self.head.number,
            "parent_hash": self.head.block_hash,
            "parent_base_fee": self.head.next_base_fee,
            "parent_gas_limit": self.head.gas_limit,
        }

    def apply_block(self, block: Block, *, strict: bool = False) -> ExecutedBlock:
        ctx = self.parent_context()
        executed = execute_block(
            block=block,
            parent_number=ctx["parent_number"],
            parent_hash=ctx["parent_hash"],
            parent_base_fee=ctx["parent_base_fee"],
            parent_gas_limit=ctx["parent_gas_limit"],
            state=self.state,
            strict=strict,
        )
        if not executed.accepted:
            return executed
        valid_hashes = [r.tx_hash for r in executed.receipts if r.valid]
        block_hash = self._hash_header(
            block.parent_hash, block.number, block.base_fee_per_gas,
            block.gas_limit, block.gas_used, valid_hashes,
        )
        block.block_hash = block_hash
        self._blocks[block.number] = executed
        self._hashes[block_hash] = block.number
        self.head = HeadInfo(
            number=block.number,
            block_hash=block_hash,
            base_fee=block.base_fee_per_gas,
            gas_limit=block.gas_limit,
            gas_used=block.gas_used,
            next_base_fee=executed.next_base_fee,
        )
        return executed

    def get_block(self, number: int) -> Optional[ExecutedBlock]:
        return self._blocks.get(number)

    def get_by_hash(self, block_hash: str) -> Optional[ExecutedBlock]:
        number = self._hashes.get(block_hash)
        return self._blocks.get(number) if number is not None else None

    def height(self) -> int:
        return self.head.number
