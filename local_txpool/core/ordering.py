"""候选区块排序引擎（模块职责：给定状态快照，产出确定的执行顺序）。

规则（与题述一致）
------------------
1. **费用高不能跳过自身 nonce 缺口**：每个发送者只有 nonce 序上的"队头"
   交易有资格参与全局费竞争；队头被选入/阻塞后，下一笔才可能暴露。
   因此 queued（缺口之后）以及 pending 中排在后面的交易即使 gas_price
   全池最高也不会越过前序交易。
2. 全局每轮在所有合格队头中取 gas_price 最高者（费竞争）；同价按
   ``received_at_ms`` 升序、tx_hash 升序，保证跨机器/跨回放完全确定。
3. 受 block gas limit 约束：当前装不下的队头让位，先尝试其它发送者；
   若一轮无任何进展即终止，装不下者记录 ``gas_fills_block``。
4. 余额在计划期内逐笔扣减；某队头余额不足时，该发送者在本块内阻塞，
   记录 ``balance_exhausted``，其后交易一并不可选（缺口形成）。

引擎是纯函数：输入不可变快照、输出有序计划，不写库，便于独立测试与回放。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import StoredTransaction


@dataclass(frozen=True)
class SkipReason:
    tx_hash: str
    reason: str  # gas_fills_block | balance_exhausted
    detail: dict[str, int | str] = field(default_factory=dict)


@dataclass
class CandidatePlan:
    ordered: list[StoredTransaction] = field(default_factory=list)
    skipped: list[SkipReason] = field(default_factory=list)
    gas_used: int = 0

    def tx_hashes(self) -> list[str]:
        return [t.tx.tx_hash for t in self.ordered]


@dataclass(frozen=True)
class SenderContext:
    """调用方（kernel）提供的账户执行前快照。"""

    address: str
    nonce: int               # 下一个可执行 nonce
    available_balance: int   # 执行本块时可花费的余额


@dataclass(frozen=True)
class OrderingConfig:
    block_gas_limit: int


def _price_key(stored: StoredTransaction) -> tuple[int, int, str]:
    # 全局选择按 gas_price 降序（取负）、到达时间升序、哈希升序。
    return (-stored.tx.gas_price, stored.received_at_ms, stored.tx.tx_hash)


def build_candidate_plan(
    pending: list[StoredTransaction],
    *,
    sender_ctx: dict[str, SenderContext],
    config: OrderingConfig,
) -> CandidatePlan:
    """以"每发送者 nonce 队头 + 全局最高价"的方式构造执行计划。"""

    plan = CandidatePlan()

    # 每个发送者的 pending 严格按 nonce 排序（pool 查询只保证价格序，
    # 这里必须自行重排以守住 nonce 边界）。
    queues: dict[str, list[StoredTransaction]] = {}
    for stored in pending:
        queues.setdefault(stored.tx.sender, []).append(stored)
    for items in queues.values():
        items.sort(key=lambda s: s.tx.nonce)

    head_index: dict[str, int] = {a: 0 for a in queues}
    remaining_balance: dict[str, int] = {
        a: ctx.available_balance for a, ctx in sender_ctx.items()
    }
    next_nonce: dict[str, int] = {
        a: ctx.nonce for a, ctx in sender_ctx.items()
    }
    blocked: dict[str, SkipReason] = {}
    gas_remaining = config.block_gas_limit

    def current_head(sender: str) -> StoredTransaction | None:
        items = queues.get(sender, [])
        idx = head_index[sender]
        return items[idx] if idx < len(items) else None

    while True:
        # 收集所有发送者的合格队头。
        eligible: list[StoredTransaction] = []
        gas_waiting: list[StoredTransaction] = []
        for sender, items in queues.items():
            if sender in blocked or head_index[sender] >= len(items):
                continue
            head = items[head_index[sender]]
            # 防御：kernel 应保证 pending 连续从账户 nonce 起；若不连续则阻塞。
            if sender not in next_nonce or head.tx.nonce != next_nonce[sender]:
                blocked[sender] = SkipReason(
                    tx_hash=head.tx.tx_hash,
                    reason="nonce_gap",
                    detail={
                        "nonce": head.tx.nonce,
                        "expected": next_nonce.get(sender, -1),
                    },
                )
                continue
            cost = head.tx.gas_limit * head.tx.gas_price + head.tx.value
            if remaining_balance[sender] < cost:
                blocked[sender] = SkipReason(
                    tx_hash=head.tx.tx_hash,
                    reason="balance_exhausted",
                    detail={
                        "cost": cost,
                        "available": remaining_balance[sender],
                    },
                )
                continue
            if head.tx.gas_limit > gas_remaining:
                gas_waiting.append(head)
            else:
                eligible.append(head)

        if not eligible:
            # 剩余队头都装不下当前 gas 余量 -> 收敛结束。
            for head in gas_waiting:
                plan.skipped.append(
                    SkipReason(
                        tx_hash=head.tx.tx_hash,
                        reason="gas_fills_block",
                        detail={
                            "gas_limit": head.tx.gas_limit,
                            "gas_remaining": gas_remaining,
                        },
                    )
                )
            break

        choice = min(eligible, key=_price_key)
        sender = choice.tx.sender
        cost = choice.tx.gas_limit * choice.tx.gas_price + choice.tx.value
        plan.ordered.append(choice)
        plan.gas_used += choice.tx.gas_limit
        gas_remaining -= choice.tx.gas_limit
        remaining_balance[sender] -= cost
        next_nonce[sender] = choice.tx.nonce + 1
        head_index[sender] += 1

    # 被阻塞发送者：其队头及队列中剩余交易全部不可执行。
    blocked_hashes: set[str] = set()
    for sender, reason in blocked.items():
        blocked_hashes.add(reason.tx_hash)
        plan.skipped.append(reason)
        for rest in queues[sender][head_index[sender] + 1:]:
            blocked_hashes.add(rest.tx.tx_hash)
            plan.skipped.append(
                SkipReason(
                    tx_hash=rest.tx.tx_hash,
                    reason=reason.reason,
                    detail={"blocked_by_head": reason.tx_hash},
                )
            )

    plan.skipped.sort(key=lambda r: (r.tx_hash, r.reason))
    return plan
