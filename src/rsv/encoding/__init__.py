"""模块一：编码与验签。"""

from .opcodes import SUPPORTED, name_of
from .script_codec import Instruction, encode_push, parse_script
from .transaction import (
    Outpoint,
    Transaction,
    TxInput,
    TxOutput,
    transaction_from_dict,
)

__all__ = [
    "SUPPORTED",
    "name_of",
    "Instruction",
    "encode_push",
    "parse_script",
    "Outpoint",
    "Transaction",
    "TxInput",
    "TxOutput",
    "transaction_from_dict",
]
