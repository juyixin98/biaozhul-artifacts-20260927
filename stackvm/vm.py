"""受限栈虚拟机。

资源限制（全部在执行前/执行中强制）：
- 操作预算：加权步数，超额 → BUDGET_EXHAUSTED；
- 栈元素 ≤ max_element_bytes、栈项数 ≤ max_stack_items；
- 脚本长度 ≤ max_script_bytes（解码期检查）；
- IF/NOTIF 嵌套 ≤ max_if_depth；脚本上下文深度 ≤ max_script_depth
  （解锁段=1、锁定段=2；不存在 OP_EVAL 等动态求值入口）。

成功条件：没有分类失败，且结束时栈上恰好一个“真”元素（干净栈）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import crypto
from . import hashes as H
from . import opcodes as O
from .config import Limits
from .errors import FailCode, VmFailure
from .script import (
    Instruction,
    cast_bool,
    decode_scriptnum,
    encode_scriptnum,
    parse,
)
from .transaction import Transaction


@dataclass
class TraceEvent:
    pc: int
    op: str
    offset: int
    budget_left: int
    active: bool
    stack: list[str] = field(default_factory=list)
    alt: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class VMResult:
    ok: bool
    code: FailCode
    detail: str
    budget_left: int
    final_stack: list[bytes]
    trace: list[TraceEvent]
    checks: list[dict] = field(default_factory=list)  # 验签中间结果（供交叉核对）

    @property
    def kind(self):
        from .errors import kind_of
        return kind_of(self.code)


_COSTS: dict[int, int] = {
    O.Op.OP_NOP: 1,
    O.Op.OP_IF: 1, O.Op.OP_NOTIF: 1, O.Op.OP_ELSE: 1, O.Op.OP_ENDIF: 1,
    O.Op.OP_VERIFY: 1, O.Op.OP_RETURN: 1,
    O.Op.OP_TOALTSTACK: 1, O.Op.OP_FROMALTSTACK: 1,
    O.Op.OP_DROP: 1, O.Op.OP_DUP: 1, O.Op.OP_SWAP: 1, O.Op.OP_SIZE: 1,
    O.Op.OP_EQUAL: 1, O.Op.OP_EQUALVERIFY: 1, O.Op.OP_ADD: 1,
    O.Op.OP_RIPEMD160: 6, O.Op.OP_SHA1: 4, O.Op.OP_SHA256: 6,
    O.Op.OP_HASH160: 12, O.Op.OP_HASH256: 12,
    O.Op.OP_CHECKSIG: 20, O.Op.OP_CHECKSIGVERIFY: 20,
    O.Op.OP_CHECKMULTISIG: 40, O.Op.OP_CHECKMULTISIGVERIFY: 40,
}
# 每次实际执行的公钥验签额外成本（防止用大量钥匙磨预算）
_PER_SIG_COST = 8


class Machine:
    """绑定一笔交易执行解锁+锁定脚本的机器。"""

    def __init__(self, tx: Transaction, digest: bytes, limits: Limits | None = None):
        self.tx = tx
        self.digest = digest
        self.limits = limits or Limits()
        self.stack: list[bytes] = []
        self.alt: list[bytes] = []
        self.budget = self.limits.op_budget
        self.trace: list[TraceEvent] = []
        self.checks: list[dict] = []

    # ------------------------------ 基础工具 ------------------------------

    def _snapshot(self) -> tuple[list[str], list[str]]:
        return [x.hex() for x in self.stack], [x.hex() for x in self.alt]

    def _charge(self, cost: int, what: str) -> None:
        if self.budget < cost:
            raise VmFailure(
                FailCode.BUDGET_EXHAUSTED,
                f"{what} 需要 {cost} 步，仅剩 {self.budget} 步（预算 {self.limits.op_budget}）",
            )
        self.budget -= cost

    def _push(self, value: bytes) -> None:
        if len(value) > self.limits.max_element_bytes:
            raise VmFailure(
                FailCode.ELEMENT_TOO_LARGE,
                f"压入元素 {len(value)} 字节超过上限 {self.limits.max_element_bytes}",
            )
        if len(self.stack) >= self.limits.max_stack_items:
            raise VmFailure(
                FailCode.STACK_TOO_LARGE,
                f"栈项数达到上限 {self.limits.max_stack_items}",
            )
        self.stack.append(value)

    def _pop(self) -> bytes:
        if not self.stack:
            raise VmFailure(FailCode.STACK_UNDERFLOW, "尝试从空栈弹出元素")
        return self.stack.pop()

    def _need(self, n: int, op_name: str) -> None:
        if len(self.stack) < n:
            raise VmFailure(
                FailCode.STACK_UNDERFLOW,
                f"{op_name} 需要 {n} 个栈元素，实际 {len(self.stack)}",
            )

    # ------------------------------ 执行入口 ------------------------------

    def execute(self, script: bytes, depth: int) -> None:
        """在给定脚本上下文深度执行一段脚本（解锁段 depth=1，锁定段 depth=2）。"""
        if depth > self.limits.max_script_depth:
            raise VmFailure(
                FailCode.SCRIPT_DEPTH_EXCEEDED,
                f"脚本上下文深度 {depth} 超过上限 {self.limits.max_script_depth}",
            )
        instructions = parse(script, max_script_bytes=self.limits.max_script_bytes)
        branches: list[dict] = []  # {entered, executed_branch, has_else}
        pc = 0
        n = len(instructions)

        while pc < n:
            ins = instructions[pc]
            active = all(b["entered"] and b["executing"] for b in branches)

            # 结构/资源检查对非活跃分支同样生效（解析器已查未知码与截断）
            if ins.is_push and ins.data is not None and \
                    len(ins.data) > self.limits.max_element_bytes:
                raise VmFailure(
                    FailCode.ELEMENT_TOO_LARGE,
                    f"偏移 {ins.offset}: 压入元素 {len(ins.data)} 字节超过上限 "
                    f"{self.limits.max_element_bytes}",
                )

            if active:
                cost = 1 if ins.is_push else _COSTS.get(ins.op, 1)
                self._charge(cost, ins.name)
                try:
                    self._dispatch(ins, branches, pc)
                except VmFailure as fail:
                    # 失败指令同样记录（note 带判定理由），保证日志可定位最后一步
                    st, al = self._snapshot()
                    self.trace.append(
                        TraceEvent(pc, ins.name, ins.offset, self.budget, True,
                                   st, al, note=f"FAIL: {fail.code.value}: {fail.detail}"))
                    raise
                st, al = self._snapshot()
                self.trace.append(
                    TraceEvent(pc, ins.name, ins.offset, self.budget, True, st, al))
            else:
                # 非活跃分支：只允许结构性流程指令改变分支状态，且只计 1 步
                if ins.op in (O.Op.OP_IF, O.Op.OP_NOTIF,
                              O.Op.OP_ELSE, O.Op.OP_ENDIF):
                    self._charge(1, ins.name)
                    self._branch(ins, branches, condition_available=False)
                    st, al = self._snapshot()
                    self.trace.append(
                        TraceEvent(pc, ins.name, ins.offset, self.budget, False, st, al,
                                   note="inactive branch"))
            pc += 1

        if branches:
            raise VmFailure(
                FailCode.UNBALANCED_CONDITIONAL,
                f"脚本结束时仍有 {len(branches)} 个未闭合的 IF/NOTIF",
            )

    # ------------------------------ 条件分支 ------------------------------

    @staticmethod
    def _active(branches: list[dict]) -> bool:
        return all(b["entered"] and b["executing"] for b in branches)

    def _branch(self, ins: Instruction, branches: list[dict],
                condition_available: bool) -> None:
        op = ins.op
        if op in (O.Op.OP_IF, O.Op.OP_NOTIF):
            if len(branches) >= self.limits.max_if_depth:
                raise VmFailure(
                    FailCode.CONDITION_DEPTH_EXCEEDED,
                    f"条件嵌套达到 {len(branches) + 1}，超过上限 {self.limits.max_if_depth}",
                )
            if self._active(branches) and condition_available:
                cond = cast_bool(self._pop())
                want_true = cond if op == O.Op.OP_IF else not cond
                branches.append({"entered": True, "executing": want_true,
                                 "has_else": False})
            else:
                # 处于外层非活跃分支：新分支整体不活跃
                branches.append({"entered": False, "executing": False,
                                 "has_else": False})
        elif op == O.Op.OP_ELSE:
            if not branches:
                raise VmFailure(FailCode.UNBALANCED_CONDITIONAL, "ELSE 没有匹配的 IF")
            top = branches[-1]
            if top["has_else"]:
                raise VmFailure(FailCode.UNBALANCED_CONDITIONAL, "同一分支出现第二个 ELSE")
            top["has_else"] = True
            if top["entered"]:
                top["executing"] = not top["executing"]
        elif op == O.Op.OP_ENDIF:
            if not branches:
                raise VmFailure(FailCode.UNBALANCED_CONDITIONAL, "ENDIF 没有匹配的 IF")
            branches.pop()

    # ------------------------------ 指令分派 ------------------------------

    def _dispatch(self, ins: Instruction, branches: list[dict], pc: int) -> None:
        op = ins.op

        if ins.is_push:
            self._do_push(ins)
            return

        if op in (O.Op.OP_IF, O.Op.OP_NOTIF):
            self._branch(ins, branches, condition_available=True)
            return
        if op in (O.Op.OP_ELSE, O.Op.OP_ENDIF):
            self._branch(ins, branches, condition_available=False)
            return

        handler = {
            O.Op.OP_NOP: self._nop,
            O.Op.OP_VERIFY: self._verify,
            O.Op.OP_RETURN: self._return,
            O.Op.OP_TOALTSTACK: self._to_alt,
            O.Op.OP_FROMALTSTACK: self._from_alt,
            O.Op.OP_DROP: lambda: self._pop(),
            O.Op.OP_DUP: self._dup,
            O.Op.OP_SWAP: self._swap,
            O.Op.OP_SIZE: self._size,
            O.Op.OP_EQUAL: self._equal,
            O.Op.OP_EQUALVERIFY: self._equal_verify,
            O.Op.OP_ADD: self._add,
            O.Op.OP_RIPEMD160: lambda: self._hash(H.ripemd160),
            O.Op.OP_SHA1: lambda: self._hash(H.sha1),
            O.Op.OP_SHA256: lambda: self._hash(H.sha256),
            O.Op.OP_HASH160: lambda: self._hash(H.hash160),
            O.Op.OP_HASH256: lambda: self._hash(H.hash256),
            O.Op.OP_CHECKSIG: lambda: self._checksig(verify_form=True),
            O.Op.OP_CHECKSIGVERIFY: lambda: self._checksig(verify_form=False),
            O.Op.OP_CHECKMULTISIG: lambda: self._checkmultisig(verify_form=True),
            O.Op.OP_CHECKMULTISIGVERIFY: lambda: self._checkmultisig(verify_form=False),
        }.get(op)
        if handler is None:  # 解析器保证不会到达
            raise VmFailure(FailCode.UNKNOWN_OPCODE, f"未实现的操作码 0x{op:02x}")
        handler()

    def _do_push(self, ins: Instruction) -> None:
        if ins.op == O.Op.OP_0:
            self._push(b"")
        elif O.OP_1 <= ins.op <= O.OP_16:
            self._push(encode_scriptnum(ins.op - O.OP_1 + 1))
        else:
            self._push(ins.data or b"")

    def _nop(self) -> None:
        return

    def _verify(self) -> None:
        top = self._pop()
        if not cast_bool(top):
            raise VmFailure(FailCode.EVAL_FALSE, "OP_VERIFY 栈顶为假")

    def _return(self) -> None:
        raise VmFailure(FailCode.OP_RETURN_EXECUTED, "活跃分支执行到 OP_RETURN")

    def _to_alt(self) -> None:
        self.alt.append(self._pop())

    def _from_alt(self) -> None:
        if not self.alt:
            raise VmFailure(FailCode.STACK_UNDERFLOW, "备用栈为空时执行 OP_FROMALTSTACK")
        self._push(self.alt.pop())

    def _dup(self) -> None:
        if not self.stack:
            raise VmFailure(FailCode.STACK_UNDERFLOW, "OP_DUP 时空栈")
        self._push(self.stack[-1])

    def _swap(self) -> None:
        self._need(2, "OP_SWAP")
        self.stack[-1], self.stack[-2] = self.stack[-2], self.stack[-1]

    def _size(self) -> None:
        self._need(1, "OP_SIZE")
        self._push(encode_scriptnum(len(self.stack[-1])))

    def _equal(self) -> None:
        self._need(2, "OP_EQUAL")
        a = self._pop()
        b = self._pop()
        self._push(encode_scriptnum(1) if a == b else b"")

    def _equal_verify(self) -> None:
        self._need(2, "OP_EQUALVERIFY")
        a = self._pop()
        b = self._pop()
        if a != b:
            raise VmFailure(FailCode.EVAL_FALSE, "OP_EQUALVERIFY 两元素不等")

    def _add(self) -> None:
        self._need(2, "OP_ADD")
        a = decode_scriptnum(self._pop(), max_bytes=self.limits.max_script_num_bytes)
        b = decode_scriptnum(self._pop(), max_bytes=self.limits.max_script_num_bytes)
        self._push(encode_scriptnum(a + b, max_bytes=self.limits.max_script_num_bytes))

    def _hash(self, fn) -> None:
        x = self._pop()
        self._push(fn(x))

    # ------------------------------ 验签 ------------------------------

    def _checksig(self, *, verify_form: bool) -> None:
        self._need(2, "OP_CHECKSIG")
        pub = self._pop()
        sig = self._pop()
        # 结构性解析失败直接 SIG_INVALID（不产生“可压 0”的歧义）
        crypto.parse_pubkey(pub)
        crypto.parse_signature(sig)
        ok = crypto.verify(pub, sig, self.digest)
        self.checks.append({"op": "CHECKSIG", "pub": pub.hex(),
                            "sig": sig.hex(), "digest": self.digest.hex(),
                            "result": ok})
        if not ok:
            raise VmFailure(
                FailCode.SIG_INVALID,
                "OP_CHECKSIG：签名未通过（摘要/域标签/公钥不匹配）",
            )
        if not verify_form:
            return  # OP_CHECKSIGVERIFY：通过即清空，不留元素
        self._push(encode_scriptnum(1))

    def _checkmultisig(self, *, verify_form: bool) -> None:
        """栈布局（栈顶在上，注意与 Bitcoin 分叉：无 dummy 元素）：

            ... sig(m) ... sig(1) m pub(n) ... pub(1) n
        弹栈顺序：n, pub1..pubn（脚本顺序），m, sig1..sigm（脚本顺序）。
        匹配采用 Bitcoin 式有序贪心：签名按序前进匹配公钥；同一公钥不得
        被第二个签名使用（重复计数 → SIG_DUPLICATED）。
        """
        self._need(1, "OP_CHECKMULTISIG")
        n = decode_scriptnum(self._pop(), max_bytes=self.limits.max_script_num_bytes)
        if n < 1 or n > self.limits.max_multisig_n:
            raise VmFailure(
                FailCode.MULTISIG_MALFORMED,
                f"公钥数 n={n} 超出 1..{self.limits.max_multisig_n}",
            )
        self._need(n + 1, "OP_CHECKMULTISIG")
        pubs = [self._pop() for _ in range(n)]  # pop 顺序即脚本中 pub1..pubn
        m = decode_scriptnum(self._pop(), max_bytes=self.limits.max_script_num_bytes)
        if m < 0 or m > n:
            raise VmFailure(
                FailCode.MULTISIG_MALFORMED,
                f"门槛 m={m} 非法（要求 0≤m≤n={n}）",
            )
        self._need(m, "OP_CHECKMULTISIG")
        sigs = [self._pop() for _ in range(m)]

        for pub in pubs:
            crypto.parse_pubkey(pub)
        seen_sigs: set[bytes] = set()
        for sig in sigs:
            crypto.parse_signature(sig)
            if sig in seen_sigs:
                raise VmFailure(FailCode.SIG_DUPLICATED,
                                "同一签名在 CHECKMULTISIG 中被重复提交")
            seen_sigs.add(sig)
        if len({p for p in pubs}) != n:
            raise VmFailure(FailCode.SIG_DUPLICATED,
                            "CHECKMULTISIG 公钥列表含重复公钥")

        # 预算按“可能的验签次数”提前收取（签名数*钥匙数的上界）
        self._charge(m * n * _PER_SIG_COST, "CHECKMULTISIG 验签")

        matched_keys: set[bytes] = set()
        key_cursor = 0
        for si, sig in enumerate(sigs):
            where = None
            for ki in range(key_cursor, n):
                ok = crypto.verify(pubs[ki], sig, self.digest)
                self.checks.append({
                    "op": "CHECKMULTISIG", "sig_index": si, "key_index": ki,
                    "pub": pubs[ki].hex(), "sig": sig.hex(),
                    "digest": self.digest.hex(), "result": ok,
                })
                if ok:
                    where = ki
                    break
            if where is None:
                # 没有任何剩余公钥能验证该签名
                raise VmFailure(
                    FailCode.THRESHOLD_NOT_MET,
                    f"第 {si + 1}/{m} 个签名无法匹配剩余公钥（顺序/摘要/域标签问题）",
                )
            if pubs[where] in matched_keys:
                raise VmFailure(
                    FailCode.SIG_DUPLICATED,
                    f"公钥 index={where} 已被前一签名计数，禁止重复计数",
                )
            matched_keys.add(pubs[where])
            key_cursor = where + 1

        # 走到这里 m 个签名全部匹配成功（m==0 视为空门槛成功）
        if not verify_form:
            return
        self._push(encode_scriptnum(1))


def run_scripts(tx: Transaction, digest: bytes,
                unlock: bytes, lock: bytes,
                limits: Limits | None = None) -> VMResult:
    """两段式执行：先解锁段（depth=1），保留栈，再锁定段（depth=2）。"""
    machine = Machine(tx, digest, limits)
    lim = machine.limits
    try:
        if lim.push_only_unlock:
            from .script import assert_push_only
            assert_push_only(unlock, max_script_bytes=lim.max_script_bytes)
        machine.execute(unlock, depth=1)
        machine.execute(lock, depth=2)
        if lim.require_clean_stack:
            if len(machine.stack) != 1 or not cast_bool(machine.stack[0]):
                raise VmFailure(
                    FailCode.UNCLEAN_STACK,
                    f"结束时栈状态不满足“唯一真元素”：{len(machine.stack)} 个元素，"
                    f"栈顶={machine.stack[-1].hex() if machine.stack else '<空>'}",
                )
    except VmFailure as fail:
        return VMResult(ok=False, code=fail.code, detail=fail.detail,
                        budget_left=machine.budget,
                        final_stack=list(machine.stack), trace=machine.trace,
                        checks=machine.checks)
    return VMResult(ok=True, code=FailCode.OK, detail="验证通过",
                    budget_left=machine.budget, final_stack=list(machine.stack),
                    trace=machine.trace, checks=machine.checks)
