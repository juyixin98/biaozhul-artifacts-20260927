"""链状态内核：一个确定性的本地伪 EVM 账本。

不模拟 EVM 字节码，而是在我们自己的 ABI 内核之上实现四个确定性
“伪合约方法”，用于演示完整的 编码→验签(选择器分发)→状态转移→
索引存储→离线回放 链路。

方法（参数全部使用受限类型；地址用 bytes20 表达，而非 address）：
    mint(bytes20 to, uint256 amount)
        增发（本地演示，无权限限制，恒定成功直至溢出）
    transfer(bytes20 from, bytes20 to, uint256 amount)
        转账；余额不足回滚
    setNote(bytes20 who, string note)
        写入账户备注（演示动态字符串进状态）
    noteOf(bytes20 who) -> string
        读取备注（视图方法，不改变状态）

失败语义：失败的交易不改变状态，并返回 status="reverted" + 明确
错误类别；绝不会把异常吞成成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..abi import decode_call
from ..abi.errors import ABIError
from ..abi.hashing import keccak256

ZERO_ACCOUNT = b"\x00" * 20
MAX_SUPPLY = (1 << 256) - 1

# 方法注册表：selector -> (签名, 参数类型)
# 延迟构建（需要 keccak）
_REGISTRY: dict[bytes, tuple[str, tuple[str, ...]]] | None = None

METHOD_ABIS: dict[str, tuple[str, ...]] = {
    "mint": ("mint(bytes20,uint256)", ("bytes20", "uint256")),
    "transfer": ("transfer(bytes20,bytes20,uint256)", ("bytes20", "bytes20", "uint256")),
    "setNote": ("setNote(bytes20,string)", ("bytes20", "string")),
    "noteOf": ("noteOf(bytes20)", ("bytes20",)),
}

VIEW_METHODS = {"noteOf"}


def get_registry() -> dict[bytes, tuple[str, tuple[str, ...]]]:
    global _REGISTRY
    if _REGISTRY is None:
        from ..abi import function_selector

        reg: dict[bytes, tuple[str, tuple[str, ...]]] = {}
        for sig, types in METHOD_ABIS.values():
            reg[function_selector(sig)] = (sig, tuple(types))
        _REGISTRY = reg
    return _REGISTRY


@dataclass
class Account:
    balance: int = 0
    nonce: int = 0
    note: str = ""


@dataclass
class Receipt:
    tx_hash: str
    block_number: int
    index: int
    signature: str
    status: str  # "ok" | "reverted"
    error_category: str | None
    error_message: str | None
    # 状态变更（成功时）
    from_account: str | None = None
    to_account: str | None = None
    amount: int | None = None


@dataclass
class Block:
    number: int
    parent_hash: bytes
    receipts: list[Receipt] = field(default_factory=list)
    state_root: bytes = b""
    block_hash: bytes = b""


class ChainKernel:
    """内存确定性账本；状态可通过 state_root 指纹化，可离线重建。"""

    def __init__(self, genesis_parent: bytes = b"\x00" * 32):
        self.accounts: dict[bytes, Account] = {}
        self.block_number = 0
        self.parent_hash = genesis_parent
        self.tx_index = 0

    # ---- 账户视图 ----
    def account(self, addr: bytes) -> Account:
        if len(addr) != 20:
            raise ValueError("账户地址必须是 20 字节")
        acct = self.accounts.get(addr)
        if acct is None:
            acct = Account()
            self.accounts[addr] = acct
        return acct

    def balance_of(self, addr: bytes) -> int:
        if len(addr) != 20:
            raise ValueError("账户地址必须是 20 字节")
        acct = self.accounts.get(addr)
        return acct.balance if acct else 0

    def note_of(self, addr: bytes) -> str:
        if len(addr) != 20:
            raise ValueError("账户地址必须是 20 字节")
        acct = self.accounts.get(addr)
        return acct.note if acct else ""

    # ---- 单笔交易 ----
    def apply_tx(self, calldata: bytes) -> tuple[Receipt, object | None]:
        """执行一笔 calldata。返回 (回执, 视图返回值或 None)。

        ABI 解码失败/未知选择器/业务回滚都生成 reverted 回执（不抛给
        调用方），但带明确 error_category；调用方据此判定，绝不当成功。
        """
        index = self.tx_index
        self.tx_index += 1
        tx_hash = "0x" + keccak256(calldata + self.block_number.to_bytes(32, "big")
                                   + index.to_bytes(32, "big")).hex()

        try:
            signature, args = decode_call(calldata, get_registry())
        except ABIError as e:
            return Receipt(
                tx_hash=tx_hash, block_number=self.block_number, index=index,
                signature="<unparseable>", status="reverted",
                error_category=e.category, error_message=str(e),
            ), None

        name = signature.split("(", 1)[0]
        try:
            view_result = self._dispatch(name, args)
        except _Revert as rv:
            return Receipt(
                tx_hash=tx_hash, block_number=self.block_number, index=index,
                signature=signature, status="reverted",
                error_category=rv.category, error_message=str(rv),
            ), None
        except ABIError as e:  # 值层面（理论上解码后少见，双保险）
            return Receipt(
                tx_hash=tx_hash, block_number=self.block_number, index=index,
                signature=signature, status="reverted",
                error_category=e.category, error_message=str(e),
            ), None

        receipt = Receipt(
            tx_hash=tx_hash, block_number=self.block_number, index=index,
            signature=signature, status="ok", error_category=None,
            error_message=None,
        )
        self._annotate(receipt, name, args)
        return receipt, view_result

    def _annotate(self, receipt: Receipt, name: str, args) -> None:
        if name == "transfer":
            receipt.from_account = "0x" + args[0].hex()
            receipt.to_account = "0x" + args[1].hex()
            receipt.amount = args[2]
        elif name == "mint":
            receipt.to_account = "0x" + args[0].hex()
            receipt.amount = args[1]

    def _dispatch(self, name: str, args):
        if name == "mint":
            to, amount = args
            if amount < 0:
                raise _Revert("value_error", "mint 金额不能为负")
            acct = self.account(to)
            if acct.balance > MAX_SUPPLY - amount:
                raise _Revert("arithmetic_overflow", "铸造导致总供应溢出 uint256")
            acct.balance += amount
            acct.nonce += 1
            return None

        if name == "transfer":
            frm, to, amount = args
            if amount < 0:
                raise _Revert("value_error", "转账金额不能为负")
            src = self.account(frm)
            if src.balance < amount:
                raise _Revert("insufficient_balance",
                              f"余额 {src.balance} 不足，试图转出 {amount}")
            dst = self.account(to)
            if dst.balance > MAX_SUPPLY - amount:
                raise _Revert("arithmetic_overflow", "收款导致余额溢出 uint256")
            src.balance -= amount
            dst.balance += amount
            src.nonce += 1
            if frm != to:
                dst.nonce += 1
            return None

        if name == "setNote":
            who, note = args
            acct = self.account(who)
            acct.note = note
            acct.nonce += 1
            return None

        if name == "noteOf":
            (who,) = args
            return self.note_of(who)

        raise _Revert("unknown_method", f"内核未实现方法 {name}")

    # ---- 区块 ----
    def apply_block(self, calldatas: list[bytes]) -> Block:
        """顺序执行一个区块的全部交易，计算状态根与区块哈希。"""
        receipts: list[Receipt] = []
        for cd in calldatas:
            receipt, _ = self.apply_tx(cd)
            receipts.append(receipt)

        state_root = self.compute_state_root()
        block = Block(
            number=self.block_number,
            parent_hash=self.parent_hash,
            receipts=receipts,
            state_root=state_root,
        )
        block.block_hash = self._compute_block_hash(block)
        self.parent_hash = block.block_hash
        self.block_number += 1
        return block

    def compute_state_root(self) -> bytes:
        """状态指纹：对排序后的账户做确定性 ABI 编码后取 keccak。

        用我们自己的 ABI 内核编码一个 (bytes20,uint256,uint256,string)[]，
        不引入外部 RLP/默尔克实现，保持依赖最小且完全可复算。
        """
        from ..abi import encode

        rows = []
        for addr in sorted(self.accounts.keys()):
            a = self.accounts[addr]
            rows.append((addr, a.balance, a.nonce, a.note))
        encoded = encode(
            ["(bytes20,uint256,uint256,string)[]"],
            [rows],
        )
        return keccak256(encoded)

    @staticmethod
    def _compute_block_hash(block: Block) -> bytes:
        # 区块头：parent_hash(32) | number(32) | state_root(32)
        head = (
            block.parent_hash
            + block.number.to_bytes(32, "big")
            + block.state_root
        )
        return keccak256(head)


class _Revert(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
