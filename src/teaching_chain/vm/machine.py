"""确定性栈式虚拟机执行引擎。

设计要点：

* **先扣费、后副作用**：每条指令先检查剩余 gas，扣除指令费与内存扩张费，
  然后才执行运算 / 内存 / 存储 / 嵌套调用；
* **状态与费用回滚范围分离**：帧（一次顶层执行或一次 CALL）异常停机时，
  该帧内全部存储写入被丢弃（嵌套子帧异常时子帧写入也丢弃），但帧被
  分配的 gas 全部消耗——成功时只消耗实际使用量；
* **无宿主依赖**：没有时间、随机、网络、文件系统；给定同样的输入字节码、
  初始存储与 gas，任何进程产生完全相同的结果；
* 所有整数为 64 位有符号整数，``ADD/SUB/MUL`` 越界报 ``INTEGER_OVERFLOW``，
  ``DIV/MOD`` 除零报 ``DIV_BY_ZERO``；``ADDMOD/MULMOD`` 全精度中间值取模，
  仅在结果超界时报溢出（除数必须为正）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from . import gas
from .errors import Failure
from .opcodes import OP_FEE, OP_STACK_EFFECT, Op, read_uleb128, validate_bytecode

INT64_MIN = gas.INT64_MIN
INT64_MAX = gas.INT64_MAX

MAX_CALL_DEPTH = 8


class VMError(Exception):
    """执行期错误。``category`` 是写入收据的稳定类别字符串。"""

    def __init__(self, category: Failure, pc: int = -1, detail: str = ""):
        self.category = str(category)
        self.pc = pc
        super().__init__(f"{category} at pc={pc}" + (f": {detail}" if detail else ""))


@dataclass
class FrameResult:
    ok: bool
    gas_remaining: int            # 成功时本帧剩余 gas；异常时为 0
    stack: list[int] = field(default_factory=list)
    memory: bytes = b""
    storage: dict[int, int] = field(default_factory=dict)
    error_category: str | None = None
    error_pc: int = -1


@dataclass
class ExecutionResult:
    ok: bool
    gas_used: int                 # 含 intrinsic 之外的全部执行消耗（顶层结果）
    stack: list[int]
    memory_hex: str
    storage: dict[int, int]       # 成功时为最终存储；失败时为**初始**存储（回滚后）
    error_category: str | None = None
    error_pc: int = -1
    trace: list[str] = field(default_factory=list)  # 每步一行的结构化执行轨迹
    return_value: int = 0         # 栈顶（成功时），便于断言


def _to_int64(value: int) -> int:
    """截断为 64 位有符号整数（用于取模后的规整）。"""
    value &= gas.UINT64_MOD - 1
    if value >= 1 << 63:
        value -= gas.UINT64_MOD
    return value


def _mem_words_for_end(end: int) -> int:
    if end <= 0:
        return 0
    return (end + gas.MEMORY_WORD_SIZE - 1) // gas.MEMORY_WORD_SIZE


def _expand_memory(memory: bytes, end: int) -> bytes:
    if end > len(memory):
        words = _mem_words_for_end(end)
        return memory + b"\x00" * (words * gas.MEMORY_WORD_SIZE - len(memory))
    return memory


class _Frame(NamedTuple):
    code: bytes
    gas: int
    storage: dict[int, int]
    depth: int


def execute(
    code: bytes,
    gas_limit: int,
    storage: dict[int, int] | None = None,
    trace: bool = False,
) -> ExecutionResult:
    """执行顶层字节码。

    ``storage`` 不会被原地修改：成功时结果里返回新字典，失败时返回
    与输入相等的字典（回滚）。
    """
    initial_storage = dict(storage or {})
    # 字节码非法属于静态错误：执行尚未开始，不消耗帧 gas（gas_used=0）。
    try:
        validate_bytecode(code)
    except Exception as exc:
        return ExecutionResult(
            ok=False,
            gas_used=0,
            stack=[],
            memory_hex="",
            storage=dict(initial_storage),
            error_category=Failure.INVALID_BYTECODE,
            error_pc=-1,
            trace=[f"拒绝执行: {exc}"] if trace else [],
        )
    if gas_limit < 0:
        raise ValueError("gas_limit 不能为负")

    frame_storage = dict(initial_storage)
    result, steps = _run_frame(_Frame(code, gas_limit, frame_storage, 0), trace)

    if result.ok:
        final_storage = result.storage
        gas_used = gas_limit - result.gas_remaining
        return ExecutionResult(
            ok=True,
            gas_used=gas_used,
            stack=result.stack,
            memory_hex=result.memory.hex(),
            storage=final_storage,
            trace=steps,
            return_value=result.stack[-1] if result.stack else 0,
        )
    # 回滚：存储恢复为初始值；费用保留：整帧 gas 全耗
    return ExecutionResult(
        ok=False,
        gas_used=gas_limit,
        stack=[],
        memory_hex="",
        storage=dict(initial_storage),
        error_category=result.error_category,
        error_pc=result.error_pc,
        trace=steps,
    )


def _run_frame(frame: _Frame, trace: bool) -> tuple[FrameResult, list[str]]:
    """在帧内执行；返回帧结果（gas 字段表示**剩余** gas）与轨迹。"""
    code = frame.code
    stack: list[int] = []
    memory = b""
    storage = frame.storage          # 仅本帧可见的存储映射
    remaining = frame.gas
    mem_words = 0
    steps: list[str] = []

    def fail(err: VMError) -> FrameResult:
        if trace:
            steps.append(f"!! {err.category} pc={err.pc}")
        return FrameResult(
            ok=False,
            gas_remaining=0,          # 异常停机：帧 gas 全耗，不退还
            stack=[],
            memory=b"",
            storage={},
            error_category=err.category,
            error_pc=err.pc,
        )

    pc = 0
    while pc < len(code):
        op = code[pc]
        op_pc = pc
        pc += 1

        # 1) 解析立即数（静态校验已保证完整）
        immediate: int | bytes | None = None
        call_code: bytes | None = None
        if op == Op.PUSH1:
            immediate = code[pc]
            pc += 1
        elif op == Op.PUSH8:
            raw = code[pc : pc + 8]
            pc += 8
            immediate = int.from_bytes(raw, "big", signed=True)
        elif op == Op.CALL:
            length, data_pos = read_uleb128(code, pc)
            call_code = code[data_pos : data_pos + length]
            pc = data_pos + length

        # 2) 先检查固定费用是否充足，再扣费
        fee = OP_FEE[op]
        if remaining < fee:
            return fail(VMError(Failure.OUT_OF_GAS, op_pc, f"需要 {fee}，剩余 {remaining}")), steps
        remaining -= fee

        # 3) 栈形状检查（扣费之后、副作用之前）
        pops, pushes = OP_STACK_EFFECT[op]
        if len(stack) < pops:
            return fail(VMError(Failure.STACK_UNDERFLOW, op_pc, f"需要 {pops}，实际 {len(stack)}")), steps
        if len(stack) - pops + pushes > gas.STACK_LIMIT:
            return fail(VMError(Failure.STACK_OVERFLOW, op_pc)), steps

        # 4) 执行（内存费用在确定访问末端后、读写之前扣除）
        try:
            if op == Op.STOP:
                if trace:
                    steps.append(f"pc={op_pc} STOP")
                break

            if op in (Op.ADD, Op.SUB, Op.MUL, Op.DIV, Op.MOD,
                      Op.ADDMOD, Op.MULMOD, Op.LT, Op.GT, Op.EQ, Op.ISZERO):
                result = _arith(op, stack, op_pc)
                if trace:
                    steps.append(f"pc={op_pc} {Op(op).name} -> {result} (gas剩{remaining})")
                stack.append(result)
                continue

            if op in (Op.PUSH1, Op.PUSH8):
                if trace:
                    steps.append(f"pc={op_pc} {Op(op).name} {immediate} (gas剩{remaining})")
                stack.append(immediate)  # type: ignore[arg-type]
                continue

            if op == Op.POP:
                popped = stack.pop()
                if trace:
                    steps.append(f"pc={op_pc} POP {popped}")
                continue

            if op == Op.DUP1:
                stack.append(stack[-1])
                if trace:
                    steps.append(f"pc={op_pc} DUP1 -> {stack[-1]}")
                continue

            if op == Op.SWAP1:
                stack[-1], stack[-2] = stack[-2], stack[-1]
                if trace:
                    steps.append(f"pc={op_pc} SWAP1 -> {stack[-2]},{stack[-1]}")
                continue

            if op in (Op.MLOAD, Op.MSTORE):
                offset = stack[-1]
                if offset < 0 or offset > gas.MEMORY_MAX_END - gas.MEMORY_WORD_SIZE:
                    raise VMError(Failure.INVALID_MEMORY, op_pc, f"offset={offset}")
                end = offset + gas.MEMORY_WORD_SIZE
                new_words = _mem_words_for_end(end)
                expansion = gas.memory_expansion_cost(mem_words, new_words)
                if remaining < expansion:
                    raise VMError(Failure.OUT_OF_GAS, op_pc,
                                  f"内存扩张需 {expansion}，剩余 {remaining}")
                remaining -= expansion
                mem_words = new_words
                memory = _expand_memory(memory, end)
                if op == Op.MLOAD:
                    offset = stack.pop()
                    raw = memory[offset : offset + gas.MEMORY_WORD_SIZE]
                    value = int.from_bytes(raw, "big", signed=True)
                    stack.append(value)
                    if trace:
                        steps.append(f"pc={op_pc} MLOAD[{offset}] -> {value}")
                else:
                    offset, value = stack.pop(), stack.pop()
                    memory = (
                        memory[:offset]
                        + _to_signed32_bytes(value)
                        + memory[offset + gas.MEMORY_WORD_SIZE :]
                    )
                    if trace:
                        steps.append(f"pc={op_pc} MSTORE[{offset}] = {value}")
                continue

            if op == Op.SLOAD:
                key = stack.pop()
                _check_storage_key(key, op_pc)
                value = storage.get(key, 0)
                stack.append(value)
                if trace:
                    steps.append(f"pc={op_pc} SLOAD[{key}] -> {value}")
                continue

            if op == Op.SSTORE:
                key, value = stack.pop(), stack.pop()
                _check_storage_key(key, op_pc)
                if not (gas.INT64_MIN <= value <= gas.INT64_MAX):
                    raise VMError(Failure.INTEGER_OVERFLOW, op_pc, f"存储值越界: {value}")
                storage[key] = value
                if trace:
                    steps.append(f"pc={op_pc} SSTORE[{key}] = {value}")
                continue

            if op == Op.CALL:
                if frame.depth + 1 > MAX_CALL_DEPTH:
                    raise VMError(Failure.CALL_DEPTH_EXCEEDED, op_pc,
                                  f"深度上限 {MAX_CALL_DEPTH}")
                # CALL 不分走父帧 gas：子帧获得父帧**剩余全部** gas
                # （基础费已扣除）。成功退还未用部分；失败全部消耗，
                # 子帧存储改动随其独立映射一起丢弃。
                child = _Frame(call_code, remaining, {}, frame.depth + 1)
                child_result, child_steps = _run_frame(child, trace)
                if trace:
                    steps.append(f"pc={op_pc} CALL depth={frame.depth + 1} "
                                 f"ok={child_result.ok}")
                    steps.extend("  " + s for s in child_steps)
                if child_result.ok:
                    remaining = child_result.gas_remaining
                    if trace:
                        steps.append(f"pc={op_pc} CALL 成功，退还后剩余 {remaining}")
                else:
                    remaining = 0
                    raise VMError(Failure(child_result.error_category), op_pc,  # type: ignore[arg-type]
                                  f"嵌套调用失败: {child_result.error_category}@"
                                  f"pc={child_result.error_pc}")
                continue

            if op == Op.REVERT:
                raise VMError(Failure.REVERTED, op_pc)

            raise VMError(Failure.INVALID_BYTECODE, op_pc, f"未实现的操作码 0x{op:02x}")

        except VMError as err:
            if err.pc < 0:
                err.pc = op_pc
            return fail(err), steps

    # 自然停机等价 STOP
    if trace:
        steps.append(f"pc={pc} 停机（自然结束）")
    return FrameResult(
        ok=True,
        gas_remaining=remaining,
        stack=stack,
        memory=memory,
        storage=storage,
    ), steps


def _to_signed32_bytes(value: int) -> bytes:
    if not (gas.INT64_MIN <= value <= gas.INT64_MAX):
        # 进入内存前值必然是 64 位有符号数（来自栈）
        raise VMError(Failure.INTEGER_OVERFLOW, -1, f"待写入内存的值越界: {value}")
    return value.to_bytes(gas.MEMORY_WORD_SIZE, "big", signed=True)


def _check_storage_key(key: int, pc: int) -> None:
    if key < 0:
        raise VMError(Failure.INVALID_MEMORY, pc, f"存储键不能为负: {key}")
    if key > gas.INT64_MAX:
        raise VMError(Failure.INTEGER_OVERFLOW, pc, f"存储键越界: {key}")


def _arith(op: int, stack: list[int], pc: int) -> int:
    if op == Op.ADD:
        b, a = stack.pop(), stack.pop()
        _check_binop_range(a, b, pc)
        r = a + b
        return _guard64(r, pc)
    if op == Op.SUB:
        b, a = stack.pop(), stack.pop()
        _check_binop_range(a, b, pc)
        r = a - b
        return _guard64(r, pc)
    if op == Op.MUL:
        b, a = stack.pop(), stack.pop()
        r = a * b
        return _guard64(r, pc)
    if op == Op.DIV:
        b, a = stack.pop(), stack.pop()
        if b == 0:
            raise VMError(Failure.DIV_BY_ZERO, pc, "DIV 除数为零")
        # 带符号整除，向零取整
        q = abs(a) // abs(b)
        return _guard64(-q if (a < 0) ^ (b < 0) else q, pc)
    if op == Op.MOD:
        b, a = stack.pop(), stack.pop()
        if b == 0:
            raise VMError(Failure.DIV_BY_ZERO, pc, "MOD 除数为零")
        r = abs(a) % abs(b)
        return -r if a < 0 else r
    if op == Op.ADDMOD:
        n, b, a = stack.pop(), stack.pop(), stack.pop()
        if n <= 0:
            raise VMError(Failure.DIV_BY_ZERO, pc, f"ADDMOD 模数非正: {n}")
        return _guard64((a + b) % n, pc)
    if op == Op.MULMOD:
        n, b, a = stack.pop(), stack.pop(), stack.pop()
        if n <= 0:
            raise VMError(Failure.DIV_BY_ZERO, pc, f"MULMOD 模数非正: {n}")
        return _guard64((a * b) % n, pc)
    if op == Op.LT:
        b, a = stack.pop(), stack.pop()
        return int(a < b)
    if op == Op.GT:
        b, a = stack.pop(), stack.pop()
        return int(a > b)
    if op == Op.EQ:
        b, a = stack.pop(), stack.pop()
        return int(a == b)
    if op == Op.ISZERO:
        a = stack.pop()
        return int(a == 0)
    raise VMError(Failure.INVALID_BYTECODE, pc, f"非算术操作码 0x{op:02x}")


def _check_binop_range(a: int, b: int, pc: int) -> None:
    if not (gas.INT64_MIN <= a <= gas.INT64_MAX and gas.INT64_MIN <= b <= gas.INT64_MAX):
        raise VMError(Failure.INTEGER_OVERFLOW, pc, "操作数越界")


def _guard64(value: int, pc: int) -> int:
    if not (gas.INT64_MIN <= value <= gas.INT64_MAX):
        raise VMError(Failure.INTEGER_OVERFLOW, pc, f"结果 {value} 越出 64 位有符号范围")
    return value
