"""服务编排层：把引擎与存储组成原子业务操作。

对外的主要操作：
* :meth:`create_document`      建文档（写入 rev=0 基线快照）
* :meth:`submit`               幂等提交一条客户端操作
* :meth:`get_document`         读当前文本/版本/基线
* :meth:`history`              版本查询（含诊断：来源、长度、校验和）
* :meth:`snapshot_and_trim`    历史裁剪（先写快照，再抬基线）
* :meth:`catchup`              旧客户端重建基线所需的当前视图

裁剪条款（对应题目）：“历史裁剪前旧客户端必须重建基线”。
实现为：裁剪抬高 ``baseline_rev`` 并删除更旧修订；此后任何
``base_rev < baseline_rev`` 的提交都被拒绝（BASELINE_TRIMMED），
客户端必须先 :meth:`catchup` 拿到新基线文本与版本号。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import Engine
from .errors import InputInvalid, StateConflict
from .repository import Repository
from .textmodel import Comp, parse_message


@dataclass(slots=True)
class SubmitAck:
    rev: int
    head_rev: int
    base_rev: int
    client_id: str
    client_op_id: int
    text: str
    ops: list[Comp]


@dataclass(slots=True)
class DocView:
    doc_id: str
    rev: int
    baseline_rev: int
    text: str
    length_chars: int


@dataclass(slots=True)
class RevView:
    rev: int
    client_id: str
    client_op_id: int
    base_rev: int
    length_before: int
    length_after: int
    ops: list[Comp]
    checksum: str


class OTService:
    def __init__(self, repository: Repository, **engine_kwargs):
        self.repo = repository
        self.engine = Engine(repository, **engine_kwargs)

    # -------------------------------------------------- 文档

    def create_document(self, doc_id: str, initial_text: str = "") -> DocView:
        if not isinstance(doc_id, str) or not doc_id:
            raise InputInvalid("doc_id 必须是非空字符串", reason="BAD_DOC_ID")
        if not isinstance(initial_text, str):
            raise InputInvalid("initial_text 必须是字符串", reason="BAD_TEXT")
        if self.repo.exists(doc_id):
            raise StateConflict(f"文档 {doc_id!r} 已存在", reason="DOC_EXISTS",
                                details={"doc_id": doc_id})
        self.repo.create_document(doc_id, initial_text)
        return DocView(doc_id, 0, 0, initial_text, len(initial_text))

    def get_document(self, doc_id: str) -> DocView:
        text = self.repo.head_text(doc_id)
        return DocView(
            doc_id=doc_id,
            rev=self.repo.head_rev(doc_id),
            baseline_rev=self.repo.baseline_rev(doc_id),
            text=text,
            length_chars=len(text),
        )

    # -------------------------------------------------- 提交

    def submit(self, doc_id: str, base_rev: int, client_id: str,
               client_op_id: int, raw_ops: list[dict]) -> SubmitAck:
        """幂等提交。

        同一 ``(doc_id, client_id, client_op_id)`` 的重复提交：
        * 若操作体完全一致 -> 返回首次结果修订（幂等成功）；
        * 若操作体不同 -> 状态冲突 REUSED_OP_ID（防客户端序号错误）。
        """
        if not isinstance(client_id, str) or not client_id:
            raise InputInvalid("client_id 必须是非空字符串", reason="BAD_CLIENT_ID")
        if (not isinstance(client_op_id, int) or isinstance(client_op_id, bool)
                or client_op_id < 0):
            raise InputInvalid("client_op_id 必须是非负整数", reason="BAD_CLIENT_OP_ID")
        if not isinstance(base_rev, int) or isinstance(base_rev, bool):
            raise InputInvalid("base_rev 必须是整数", reason="BAD_BASE_REV")
        if not isinstance(raw_ops, list):
            raise InputInvalid("ops 必须是数组", reason="BAD_OPS")

        ops = parse_message(raw_ops)
        # 给尚未带来源的插入统一盖上提交者身份（删除无身份）
        ops = [c if (not c.is_ins or c.client_id) else c.stamp(client_id, client_op_id)
               for c in ops]
        # 同一条消息里的插入身份必须属于提交者
        for c in ops:
            if c.is_ins and (c.client_id != client_id or c.client_op_id != client_op_id):
                raise InputInvalid(
                    "插入的来源身份必须等于提交者",
                    reason="ORIGIN_MISMATCH",
                    details={"got": c.client_id, "expected": client_id},
                )

        signature = _raw_signature(raw_ops)

        def work():
            existing = self.repo.lookup_submission(doc_id, client_id, client_op_id)
            if existing is not None:
                first_sig = self.repo.lookup_raw_signature(doc_id, client_id, client_op_id)
                if first_sig != signature:
                    # 同一序号被用于两个不同操作体：客户端序号错误（状态冲突）
                    raise StateConflict(
                        "client_op_id 已被用于不同操作",
                        reason="REUSED_OP_ID",
                        details={"client_id": client_id, "client_op_id": client_op_id},
                    )
                return existing

            result = self.engine.commit(doc_id, base_rev, client_id, client_op_id, ops)
            self.repo.insert_revision(result.revision)
            self.repo.set_head(doc_id, result.revision.rev, result.head_text,
                               result.iddoc_json)
            self.repo.remember_submission(doc_id, client_id, client_op_id,
                                          result.revision.rev, signature)
            return result.revision.rev

        rev_no = self.repo.run_transaction(work)
        text = self.repo.head_text(doc_id)
        stored = self.repo.get_revision(doc_id, rev_no)
        return SubmitAck(
            rev=rev_no,
            head_rev=self.repo.head_rev(doc_id),
            base_rev=base_rev,
            client_id=client_id,
            client_op_id=client_op_id,
            text=text,
            ops=stored.ops,
        )

    # -------------------------------------------------- 查询

    def history(self, doc_id: str, since: int = 0, limit: int = 1000) -> list[RevView]:
        head = self.repo.head_rev(doc_id)
        baseline = self.repo.baseline_rev(doc_id)
        if since < 0:
            raise InputInvalid("since 不能为负", reason="BAD_SINCE")
        if since < baseline:
            raise StateConflict(
                f"since={since} 已低于裁剪基线 {baseline}，请先 catchup 重建",
                reason="BASELINE_TRIMMED",
                details={"since": since, "baseline": baseline},
            )
        if since > head:
            raise StateConflict("since 超过 HEAD", reason="SINCE_AHEAD",
                                details={"since": since, "head": head})
        out: list[RevView] = []
        for r in range(since + 1, min(head, since + limit) + 1):
            s = self.repo.get_revision(doc_id, r)
            out.append(RevView(
                rev=s.rev, client_id=s.client_id, client_op_id=s.client_op_id,
                base_rev=s.base_rev, length_before=s.length_before,
                length_after=s.length_after, ops=s.ops, checksum=s.checksum,
            ))
        return out

    def revision_detail(self, doc_id: str, rev: int) -> RevView:
        s = self.repo.get_revision(doc_id, rev)
        return RevView(
            rev=s.rev, client_id=s.client_id, client_op_id=s.client_op_id,
            base_rev=s.base_rev, length_before=s.length_before,
            length_after=s.length_after, ops=s.ops, checksum=s.checksum,
        )

    # -------------------------------------------------- 裁剪/追赶

    def snapshot_and_trim(self, doc_id: str, keep_from_rev: int) -> dict:
        """写当前 HEAD 快照并把基线抬高到 keep_from_rev。

        快照文本按引擎重建（保证与修订链一致）；随后删除
        ``rev < keep_from_rev`` 的修订。裁剪是不可逆的：更旧基线的
        客户端必须 :meth:`catchup`。
        """
        head = self.repo.head_rev(doc_id)
        if keep_from_rev < 0 or keep_from_rev > head:
            raise InputInvalid(
                "keep_from_rev 必须在 [0, head] 内",
                reason="BAD_TRIM_REV",
                details={"keep_from_rev": keep_from_rev, "head": head},
            )

        def work():
            text = self.engine.text_at(doc_id, keep_from_rev)
            self.repo.put_snapshot(doc_id, keep_from_rev, text)
            self.repo.trim(doc_id, keep_from_rev)
            return text

        text = self.repo.run_transaction(work)
        return {
            "doc_id": doc_id,
            "baseline_rev": keep_from_rev,
            "head_rev": self.repo.head_rev(doc_id),
            "text": text,
        }

    def catchup(self, doc_id: str) -> DocView:
        """旧客户端重建基线：返回当前基线之后的完整视图。"""
        return self.get_document(doc_id)


def _raw_signature(raw_ops: list[dict]) -> str:
    import json

    return json.dumps(raw_ops, ensure_ascii=False, sort_keys=True)
