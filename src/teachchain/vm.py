"""确定性虚拟机：u64 栈机 + 内存 + 账户存储 + 嵌套调用。

本模块刻意**不导入** time/random/datetime 等宿主源；其输出仅由
(代码, 输入数据, 已提交状态, gas 上限) 决定。两个独立进程重放同一输入，
栈轨迹/写集/gas/输出必须完全一致（见 tests/test_replay_cross_process.py）。

回滚边界
--------
每次 CALL 都是一个独立帧：

* 帧内 SSTORE 只写帧的本地写集；读取沿「当前帧 → 父帧 → … → 已提交状态」穿透；
* 帧成功结束时，写集合并进父帧，未用完的 gas 退回父帧；
* 帧 REVERT / 异常中止时写集整体丢弃，父帧以状态码 0 继续执行；
  REVERT 退回未用完 gas，异常中止（如 out_of_gas）不退回已转发 gas。

整数语义
--------
所有运算在 u64 上：加减乘模 2**64 回绕；DIV/MOD 除以 0 得 0（非崩溃）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import gas as G
from .errors import Halt, Revert
from .opcodes import NAMES, Op, decode

MOD64 = 1 << 64
MASK64 = MOD64 - 1
MAX_RETURN_WORDS = 64
MAX_CALL_DEPTH = 32


@dataclass
class VmResult:
    """一次根执行结果（不含交易内在费用，内在费用由 kernel 计）。"""

    status: int  # 1 成功 / 0 回滚
    output: list[int]
    gas_used: int
    gas_refund: int         # 成功路径产生的待退 gas（清零写退款）
    halt_code: str | None   # status==0 且非 REVERT 时给出失败类别
    reverted: bool          # 是否程序主动 REVERT
    writes: dict[tuple[str, int], int]
    trace: list[str] = field(default_factory=list)


class _FrameExit(Exception):
    """帧正常结束的内部信号（STOP / 走到末尾 / RETURN）。"""

    def __init__(self, output: list[int]) -> None:
        self.output = output
        super().__init__("frame exit")


@dataclass
class Frame:
    address: str
    instrs: list
    jumpdests: frozenset[int]
    calldata: list[int]
    writes: dict[tuple[str, int], int]
    pc: int = 0
    stack: list[int] = field(default_factory=list)
    memory: list[int] = field(default_factory=list)  # 按字索引，惰性分配
    mem_words: int = 0
    gas: int = 0
    depth: int = 0
    refund_delta: int = 0  # 本帧（含已成功合并的子孙帧）产生的清零退款
    _offset_map: dict[int, int] = field(default_factory=dict)

    def name(self, op: int) -> str:
        return NAMES.get(op, f"0x{op:02x}")


class VirtualMachine:
    """执行器。输出只依赖输入，不触碰宿主时间/随机源。"""

    def __init__(self, codes: dict[str, bytes], trace: bool = False) -> None:
        self.codes = codes
        self.trace_enabled = trace

    # ---- 入口 ----
    def execute(
        self,
        address: str,
        code: bytes,
        calldata: list[int] | None,
        gas_limit: int,
        committed: dict[tuple[str, int], int],
    ) -> VmResult:
        root = self._make_frame(address, code, list(calldata or []), gas_limit, {}, 0)
        frames: list[Frame] = [root]
        log: list[str] = []

        while True:
            f = frames[-1]
            try:
                if f.pc >= len(f.instrs):
                    if self.trace_enabled:
                        log.append(f"d{f.depth} STOP(end) gas={f.gas}")
                    raise _FrameExit([])
                ins = f.instrs[f.pc]
                if self.trace_enabled:
                    log.append(
                        f"d{f.depth} {f.name(ins.op)} pc={ins.pc} gas={f.gas} "
                        f"stack={len(f.stack)} mem={f.mem_words}"
                    )
                self._step(f, frames, ins, committed)
            except _FrameExit as done:
                if f.depth == 0:
                    used = gas_limit - f.gas
                    return VmResult(1, done.output, used, f.refund_delta,
                                    None, False, dict(f.writes), log)
                self._merge_success(frames, f, done.output)
            except Revert as rev:
                if f.depth == 0:
                    used = gas_limit - f.gas
                    return VmResult(0, rev.output, used, 0, "revert", True, {}, log)
                self._drop_child(frames, f, reverted=True,
                                 output=rev.output, gas_back=f.gas)
            except Halt as h:
                if f.depth == 0:
                    return VmResult(0, [], gas_limit, 0, h.code, False, {}, log)
                self._drop_child(frames, f, reverted=False,
                                 output=[], gas_back=0, halt_code=h.code)

    # ---- 帧管理 ----
    def _make_frame(self, address, code, calldata, gas, writes, depth) -> Frame:
        instrs = decode(code)
        jd = frozenset(i.pc for i in instrs if i.op == Op.JUMPDEST)
        offset_map = {ins.pc: i for i, ins in enumerate(instrs)}
        return Frame(address=address, instrs=instrs, jumpdests=jd, calldata=calldata,
                     writes=writes, gas=gas, depth=depth, _offset_map=offset_map)

    def _merge_success(self, frames: list[Frame], child: Frame, output: list[int]) -> None:
        frames.pop()
        parent = frames[-1]
        parent.gas += child.gas
        parent.writes.update(child.writes)
        parent.refund_delta += child.refund_delta
        # CALL 已弹 3 个操作数；压 status, ret0，然后跳过 CALL 指令
        parent.stack.append(1)
        parent.stack.append(output[0] if output else 0)
        parent.pc += 1

    def _drop_child(self, frames, child, *, reverted, output, gas_back, halt_code=None) -> None:
        frames.pop()
        parent = frames[-1]
        parent.gas += gas_back  # REVERT 退回剩余；halt 为 0
        parent.stack.append(0)
        parent.stack.append(output[0] if output else 0)
        parent.pc += 1

    # ---- 单步 ----
    def _step(self, f: Frame, frames: list[Frame], ins, committed) -> None:
        op = ins.op
        self._charge_flat(f, op)

        if op == Op.STOP:
            raise _FrameExit([])
        if op == Op.JUMPDEST:
            pass  # 合法跳转落点，无副作用
        elif op == Op.INVALID:
            raise Halt("invalid_instruction")

        elif op == Op.PUSH:
            self._push(f, ins.imm)
        elif op in (Op.ADD, Op.SUB, Op.MUL, Op.DIV, Op.MOD):
            # _pop2 返回 (次顶, 栈顶)；栈顶为第一操作数 a，次顶为 b，计算 a op b
            b, a = self._pop2(f)
            if op == Op.ADD:
                v = (a + b) & MASK64
            elif op == Op.SUB:
                v = (a - b) & MASK64
            elif op == Op.MUL:
                v = (a * b) & MASK64
            elif op == Op.DIV:
                v = 0 if b == 0 else a // b
            else:
                v = 0 if b == 0 else a % b
            self._push(f, v)
        elif op in (Op.LT, Op.GT, Op.EQ):
            b, a = self._pop2(f)  # a=栈顶(第一操作数), b=次顶
            self._push(f, int(a < b) if op == Op.LT
                       else int(a > b) if op == Op.GT else int(a == b))
        elif op == Op.ISZERO:
            self._push(f, int(self._pop(f) == 0))
        elif op == Op.POP:
            self._pop(f)
        elif op == Op.DUP:
            n = ins.imm
            if len(f.stack) < n:
                raise Halt("stack_underflow")
            self._push(f, f.stack[-n])
        elif op == Op.SWAP:
            n = ins.imm
            if len(f.stack) < n + 1:
                raise Halt("stack_underflow")
            f.stack[-1], f.stack[-1 - n] = f.stack[-1 - n], f.stack[-1]
        elif op == Op.JUMP:
            target = self._pop(f)
            self._jump(f, target)
            return
        elif op == Op.JUMPI:
            # pop2 返回 (次顶=cond, 栈顶=dest)
            cond, dest = self._pop2(f)
            if cond != 0:
                self._jump(f, dest)
                return
        elif op == Op.CALLDATASIZE:
            self._push(f, len(f.calldata))
        elif op == Op.CALLDATALOAD:
            idx = self._pop(f)
            self._push(f, f.calldata[idx] if 0 <= idx < len(f.calldata) else 0)
        elif op == Op.MSIZE:
            self._push(f, f.mem_words)
        elif op == Op.MLOAD:
            # 栈顶 = offset
            offset = self._pop(f)
            self._expand_memory(f, offset + 1)
            self._push(f, f.memory[offset] if offset < len(f.memory) else 0)
        elif op == Op.MSTORE:
            # EVM: 栈顶=offset、次顶=value；_pop2 返回 (次顶, 栈顶)
            value, offset = self._pop2(f)
            self._expand_memory(f, offset + 1)
            if offset >= len(f.memory):
                f.memory.extend([0] * (offset + 1 - len(f.memory)))
            f.memory[offset] = value & MASK64
        elif op == Op.SLOAD:
            key = self._pop(f)
            self._push(f, self._read_storage(frames, committed, f.address, key))
        elif op == Op.SSTORE:
            # EVM: 栈顶=key、次顶=value
            value, key = self._pop2(f)
            value &= MASK64
            current = self._read_storage(frames, committed, f.address, key)
            cost, refund_inc = G.sstore_cost(current, value)
            self._charge(f, cost, "sstore")
            f.writes[(f.address, key)] = value
            f.refund_delta += refund_inc
        elif op == Op.RETURN:
            # EVM: 栈顶=offset、次顶=length；_pop2 返回 (次顶=length, 栈顶=offset)
            length, offset = self._pop2(f)
            raise _FrameExit(self._read_output(f, offset, length))
        elif op == Op.REVERT:
            length, offset = self._pop2(f)
            raise Revert(self._read_output(f, offset, length))
        elif op == Op.CALL:
            self._do_call(f, frames)
            return
        else:  # pragma: no cover —— decode 已挡住未知字节
            raise Halt("invalid_instruction")

        f.pc += 1

    def _do_call(self, f: Frame, frames: list[Frame]) -> None:
        # 栈布局（栈顶起）：gas, slot, addr —— 三次单弹
        gas_in = self._pop(f)
        slot = self._pop(f)
        addr_word = self._pop(f)
        addr = f"0x{addr_word:016x}"
        # 调用深度保护：仅消耗 CALL 固定费，返回状态码 0
        if f.depth + 1 >= MAX_CALL_DEPTH:
            self._push(f, 0)
            self._push(f, 0)
            f.pc += 1
            return
        code = self.codes.get(addr)
        if code is None:
            # 无代码账户：成功空操作（教学链不做转账）
            self._push(f, 1)
            self._push(f, 0)
            f.pc += 1
            return
        # 63/64 转发规则（EIP-150 风格），保证父帧至少保留 1/64
        forward = min(gas_in, f.gas * 63 // 64)
        f.gas -= forward
        child = self._make_frame(addr, code, [], forward, {}, f.depth + 1)
        frames.append(child)

    # ---- 辅助 ----
    def _charge_flat(self, f: Frame, op: int) -> None:
        table = {
            Op.STOP: G.COST_STOP, Op.JUMPDEST: G.COST_JUMPDEST,
            Op.POP: G.COST_POP, Op.PUSH: G.COST_PUSH,
            Op.DUP: G.COST_DUP, Op.SWAP: G.COST_SWAP,
            Op.MLOAD: G.COST_MLOAD, Op.MSTORE: G.COST_MSTORE,
            Op.MSIZE: G.COST_MSIZE,
            Op.ADD: G.COST_ARITH, Op.SUB: G.COST_ARITH, Op.MUL: G.COST_ARITH,
            Op.DIV: G.COST_ARITH, Op.MOD: G.COST_ARITH,
            Op.LT: G.COST_COMPARE, Op.GT: G.COST_COMPARE,
            Op.EQ: G.COST_COMPARE, Op.ISZERO: G.COST_COMPARE,
            Op.CALLDATALOAD: G.COST_CALLDATALOAD,
            Op.CALLDATASIZE: G.COST_CALLDATASIZE,
            Op.JUMP: G.COST_JUMP, Op.JUMPI: G.COST_JUMPI,
            Op.SLOAD: G.COST_SLOAD,
            Op.SSTORE: 0,   # 费用与操作数相关，分支内扣
            Op.CALL: G.COST_CALL,
            Op.RETURN: G.COST_RETURN, Op.REVERT: G.COST_REVERT,
            Op.INVALID: 0,
        }
        self._charge(f, table[op], NAMES[op])

    def _charge(self, f: Frame, amount: int, what: str) -> None:
        if f.gas < amount:
            raise Halt("out_of_gas")
        f.gas -= amount

    def _expand_memory(self, f: Frame, need_words: int) -> None:
        if need_words <= f.mem_words:
            return
        self._charge(f, G.memory_expansion_delta(f.mem_words, need_words),
                     "memory_expand")
        f.mem_words = need_words

    def _push(self, f: Frame, v: int) -> None:
        if len(f.stack) >= G.MAX_STACK:
            raise Halt("stack_overflow")
        f.stack.append(v & MASK64)

    def _pop(self, f: Frame) -> int:
        if not f.stack:
            raise Halt("stack_underflow")
        return f.stack.pop()

    def _pop2(self, f: Frame) -> tuple[int, int]:
        """弹出两个值，返回 (次顶 a, 栈顶 b) —— 即栈布局 [a, b]。"""
        if len(f.stack) < 2:
            raise Halt("stack_underflow")
        b = f.stack.pop()
        a = f.stack.pop()
        return a, b

    def _jump(self, f: Frame, byte_offset: int) -> None:
        if byte_offset not in f.jumpdests:
            raise Halt("invalid_jump")
        f.pc = f._offset_map[byte_offset]

    def _read_output(self, f: Frame, off: int, length: int) -> list[int]:
        if (length > MAX_RETURN_WORDS or off < 0 or length < 0
                or off + length > f.mem_words):
            raise Halt("invalid_return_range")
        return [f.memory[i] if i < len(f.memory) else 0
                for i in range(off, off + length)]

    def _read_storage(self, frames, committed, address, slot) -> int:
        key = (address, slot)
        for fr in reversed(frames):
            if key in fr.writes:
                return fr.writes[key]
        return committed.get(key, 0)
