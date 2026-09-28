"""链状态内核 (consensus kernel boundary)。

内核拥有全部共识规则，且**不触碰 sqlite**：它在一个内存"暂定视图"中逐笔
规划交易，产出 :class:`BlockPlan`；只有全部交易合法后，存储层才一次性提交。
任意一笔交易非法 -> 立即拒绝，存储层没有任何写入，UTXO 集保持原样。

校验顺序（固定，决定首个报告的失败类别，日志中保留同样的逐步判定理由）：
  块级：解码（调用方） -> 链定位/genesis -> 结构资源上限 -> txid 重复
        -> 前向引用 -> 引用环 -> 逐笔交易 -> 重算 tx_root/witness_root
  交易级（i 从 0 起，块内前序交易的输出立即可用）：
        见证数量 -> 条数上限 -> 版本 -> 金额范围/零值
        -> 发行合法性 -> 同交易重复输入 -> 逐输入 outpoint 解析(未花费)+验签
        -> 溢出保护求和 -> 守恒 sum_in == sum_out + fee

错误类别语义见 :mod:`utxo_ledger.errors`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import encoding
from .crypto import verify as crypto_verify
from .encoding import (
    PUBKEY_COMPRESSED_SIZE,
    Block,
    Outpoint,
    Transaction,
    TxOutput,
    block_id_of,
    encode_block,
    sighash_of,
    tx_root,
    txid_of,
    witness_root,
)
from .errors import (
    AmountOutOfRangeError,
    AmountOverflowError,
    BadGenesisError,
    BlockConflictError,
    ConservationMismatchError,
    DoubleSpendError,
    ForwardReferenceError,
    IllegalIssuanceError,
    InvalidFeeError,
    LedgerError,
    MalformedEncodingError,
    MerkleRootMismatchError,
    ReferenceCycleError,
    ResourceLimitError,
    TxidDuplicateError,
    UnknownOutpointError,
    WitnessCountMismatchError,
    ZeroValueError,
)
from .store import ChainView, Utxo

MAX_MONEY = (1 << 63) - 1  # 金额为整数：单值与求和均不得超过此值


@dataclass(frozen=True, slots=True)
class Limits:
    """资源/预算上限。超限一律 RESOURCE_EXHAUSTED(RESOURCE_LIMIT)。"""

    max_block_bytes: int = 1_000_000
    max_txs_per_block: int = 10_000
    max_inputs_per_tx: int = 10_000
    max_outputs_per_tx: int = 10_000


@dataclass(frozen=True, slots=True)
class PlannedTx:
    """单笔交易通过内核规划后的结果（存储层消费的写计划单元）。"""

    index: int
    tx: Transaction
    txid: bytes
    committed_inputs: tuple[Utxo, ...]
    new_outputs: tuple[tuple[int, TxOutput], ...]
    sum_in: int
    sum_out: int


@dataclass(frozen=True, slots=True)
class BlockPlan:
    """整块的可提交计划；不提交则对外部状态零影响。"""

    block: Block
    block_id: bytes
    height: int
    results: tuple[PlannedTx, ...]
    utxo_root_before: bytes

    @property
    def total_fee(self) -> int:
        return sum(r.tx.fee for r in self.results)


EventSink = Callable[[dict[str, Any]], None]


def _build_reference_graph(
    block: Block, by_id: dict[bytes, int]
) -> tuple[list[tuple[int, int, Outpoint]], dict[int, list[int]]]:
    """从块内交易输入构造引用图。

    返回 (前向边列表, 后向邻接表 j->[i...])；自指边 j==i 既无前向意义也
    不进邻接表（自指在 outpoint 解析阶段必然 UNKNOWN/双花，先于环报出）。
    """
    forward: list[tuple[int, int, Outpoint]] = []
    adj: dict[int, list[int]] = {i: [] for i in range(len(block.transactions))}
    for i, tx in enumerate(block.transactions):
        for inp in tx.inputs:
            j = by_id.get(inp.prev.txid)
            if j is None:
                continue
            if j > i:
                forward.append((i, j, inp.prev))
            elif j < i:
                adj[j].append(i)
    return forward, adj


def _find_cycle_nodes(
    n: int, adj: dict[int, list[int]]
) -> list[int]:
    """Kahn 拓扑排序；返回仍有入度（处于环上）的节点索引，空列表表示无环。"""
    indeg = {i: 0 for i in range(n)}
    for js in adj.values():
        for m in js:
            indeg[m] += 1
    queue = sorted(i for i, d in indeg.items() if d == 0)
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for m in adj[node]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return sorted(i for i, d in indeg.items() if d > 0)


class Kernel:
    def __init__(
        self,
        view: ChainView,
        *,
        limits: Limits | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._view = view
        self._limits = limits or Limits()
        self._sink = event_sink or (lambda _e: None)

    def _event(self, **payload: Any) -> None:
        self._sink(payload)

    # ------------------------------------------------------------------
    def plan_block(self, block: Block) -> BlockPlan:
        """规划整块；失败抛 LedgerError，成功返回 BlockPlan（不写存储）。"""
        lim = self._limits
        header = block.header

        root_before = self._view.utxo_root()
        tip = self._view.tip()

        # --- 1. 链定位 / genesis 语义 ----------------------------------
        if tip is None:
            if header.height != 0:
                raise BadGenesisError(
                    "空链第一块必须为 height=0 的 genesis 块",
                    details={"height": header.height},
                )
            if header.prev_hash != encoding.ZERO_HASH:
                raise BadGenesisError(
                    "genesis 块 prev_hash 必须为全零",
                    details={"prev_hash": header.prev_hash.hex()},
                )
        else:
            tip_height, tip_block_id = tip
            if header.height == 0:
                raise BlockConflictError(
                    "链上已有 genesis，不能再次提交 height=0 块",
                    details={"tip_height": tip_height},
                )
            if header.height != tip_height + 1:
                raise BlockConflictError(
                    "块高度不连续",
                    details={
                        "height": header.height,
                        "expected": tip_height + 1,
                        "tip": tip_height,
                    },
                )
            if header.prev_hash != tip_block_id:
                raise BlockConflictError(
                    "prev_hash 与链尖不一致",
                    details={
                        "prev_hash": header.prev_hash.hex(),
                        "tip_block_id": tip_block_id.hex(),
                    },
                )

        # --- 2. 结构/资源上限 ------------------------------------------
        size = len(encode_block(block))
        if size > lim.max_block_bytes:
            raise ResourceLimitError(
                "块字节数超上限",
                details={
                    "limit_name": "max_block_bytes",
                    "limit": lim.max_block_bytes,
                    "value": size,
                },
            )
        if len(block.transactions) > lim.max_txs_per_block:
            raise ResourceLimitError(
                "块内交易数超上限",
                details={
                    "limit_name": "max_txs_per_block",
                    "limit": lim.max_txs_per_block,
                    "value": len(block.transactions),
                },
            )
        if not block.transactions:
            raise ResourceLimitError(
                "块必须至少包含一笔交易",
                details={"limit_name": "min_txs_per_block", "limit": 1, "value": 0},
            )

        # --- 3. txid 计算与块内重复检测 --------------------------------
        ids: list[bytes] = []
        by_id: dict[bytes, int] = {}
        for i, tx in enumerate(block.transactions):
            tid = txid_of(tx)
            if tid in by_id:
                raise TxidDuplicateError(
                    "块内出现重复 txid",
                    details={"txid": tid.hex(), "first_index": by_id[tid]},
                    tx_index=i,
                )
            by_id[tid] = i
            ids.append(tid)

        # --- 4. 拓扑：前向引用 / 环 ------------------------------------
        self._check_topology(block, by_id)

        # --- 5. 逐笔交易（暂定视图） -----------------------------------
        intra_outputs: dict[bytes, list[tuple[int, TxOutput]]] = {
            ids[i]: [(v, tx.outputs[v]) for v in range(len(tx.outputs))]
            for i, tx in enumerate(block.transactions)
        }
        spent: set[tuple[bytes, int]] = set()

        results: list[PlannedTx] = []
        is_genesis = header.height == 0
        for i, tx in enumerate(block.transactions):
            results.append(
                self._plan_tx(
                    index=i,
                    tx=tx,
                    tid=ids[i],
                    block_height=header.height,
                    is_genesis=is_genesis,
                    intra_outputs=intra_outputs,
                    spent=spent,
                )
            )

        # --- 6. 重算根并比对块头 ---------------------------------------
        calc_tx_root = tx_root(ids)
        if calc_tx_root != header.tx_root:
            raise MerkleRootMismatchError(
                "tx_root 与交易列表不匹配",
                details={
                    "declared": header.tx_root.hex(),
                    "computed": calc_tx_root.hex(),
                },
            )
        calc_wit_root = witness_root(block.transactions)
        if calc_wit_root != header.witness_root:
            raise MerkleRootMismatchError(
                "witness_root 与交易列表不匹配",
                details={
                    "declared": header.witness_root.hex(),
                    "computed": calc_wit_root.hex(),
                },
            )

        block_id = block_id_of(block)
        plan = BlockPlan(
            block=block,
            block_id=block_id,
            height=header.height,
            results=tuple(results),
            utxo_root_before=root_before,
        )
        self._event(
            stage="block_planned",
            height=header.height,
            block_id=block_id.hex(),
            tx_count=len(results),
            total_fee=plan.total_fee,
            utxo_root_before=root_before.hex(),
        )
        return plan

    # ------------------------------------------------------------------
    def _check_topology(
        self,
        block: Block,
        by_id: dict[bytes, int],
    ) -> None:
        edges_forward, adj = _build_reference_graph(block, by_id)
        if edges_forward:
            i, _j, prev = edges_forward[0]
            raise ForwardReferenceError(
                "交易前向引用了块内后续交易",
                details={"tx_index": i, "outpoint": prev.to_dict()},
                tx_index=i,
            )
        cycle_nodes = _find_cycle_nodes(len(block.transactions), adj)
        if cycle_nodes:
            raise ReferenceCycleError(
                "块内交易依赖图存在环",
                details={"cycle_member_indexes": cycle_nodes},
            )

    # ------------------------------------------------------------------
    def _resolve(
        self,
        prev: Outpoint,
        intra_outputs: dict[bytes, list[tuple[int, TxOutput]]],
        spent: set[tuple[bytes, int]],
        block_height: int,
    ) -> Utxo:
        """把 outpoint 解析为一个当前未花费输出（块内前序交易或链上历史）。

        ``spent`` 是本块暂定花费集合，**同时**覆盖块内输出与链上输出——
        提交尚未落库，链上 UTXO 在块内的重复引用必须靠它拦截。
        """
        key = (prev.txid, prev.vout)
        if key in spent:
            # 链上输出在块内被重复花 / 块内输出被第二笔再花，都报双花
            if intra_outputs.get(prev.txid) is not None:
                raise DoubleSpendError(
                    "块内双花：输出已被块内前序交易花费", details=prev.to_dict()
                )
            raise DoubleSpendError(
                "块内双花：链上输出在本块被重复引用", details=prev.to_dict()
            )
        local = intra_outputs.get(prev.txid)
        if local is not None:
            if prev.vout >= len(local):
                raise UnknownOutpointError(
                    "块内引用的输出索引越界", details=prev.to_dict()
                )
            _, out = local[prev.vout]
            return Utxo(
                txid=prev.txid,
                vout=prev.vout,
                amount=out.amount,
                pubkey=out.pubkey,
                created_height=block_height,
            )
        status = self._view.classify_outpoint(prev.txid, prev.vout)
        if status == "spent":
            raise DoubleSpendError(
                "引用的 outpoint 已在历史链上花费", details=prev.to_dict()
            )
        if status == "unknown":
            raise UnknownOutpointError(
                "引用的 outpoint 不存在", details=prev.to_dict()
            )
        utxo = self._view.get_utxo(prev.txid, prev.vout)
        assert utxo is not None  # classify 为 unspent 时必命中
        return utxo

    # ------------------------------------------------------------------
    def _plan_tx(
        self,
        *,
        index: int,
        tx: Transaction,
        tid: bytes,
        block_height: int,
        is_genesis: bool,
        intra_outputs: dict[bytes, list[tuple[int, TxOutput]]],
        spent: set[tuple[bytes, int]],
    ) -> PlannedTx:
        lim = self._limits

        def fail(err: LedgerError) -> None:
            if err.tx_index is None:
                err.tx_index = index
            self._event(
                stage="tx_rejected",
                tx_index=index,
                txid=tid.hex(),
                category=err.category.value,
                code=err.code,
                reason=err.message,
                details=err.details,
            )
            raise err

        # (1) 见证数量
        if len(tx.witnesses) != len(tx.inputs):
            fail(
                WitnessCountMismatchError(
                    "见证数量必须等于输入数量",
                    details={
                        "inputs": len(tx.inputs),
                        "witnesses": len(tx.witnesses),
                    },
                )
            )
        # (2) 条数资源上限
        if len(tx.inputs) > lim.max_inputs_per_tx:
            raise ResourceLimitError(
                "交易输入数超上限",
                details={
                    "limit_name": "max_inputs_per_tx",
                    "limit": lim.max_inputs_per_tx,
                    "value": len(tx.inputs),
                },
                tx_index=index,
            )
        if len(tx.outputs) > lim.max_outputs_per_tx:
            raise ResourceLimitError(
                "交易输出数超上限",
                details={
                    "limit_name": "max_outputs_per_tx",
                    "limit": lim.max_outputs_per_tx,
                    "value": len(tx.outputs),
                },
                tx_index=index,
            )
        if tx.version != encoding.PROTOCOL_VERSION:
            fail(
                MalformedEncodingError(
                    f"不支持的交易版本 {tx.version}",
                    details={
                        "version": tx.version,
                        "supported": encoding.PROTOCOL_VERSION,
                    },
                )
            )

        # (3) 费用/输出金额范围与零值（带溢出保护累加）
        if not 0 <= tx.fee <= MAX_MONEY:
            fail(
                InvalidFeeError(
                    "费用必须为 [0, MAX_MONEY] 内整数", details={"fee": tx.fee}
                )
            )
        sum_out = 0
        for v, out in enumerate(tx.outputs):
            if out.amount == 0:
                fail(ZeroValueError("禁止零值输出", details={"vout": v}))
            if not 1 <= out.amount <= MAX_MONEY:
                fail(
                    AmountOutOfRangeError(
                        "输出金额越界",
                        details={"vout": v, "amount": out.amount, "max": MAX_MONEY},
                    )
                )
            if len(out.pubkey) != PUBKEY_COMPRESSED_SIZE:
                fail(
                    MalformedEncodingError(
                        "输出公钥必须为 33 字节", details={"vout": v}
                    )
                )
            sum_out = _checked_add(sum_out, out.amount, index, fail)

        # (4) 发行合法性
        if is_genesis:
            if tx.inputs:
                fail(
                    IllegalIssuanceError(
                        "genesis 交易必须为无输入发行交易",
                        details={"inputs": len(tx.inputs)},
                    )
                )
            if tx.fee != 0:
                fail(
                    IllegalIssuanceError(
                        "genesis 交易费用必须为 0", details={"fee": tx.fee}
                    )
                )
        elif not tx.inputs:
            fail(IllegalIssuanceError("非 genesis 块禁止无输入发行交易"))

        # (5) 同一交易内重复输入
        seen: set[tuple[bytes, int]] = set()
        for inp in tx.inputs:
            key = (inp.prev.txid, inp.prev.vout)
            if key in seen:
                fail(
                    DoubleSpendError(
                        "同一交易内重复引用同一 outpoint（重复输入）",
                        details=inp.prev.to_dict(),
                    )
                )
            seen.add(key)

        # (6) 解析输入（未花费）并立即验签
        committed: list[Utxo] = []
        message = sighash_of(tx)
        for k, inp in enumerate(tx.inputs):
            try:
                utxo = self._resolve(
                    inp.prev, intra_outputs, spent, block_height=block_height
                )
            except LedgerError as exc:
                fail(exc)
            committed.append(utxo)
            try:
                crypto_verify(utxo.pubkey, tx.witnesses[k].signature, message)
            except LedgerError as exc:
                fail(exc)

        # (7) 守恒（genesis 发行无输入，价值由"仅 genesis 可发行"规则约束）
        sum_in = 0
        for u in committed:
            sum_in = _checked_add(sum_in, u.amount, index, fail)
        if not is_genesis:
            if sum_in < tx.fee:
                fail(
                    InvalidFeeError(
                        "费用超过输入总额",
                        details={"sum_in": sum_in, "fee": tx.fee},
                    )
                )
            if sum_in != sum_out + tx.fee:
                fail(
                    ConservationMismatchError(
                        "价值不守恒：sum(inputs) != sum(outputs) + fee",
                        details={
                            "sum_in": sum_in,
                            "sum_out": sum_out,
                            "fee": tx.fee,
                            "difference": sum_in - sum_out - tx.fee,
                        },
                    )
                )

        # (8) 落入暂定状态
        for inp in tx.inputs:
            spent.add((inp.prev.txid, inp.prev.vout))
        planned = PlannedTx(
            index=index,
            tx=tx,
            txid=tid,
            committed_inputs=tuple(committed),
            new_outputs=tuple((v, out) for v, out in enumerate(tx.outputs)),
            sum_in=sum_in,
            sum_out=sum_out,
        )
        self._event(
            stage="tx_planned",
            tx_index=index,
            txid=tid.hex(),
            inputs=len(tx.inputs),
            outputs=len(tx.outputs),
            sum_in=sum_in,
            sum_out=sum_out,
            fee=tx.fee,
        )
        return planned


def _checked_add(acc: int, value: int, tx_index: int, fail: Any) -> int:
    """有界整数累加，超过 MAX_MONEY 即 AMOUNT_OVERFLOW（溢出保护）。"""
    if acc > MAX_MONEY - value:
        fail(
            AmountOverflowError(
                "金额累加超过 MAX_MONEY（溢出保护）",
                details={"partial_sum": acc, "addend": value, "max": MAX_MONEY},
                tx_index=tx_index,
            )
        )
    return acc + value
