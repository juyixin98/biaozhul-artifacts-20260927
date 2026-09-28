"""The chain-state kernel: transaction validation and block state transition.

This module does the actual state-machine work:

1. recover and verify the signer (encoding + signature module),
2. validate fee caps, gas, nonce, arithmetic overflow and balance,
3. charge ``effective_gas_price * gas`` (burn base-fee part, pay tip to a local
   coinbase), enforce gas-cap and parent-only recurrence rules,
4. commit atomically — a hard block-level error rejects the whole block.

No EVM is executed: each valid transaction consumes exactly ``gas_limit`` (the
synthetic model treats the declared gas as the realized gas). Overpayment slack
(``max_fee - effective_price``) is simply left in the sender's balance, which
is what produces the exact conservation identity tested by the test-suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..params import PARAMS, UINT256_MAX
from ..errors import ErrorCode
from ..encoding import serialization as ser
from ..encoding.hexutil import hex_to_bytes
from ..encoding import crypto
from . import eip1559
from .models import (
    Block,
    ExecutedBlock,
    InvalidTxRecord,
    Transaction,
    TxReceipt,
)

BURN_ADDRESS = "0x" + "00" * 20
COINBASE_ADDRESS = "0x" + "11" * 20


@dataclass
class ChainState:
    """Minimal mutable world state: balances and account nonces."""

    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    protocol_version: str = PARAMS.protocol_version

    def clone(self) -> "ChainState":
        return ChainState(
            balances=dict(self.balances),
            nonces=dict(self.nonces),
            protocol_version=self.protocol_version,
        )


def _to_bytes_addr(address: str) -> bytes:
    raw = hex_to_bytes(address)
    if len(raw) != 20:
        raise ValueError("address must be 20 bytes")
    return raw


def recover_sender(tx: Transaction) -> tuple[Optional[str], Optional[str]]:
    """Return (sender, error_code). Exactly one is None."""
    if tx.signature is None:
        return None, ErrorCode.E010_SIGNATURE_MALFORMED.value
    sig = tx.signature
    if sig.v not in (0, 1):
        return None, ErrorCode.E010_SIGNATURE_MALFORMED.value
    if not (1 <= sig.r < crypto.CURVE.order) or not (1 <= sig.s < crypto.CURVE.order):
        return None, ErrorCode.E010_SIGNATURE_MALFORMED.value
    if sig.s > crypto.HALF_N:
        return None, ErrorCode.E012_SIGNATURE_HIGH_S.value
    try:
        to_bytes = _to_bytes_addr(tx.to)
        digest = ser.tx_digest(
            chain_id=tx.chain_id, nonce=tx.nonce,
            max_fee_per_gas=tx.max_fee_per_gas,
            max_priority_fee_per_gas=tx.max_priority_fee_per_gas,
            gas_limit=tx.gas_limit, to=to_bytes, value=tx.value, data=tx.data,
        )
        rec = crypto.recover_pubkey(sig.r, sig.s, sig.v, digest)
        sender = ser.address_from_pubkey(rec.raw_xy())
        if not crypto.verify_recovered(sig.r, sig.s, sig.v, digest, rec.raw_xy()):
            return None, ErrorCode.E011_SIGNATURE_INVALID.value
        return sender, None
    except ValueError as exc:
        msg = str(exc)
        if "hex" in msg.lower():
            return None, ErrorCode.E001_HEX_DECODE.value
        return None, ErrorCode.E011_SIGNATURE_INVALID.value


def validate_static(tx: Transaction, base_fee: int) -> Optional[str]:
    """Fee-cap, gas, field and overflow checks that need no world state."""
    if tx.chain_id != PARAMS.chain_id:
        return ErrorCode.E004_BAD_CHAIN_ID.value
    for name, val in (
        ("nonce", tx.nonce),
        ("max_fee_per_gas", tx.max_fee_per_gas),
        ("max_priority_fee_per_gas", tx.max_priority_fee_per_gas),
        ("gas_limit", tx.gas_limit),
        ("value", tx.value),
    ):
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            return ErrorCode.E022_BAD_FIELDS.value
    if tx.max_fee_per_gas > UINT256_MAX or tx.max_priority_fee_per_gas > UINT256_MAX:
        return ErrorCode.E022_BAD_FIELDS.value
    if tx.max_fee_per_gas < base_fee:
        # The fee cap cannot cover the block's base fee: the tx cannot be included.
        return ErrorCode.E020_MAX_FEE_BELOW_BASE.value
    if tx.gas_limit < PARAMS.intrinsic_tx_gas:
        return ErrorCode.E030_GAS_LIMIT_TOO_LOW.value
    # max_priority > max_fee is permitted by EIP-1559 (tip auto-capped at the
    # slack); it must NOT be rejected here. Overflow check on the worst case:
    try:
        max_fee_per_gas = tx.max_fee_per_gas
        _ = max_fee_per_gas * tx.gas_limit
        if _ > UINT256_MAX or _ < 0:
            return ErrorCode.E032_FEE_OVERFLOW.value
        _ = tx.value + _
        if _ > UINT256_MAX:
            return ErrorCode.E032_FEE_OVERFLOW.value
    except (OverflowError, ArithmeticError):
        return ErrorCode.E032_FEE_OVERFLOW.value
    return None


def _hash_tx(tx: Transaction) -> str:
    if tx.signature is None:
        return "0x" + "00" * 32
    return ser.signed_tx_hash(
        chain_id=tx.chain_id, nonce=tx.nonce,
        max_fee_per_gas=tx.max_fee_per_gas,
        max_priority_fee_per_gas=tx.max_priority_fee_per_gas,
        gas_limit=tx.gas_limit, to=_to_bytes_addr(tx.to), value=tx.value,
        data=tx.data, r=tx.signature.r, s=tx.signature.s, v=tx.signature.v,
    )


def execute_block(
    *,
    block: Block,
    parent_number: int,
    parent_hash: str,
    parent_base_fee: int,
    parent_gas_limit: int,
    state: ChainState,
    strict: bool = False,
    declared_next_base_fee: Optional[int] = None,
) -> ExecutedBlock:
    """Validate and execute one block on top of its parent.

    Returns an :class:`ExecutedBlock`. A hard error sets ``accepted=False`` and
    leaves ``state`` untouched (all mutation happens on an overlay commit).
    Per-transaction validity errors never abort in non-strict mode; they are
    collected in ``invalid`` and those transactions are skipped.
    """
    work = state.clone()
    receipts: list[TxReceipt] = []
    invalid: list[InvalidTxRecord] = []

    # ---- header / linkage checks (hard) ----
    def reject(code: str, detail: str) -> ExecutedBlock:
        return ExecutedBlock(
            block=block, receipts=receipts, invalid=invalid,
            next_base_fee=parent_base_fee, fee_step_explanation={},
            accepted=False, block_error=code, block_error_detail=detail,
            balances_after=dict(state.balances),
        )

    if block.number != parent_number + 1:
        return reject(ErrorCode.E042_BAD_PARENT.value,
                      f"block number {block.number} does not follow parent {parent_number}")
    if block.parent_hash != parent_hash:
        return reject(ErrorCode.E042_BAD_PARENT.value, "parent_hash does not connect")
    if block.gas_limit <= 0 or block.gas_limit % PARAMS.elasticity_multiplier != 0:
        return reject(ErrorCode.E045_GAS_LIMIT_INVALID.value,
                      f"gas_limit {block.gas_limit} must be positive and divisible by "
                      f"{PARAMS.elasticity_multiplier}")
    if block.gas_used < 0 or block.gas_used > block.gas_limit:
        return reject(ErrorCode.E040_BLOCK_GAS_EXCEEDED.value,
                      f"declared gas_used {block.gas_used} exceeds gas_limit {block.gas_limit}")
    if block.base_fee_per_gas != parent_base_fee:
        return reject(ErrorCode.E043_BASE_FEE_MISMATCH.value,
                      f"block base_fee {block.base_fee_per_gas} != parent's next "
                      f"base fee {parent_base_fee}")

    # ---- transaction execution ----
    cumulative_gas = 0
    total_burned = 0
    total_tipped = 0
    work.balances.setdefault(BURN_ADDRESS, 0)
    work.balances.setdefault(COINBASE_ADDRESS, 0)

    for idx, tx in enumerate(block.transactions):
        txhash = _safe_hash(tx)
        sender, sig_err = recover_sender(tx)
        if sig_err is not None:
            code = _record_invalid(invalid, idx, txhash, sig_err, strict)
            if code:
                return reject(code, f"tx[{idx}] invalid in strict mode: {sig_err}")
            receipts.append(_reject_receipt(txhash, sender, sig_err))
            continue
        static_err = validate_static(tx, block.base_fee_per_gas)
        if static_err is not None:
            code = _record_invalid(invalid, idx, txhash, static_err, strict)
            if code:
                return reject(code, f"tx[{idx}] invalid in strict mode: {static_err}")
            receipts.append(_reject_receipt(txhash, sender, static_err))
            continue

        # stateful checks
        expected_nonce = work.nonces.get(sender, 0)
        if tx.nonce != expected_nonce:
            code = _record_invalid(invalid, idx, txhash,
                                   ErrorCode.E031_NONCE_MISMATCH.value, strict)
            if code:
                return reject(code, f"tx[{idx}] nonce {tx.nonce} != expected {expected_nonce}")
            receipts.append(_reject_receipt(txhash, sender,
                                            ErrorCode.E031_NONCE_MISMATCH.value))
            continue

        tip = eip1559.effective_priority_tip(
            base_fee=block.base_fee_per_gas,
            max_fee_per_gas=tx.max_fee_per_gas,
            max_priority_fee_per_gas=tx.max_priority_fee_per_gas,
        )
        price = block.base_fee_per_gas + tip
        gas = tx.gas_limit
        fee_total = price * gas
        total_cost = fee_total + tx.value
        if total_cost > UINT256_MAX or fee_total > UINT256_MAX:
            code = _record_invalid(invalid, idx, txhash,
                                   ErrorCode.E032_FEE_OVERFLOW.value, strict)
            if code:
                return reject(code, f"tx[{idx}] fee overflow")
            receipts.append(_reject_receipt(txhash, sender,
                                            ErrorCode.E032_FEE_OVERFLOW.value))
            continue
        balance = work.balances.get(sender, 0)
        if balance < total_cost:
            code = _record_invalid(invalid, idx, txhash,
                                   ErrorCode.E033_INSUFFICIENT_BALANCE.value, strict)
            if code:
                return reject(code, f"tx[{idx}] balance {balance} < {total_cost}")
            receipts.append(_reject_receipt(txhash, sender,
                                            ErrorCode.E033_INSUFFICIENT_BALANCE.value))
            continue

        # All validity checks passed: enforce the *block* gas cap only for
        # transactions that are actually included (invalid ones consume 0).
        if cumulative_gas + gas > block.gas_limit:
            return reject(ErrorCode.E040_BLOCK_GAS_EXCEEDED.value,
                          f"including tx[{idx}] would exceed block gas_limit")

        # ---- apply (all checks passed) ----
        burned = block.base_fee_per_gas * gas
        tipped = tip * gas
        work.balances[sender] = balance - total_cost
        work.balances[BURN_ADDRESS] += burned
        work.balances[COINBASE_ADDRESS] += tipped
        if tx.to != sender:
            work.balances[tx.to] = work.balances.get(tx.to, 0) + tx.value
        else:
            # self-transfer: value stays with sender (already debited above)
            work.balances[sender] += tx.value
        work.nonces[sender] = expected_nonce + 1
        cumulative_gas += gas
        total_burned += burned
        total_tipped += tipped
        receipts.append(TxReceipt(
            tx_hash=txhash, sender=sender, valid=True, error_code=None,
            gas=gas, effective_priority_tip=tip, effective_gas_price=price,
            burned=burned, tip=tipped, total_cost=total_cost,
        ))

    if cumulative_gas != block.gas_used:
        return reject(ErrorCode.E041_GAS_USED_MISMATCH.value,
                      f"valid txs consumed {cumulative_gas} gas but header declares "
                      f"{block.gas_used} (skipped invalid txs: {len(invalid)})")

    step = eip1559.compute_next_base_fee_step(
        block.base_fee_per_gas, block.gas_used, block.gas_limit
    )
    next_fee = step.next_base_fee
    if declared_next_base_fee is not None and declared_next_base_fee != next_fee:
        return reject(ErrorCode.E043_BASE_FEE_MISMATCH.value,
                      f"declared next base fee {declared_next_base_fee} != recurrence {next_fee}")

    # Commit overlay to caller's state.
    state.balances = work.balances
    state.nonces = work.nonces

    return ExecutedBlock(
        block=block, receipts=receipts, invalid=invalid,
        next_base_fee=next_fee,
        fee_step_explanation={
            "parent_base_fee": step.parent_base_fee,
            "gas_limit": step.gas_limit,
            "gas_used": step.gas_used,
            "target_gas": step.target_gas,
            "direction": step.direction,
            "delta_numerator": step.delta_numerator,
            "delta_after_first_floor": step.delta_after_first_floor,
            "delta_final": step.delta_final,
            "applied_min_increment": step.applied_min_increment,
            "next_base_fee": step.next_base_fee,
            "denominator": PARAMS.base_fee_max_change_denominator,
            "elasticity_multiplier": PARAMS.elasticity_multiplier,
        },
        accepted=True,
        total_burned=total_burned,
        total_tipped=total_tipped,
        balances_after=dict(state.balances),
        burned_address=BURN_ADDRESS,
    )


def _safe_hash(tx: Transaction) -> str:
    try:
        return _hash_tx(tx)
    except ValueError:
        return "0x" + "ff" * 32


def _record_invalid(invalid: list, idx: int, txhash: str, code: str, strict: bool):
    invalid.append(InvalidTxRecord(index=idx, tx_hash=txhash,
                                   error_code=code, detail=code))
    return code if strict else None


def _reject_receipt(txhash: str, sender, code: str) -> TxReceipt:
    return TxReceipt(tx_hash=txhash, sender=sender or "", valid=False, error_code=code)
