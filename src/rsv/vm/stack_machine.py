"""受限栈机（模块二：链状态内核的脚本执行器）。

安全/资源约束
- 每个元素入栈检查 max_element_size、max_stack_items；
- 每条指令执行前扣减 1 步预算，耗尽即 resource.op_budget_exhausted；
- 嵌套 IF 在解析器已做配对，这里执行 active 分支并限制深度；
- 任何取数操作栈不足一律 compute.stack_underflow；
- OP_CHECKSIG 的签名消息由链层构造（绑定交易摘要+域标签）；
- OP_CHECKMULTISIG 顺序无关匹配，且同一公钥最多匹配一次（重复签名不计数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Limits
from ..encoding import crypto
from ..encoding.opcodes import (
    OP_0,
    OP_1,
    OP_16,
    OP_1NEGATE,
    OP_BOOLAND,
    OP_BOOLOR,
    OP_CHECKSIG,
    OP_CHECKSIGVERIFY,
    OP_CHECKMULTISIG,
    OP_CHECKMULTISIGVERIFY,
    OP_DEPTH,
    OP_DROP,
    OP_DUP,
    OP_ELSE,
    OP_ENDIF,
    OP_EQUAL,
    OP_EQUALVERIFY,
    OP_FROMALTSTACK,
    OP_HASH160,
    OP_HASH256,
    OP_IF,
    OP_NOTIF,
    OP_NOP,
    OP_NOT,
    OP_RIPEMD160,
    OP_RETURN,
    OP_SHA256,
    OP_SIZE,
    OP_SWAP,
    OP_TOALTSTACK,
    OP_VERIFY,
)
from ..encoding.script_codec import Instruction, parse_script
from ..errors import (
    CHECKSIG_FAILED,
    DIRTY_STACK,
    ELEMENT_TOO_LARGE,
    EQUALITY_FAILED,
    MULTISIG_POLICY,
    RETURN_HIT,
    SCRIPT_FINAL_EMPTY,
    SCRIPT_FINAL_FALSE,
    STACK_OVERFLOW,
    STACK_UNDERFLOW,
    THRESHOLD_NOT_MET,
    VERIFY_FAILED,
    VerificationFailure,
)


@dataclass
class RunContext:
    """栈机与链状态之间的窄接口：消息由链层构造并注入。"""

    message32: bytes  # 绑定交易摘要与域标签的 32B 摘要
    network: str
    domain: str


@dataclass
class ExecResult:
    ok: bool
    final_stack: list[bytes]
    steps_used: int
    trace: list[str] = field(default_factory=list)


class StackMachine:
    def __init__(
        self,
        limits: Limits,
        require_clean_stack: bool = True,
        record_trace: bool = True,
    ):
        self.limits = limits
        self.require_clean_stack = require_clean_stack
        self.record_trace = record_trace

    # -- 公开入口 ---------------------------------------------------------
    def execute(
        self,
        unlock_script: bytes,
        lock_script: bytes,
        ctx: RunContext,
        trace: list[str] | None = None,
    ) -> ExecResult:
        # 先执行解锁（签名推送），再执行锁定，共享同一栈——经典 P2SH 式组合。
        # 全局指令序号 gpc 跨两段连续编号，避免两段各自从 0 起造成轨迹歧义。
        unlock_ins = parse_script(unlock_script, self.limits)
        lock_ins = parse_script(lock_script, self.limits)
        combined = [
            (gpc, ins)
            for gpc, ins in enumerate(unlock_ins + lock_ins)
        ]
        stack: list[bytes] = []
        alt: list[bytes] = []
        tr = trace if trace is not None else []
        steps = 0

        # 分支执行状态：每层 [active, ever_taken, saw_else]
        conds: list[list[bool]] = []

        def executing() -> bool:
            return all(c["on"] for c in conds)

        for gpc, ins in combined:
            steps += 1
            if steps > self.limits.max_op_steps:
                from ..errors import OP_BUDGET_EXHAUSTED

                raise VerificationFailure(
                    OP_BUDGET_EXHAUSTED,
                    f">{self.limits.max_op_steps} steps at gpc={gpc}",
                    pc=ins.pc,
                )

            op = ins.op
            active = executing()

            # 分支控制类即使在未执行分支里也要处理结构
            if op in (OP_IF, OP_NOTIF):
                depth = len(conds)
                if active:
                    cond = self._pop_bool(stack, ins)
                    taken = (not cond) if op == OP_NOTIF else cond
                else:
                    taken = False
                # 记录本层是否执行、是否已执行过任一分支、是否已到 ELSE
                conds.append({"on": taken, "any": taken, "else": False})
                self._log(tr, steps, ins, stack, f"branch depth={depth + 1} taken={taken}")
                continue
            if op == OP_ELSE:
                if not conds:  # 解析器已配对，这里属防御
                    from ..errors import UNBALANCED_IF

                    raise VerificationFailure(UNBALANCED_IF, "ELSE w/o IF", pc=ins.pc)
                c = conds[-1]
                if c["else"]:
                    from ..errors import UNBALANCED_IF

                    raise VerificationFailure(UNBALANCED_IF, "duplicate ELSE", pc=ins.pc)
                c["else"] = True
                # ELSE 分支执行当且仅当 IF 分支没执行，且外层都在执行
                outer = all(x["on"] for x in conds[:-1])
                c["on"] = outer and not c["any"]
                if c["on"]:
                    c["any"] = True
                self._log(tr, steps, ins, stack, f"else on={c['on']}")
                continue
            if op == OP_ENDIF:
                if not conds:
                    from ..errors import UNBALANCED_IF

                    raise VerificationFailure(UNBALANCED_IF, "ENDIF w/o IF", pc=ins.pc)
                conds.pop()
                self._log(tr, steps, ins, stack, "endif")
                continue

            if not active:
                self._log(tr, steps, ins, stack, "skipped (branch inactive)")
                continue

            self._dispatch(ins, stack, alt, ctx, tr, steps)

        if conds:  # 解析器理论上已排除
            from ..errors import UNBALANCED_IF

            raise VerificationFailure(UNBALANCED_IF, "unterminated IF after exec")

        if not stack:
            raise VerificationFailure(SCRIPT_FINAL_EMPTY, "empty stack after execution")
        if not cast_bool(stack[-1]):
            raise VerificationFailure(SCRIPT_FINAL_FALSE, "top element is false")
        if self.require_clean_stack and len(stack) != 1:
            raise VerificationFailure(DIRTY_STACK, f"{len(stack)} items left on stack")
        return ExecResult(ok=True, final_stack=stack, steps_used=steps, trace=tr)

    # -- 指令分派 ---------------------------------------------------------
    def _dispatch(
        self,
        ins: Instruction,
        stack: list[bytes],
        alt: list[bytes],
        ctx: RunContext,
        tr: list[str],
        step_no: int,
    ) -> None:
        op = ins.op

        # 立即数/小整数推送
        if 0x01 <= op <= 0x4B:
            self._push(stack, ins.data, ins)
        elif op == OP_0:
            self._push(stack, b"", ins)
        elif op == OP_1NEGATE:
            self._push(stack, b"\x81", ins)
        elif OP_1 <= op <= OP_16:
            self._push(stack, bytes([op - OP_1 + 1]), ins)

        elif op == OP_NOP:
            pass

        elif op == OP_VERIFY:
            v = self._pop(stack, ins)
            if not cast_bool(v):
                raise VerificationFailure(VERIFY_FAILED, "OP_VERIFY got false", pc=ins.pc)

        elif op == OP_RETURN:
            raise VerificationFailure(RETURN_HIT, "OP_RETURN executed", pc=ins.pc)

        elif op == OP_DUP:
            self._push(stack, self._peek(stack, ins), ins)
        elif op == OP_DROP:
            self._pop(stack, ins)
        elif op == OP_SWAP:
            self._need(stack, 2, ins)
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif op == OP_DEPTH:
            self._push(stack, encode_smallint(len(stack)), ins)
        elif op == OP_TOALTSTACK:
            alt.append(self._pop(stack, ins))
        elif op == OP_FROMALTSTACK:
            if not alt:
                raise VerificationFailure(STACK_UNDERFLOW, "alt stack empty", pc=ins.pc)
            self._push(stack, alt.pop(), ins)

        elif op == OP_SIZE:
            top = self._peek(stack, ins)
            self._push(stack, encode_smallint(len(top)), ins)

        elif op == OP_NOT:
            v = self._pop_bool(stack, ins)
            self._push(stack, encode_smallint(0 if v else 1), ins)
        elif op == OP_BOOLAND:
            a = self._pop_bool(stack, ins)
            b = self._pop_bool(stack, ins)
            self._push(stack, encode_smallint(1 if a and b else 0), ins)
        elif op == OP_BOOLOR:
            a = self._pop_bool(stack, ins)
            b = self._pop_bool(stack, ins)
            self._push(stack, encode_smallint(1 if a or b else 0), ins)

        elif op in (OP_EQUAL, OP_EQUALVERIFY):
            a = self._pop(stack, ins)
            b = self._pop(stack, ins)
            eq = a == b
            if op == OP_EQUAL:
                self._push(stack, encode_smallint(1 if eq else 0), ins)
            elif not eq:
                raise VerificationFailure(
                    EQUALITY_FAILED,
                    f"{a.hex()[:32]} != {b.hex()[:32]}",
                    pc=ins.pc,
                )

        elif op == OP_RIPEMD160:
            self._push(stack, crypto.ripemd160(self._pop(stack, ins)), ins)
        elif op == OP_SHA256:
            self._push(stack, crypto.sha256(self._pop(stack, ins)), ins)
        elif op == OP_HASH160:
            self._push(stack, crypto.hash160(self._pop(stack, ins)), ins)
        elif op == OP_HASH256:
            self._push(stack, crypto.hash256(self._pop(stack, ins)), ins)

        elif op in (OP_CHECKSIG, OP_CHECKSIGVERIFY):
            # 受限语义（docs/opcodes.md 明确声明，与完整链不同）：
            # 验签失败立即抛 compute.crypto.sig，而不是压 0 交给脚本继续；
            # 结构错误（坏公钥/坏 DER）则以 input.* 向上抛，优先于逻辑判定。
            pub = self._pop(stack, ins)
            sig = self._pop(stack, ins)
            crypto.verify_signature(sig, ctx.message32, pub)
            self._push(stack, encode_smallint(1), ins)
            if op == OP_CHECKSIGVERIFY:
                self._pop(stack, ins)  # 校验刚压入的真值

        elif op in (OP_CHECKMULTISIG, OP_CHECKMULTISIGVERIFY):
            ok = self._checkmultisig(ins, stack, ctx)
            if not ok:
                # 策略非法/重复已在 _checkmultisig 内归类为 MULTISIG_POLICY；
                # 走到这里说明合法签名数不足门槛。
                raise VerificationFailure(
                    THRESHOLD_NOT_MET, "valid signatures below m-of-n threshold", pc=ins.pc
                )
            self._push(stack, encode_smallint(1), ins)
            if op == OP_CHECKMULTISIGVERIFY:
                self._pop(stack, ins)

        else:  # 解析器白名单保证不可达
            from ..errors import UNKNOWN_OPCODE

            raise VerificationFailure(UNKNOWN_OPCODE, f"0x{op:02X} no handler", pc=ins.pc)

        self._log(tr, step_no, ins, stack, None)

    def _checkmultisig(self, ins: Instruction, stack: list[bytes], ctx: RunContext) -> bool:
        """栈布局（栈顶 -> 栈底）：n, pk*n, m, sig*m。

        执行前完整的栈（底 -> 顶）为：sig_1..sig_m, <m>, pk_1..pk_n, <n>，
        与标准 multisig 约定一致（本受限版本不含 dummy 元素）。

        规则（与 docs/opcodes.md 一致）：
        - m、n 必须是最小脚本整数编码，1 <= m <= n <= 上限，否则 MULTISIG_POLICY；
        - 策略公钥重复 -> MULTISIG_POLICY（同一公钥不能重复计数）；
        - *提供的签名*重复 -> MULTISIG_POLICY（重复签名无法代表第二个公钥，
          这正是“同一公钥不能重复计数”的攻击形态：用一把私钥的多份副本凑门槛）；
        - 签名结构非法 -> input.crypto.sig_encoding（结构错优先于逻辑错）；
        - 顺序无关匹配（标准 multisig 语义），每个 pub 最多用一次；
        - 合法签名但匹配数不足 m -> False（调用方转 threshold）。
        """
        limits = self.limits
        # 1) 先弹 n
        n_raw = self._pop(stack, ins)
        n = decode_smallint(n_raw)
        if not 1 <= n <= limits.max_multisig_pubkeys or encode_smallint(n) != n_raw:
            raise VerificationFailure(MULTISIG_POLICY, f"bad/non-minimal n={n}", pc=ins.pc)
        # 2) n 个公钥
        pubs = [self._pop(stack, ins) for _ in range(n)]
        # 3) 再弹 m
        m_raw = self._pop(stack, ins)
        m = decode_smallint(m_raw)
        if not 1 <= m <= n or encode_smallint(m) != m_raw:
            raise VerificationFailure(MULTISIG_POLICY, f"bad/non-minimal m={m}, n={n}", pc=ins.pc)
        # 4) m 个签名
        sigs = [self._pop(stack, ins) for _ in range(m)]

        if len(set(pubs)) != n:
            raise VerificationFailure(MULTISIG_POLICY, "duplicate public keys in policy", pc=ins.pc)
        if len(set(sigs)) != m:
            raise VerificationFailure(
                MULTISIG_POLICY,
                "duplicate signatures supplied; one pubkey may not be counted twice",
                pc=ins.pc,
            )

        # 结构先全部校验（input 类错误优先）
        for p in pubs:
            crypto.parse_public_key(p)
        for s in sigs:
            crypto.validate_signature_der(s)

        matched_pubs: set[bytes] = set()
        matched = 0
        # 贪心、顺序无关：按签名顺序找第一个尚未使用且验签通过的公钥
        for s in sigs:
            for p in pubs:
                if p in matched_pubs:
                    continue
                if crypto.cross_check_signature(s, ctx.message32, p):
                    matched_pubs.add(p)
                    matched += 1
                    break
        return matched >= m

    # -- 栈工具 -----------------------------------------------------------
    def _push(self, stack: list[bytes], v: bytes, ins: Instruction) -> None:
        if len(v) > self.limits.max_element_size:
            raise VerificationFailure(
                ELEMENT_TOO_LARGE,
                f"{len(v)}B > {self.limits.max_element_size}B",
                pc=ins.pc,
            )
        if len(stack) >= self.limits.max_stack_items:
            raise VerificationFailure(
                STACK_OVERFLOW, f">{self.limits.max_stack_items} items", pc=ins.pc
            )
        stack.append(v)

    @staticmethod
    def _need(stack: list[bytes], k: int, ins: Instruction) -> None:
        if len(stack) < k:
            raise VerificationFailure(
                STACK_UNDERFLOW, f"need {k}, have {len(stack)}", pc=ins.pc
            )

    def _pop(self, stack: list[bytes], ins: Instruction) -> bytes:
        self._need(stack, 1, ins)
        return stack.pop()

    def _peek(self, stack: list[bytes], ins: Instruction) -> bytes:
        self._need(stack, 1, ins)
        return stack[-1]

    def _pop_bool(self, stack: list[bytes], ins: Instruction) -> bool:
        return cast_bool(self._pop(stack, ins))

    def _log(self, tr: list[str], step_no: int, ins: Instruction,
             stack: list[bytes], note: str | None) -> None:
        if not self.record_trace or len(tr) >= self.limits.max_trace_items:
            return
        depth = len(stack)
        top = stack[-1].hex()[:16] if stack else "-"
        suffix = f" {note}" if note else ""
        tr.append(f"step={step_no:<3} op=0x{ins.op:02X} stack_depth={depth:<3} top={top}{suffix}")


# ---- 脚本布尔/小整数语义 ----------------------------------------------------

def cast_bool(b: bytes) -> bool:
    """经典脚本布尔：空为假；全零为假（0x80 负零也为假）；其余为真。"""
    if not b:
        return False
    for ch in b[:-1]:
        if ch != 0:
            return True
    return b[-1] not in (0x00, 0x80)


def encode_smallint(n: int) -> bytes:
    if n == 0:
        return b""
    if n == -1:
        return b"\x81"
    if 1 <= n <= 16:
        return bytes([n])
    # 非小整数走通用最小有符号编码
    out = bytearray()
    neg = n < 0
    n = -n if neg else n
    while n:
        out.append(n & 0xFF)
        n >>= 8
    if out[0] & 0x80:
        out.append(0x80 if neg else 0x00)
    elif neg:
        out[0] |= 0x80
    return bytes(out)


def decode_smallint(b: bytes) -> int:
    """最小有符号小端整数（与 Bitcoin ScriptNumber 同构，用于 multisig 的 m/n）。"""
    if not b:
        return 0
    neg = bool(b[-1] & 0x80)
    payload = bytes([b[0]]) + b[1:-1] + bytes([b[-1] & 0x7F]) if len(b) > 1 else bytes([b[0] & 0x7F])
    n = int.from_bytes(payload, "little")
    return -n if neg else n
