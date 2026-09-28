"""离线回放：从合成夹具重放链状态，并对期望结果做强断言。

夹具格式（JSON，全部本地合成，无真实业务数据）：
{
  "description": "...",
  "blocks": [
     {
       "label": "...",
       "transactions": [
          {"call": "mint", "args_hex_for_encoder": ... }   # 见下
       ]
     }
  ],
  "expectations": { "balances": {"0x..": "100"}, "reverted_categories": [...] }
}

交易用 {signature, args} 表达；回放器用我们自己的 ABI 内核重新编码
calldata（测试的是被测核心），但夹具里的 *期望值*（余额、状态根、
回滚类别）由人工/独立计算给出，不由被测核心生成。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..abi import encode_call
from ..kernel import ChainKernel
from ..runlog import get_logger
from ..storage import Storage


def _hex_addr(b: str) -> bytes:
    s = b[2:] if b.startswith("0x") else b
    return bytes.fromhex(s)


def build_calldata(tx: dict) -> bytes:
    sig = tx["signature"]
    args = _materialize_args(tx["args"])
    return encode_call(sig, args)


def _materialize_args(args: list):
    out = []
    for a in args:
        if isinstance(a, dict) and "bytes" in a:
            out.append(bytes.fromhex(a["bytes"][2:] if a["bytes"].startswith("0x") else a["bytes"]))
        else:
            out.append(a)
    return tuple(out)


class ReplayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def replay(fixture_path: str, db_path: str = ":memory:", *, log_dir: str | None = None) -> dict:
    """执行回放；任何期望不符都抛 ReplayError（由 CLI 转非零退出码）。"""
    logger = get_logger("replay", log_dir)
    storage = None
    try:
        data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        logger.step("fixture_loaded", identity={"path": str(fixture_path)},
                    detail=data.get("description", ""))

        kernel = ChainKernel()
        storage = Storage(db_path)

        mismatch = None
        for b_idx, blk in enumerate(data["blocks"]):
            calldatas: list[bytes] = []
            steps = []
            for t_idx, tx in enumerate(blk["transactions"]):
                cd = build_calldata(tx)
                calldatas.append(cd)
                steps.append({
                    "tx": t_idx, "signature": tx["signature"],
                    "calldata_0x": "0x" + cd.hex(),
                })
            block = kernel.apply_block(calldatas)
            storage.save_block(block, calldatas, kernel.accounts)
            for s, rcpt in zip(steps, block.receipts):
                s["status"] = rcpt.status
                s["error_category"] = rcpt.error_category
            logger.step(
                "block_applied",
                identity={"block": b_idx, "label": blk.get("label", "")},
                steps=steps,
                state_root="0x" + block.state_root.hex(),
                block_hash="0x" + block.block_hash.hex(),
            )

        # ---- 强断言：余额 ----
        exp_bal = data.get("expectations", {}).get("balances", {})
        for addr_hex, expected in exp_bal.items():
            actual = kernel.balance_of(_hex_addr(addr_hex))
            if actual != int(expected):
                mismatch = ReplayError(
                    "balance_mismatch",
                    f"账户 {addr_hex} 期望余额 {expected}，实际 {actual}",
                )
                break
            logger.step("balance_ok", identity={"account": addr_hex},
                        expected=int(expected), actual=actual)

        # ---- 强断言：回滚类别序列（按区块/交易顺序）----
        exp_rev = data.get("expectations", {}).get("reverted_categories_ordered", [])
        rows = storage.conn.execute(
            "SELECT error_category FROM transactions WHERE status='reverted' "
            "ORDER BY block_number,tx_index"
        ).fetchall()
        actual_rev = [r["error_category"] for r in rows]
        if actual_rev != exp_rev:
            mismatch = ReplayError(
                "revert_category_mismatch",
                f"回滚类别期望 {exp_rev}，实际 {actual_rev}",
            )
        else:
            logger.step("reverts_ok", expected=exp_rev, actual=actual_rev)

        # ---- 强断言：状态根（若夹具钉死）----
        exp_root = data.get("expectations", {}).get("final_state_root")
        final_root = "0x" + kernel.compute_state_root().hex()
        if exp_root is not None and exp_root != final_root:
            mismatch = ReplayError(
                "state_root_mismatch",
                f"状态根期望 {exp_root}，实际 {final_root}",
            )
        elif exp_root is not None:
            logger.step("state_root_ok", final_state_root=final_root)

        summary = storage.stats()
        if mismatch is not None:
            logger.failure(
                "replay_failed", category=mismatch.code,
                message=str(mismatch), summary=summary,
            )
            raise mismatch

        logger.close(verdict="completed", summary=summary)
        return {"ok": True, "summary": summary, "final_state_root": final_root}
    finally:
        if storage is not None:
            storage.close()
