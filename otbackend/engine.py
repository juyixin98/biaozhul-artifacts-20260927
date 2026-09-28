"""版本引擎：在已存储历史之上推进文档版本。

职责
----
* 重建任意版本的文档文本（从最近快照重放）；
* 校验客户端操作与所声明基线一致（坐标、删除区间、组件上限）；
* 把旧版本操作连续变换到当前 HEAD，检查长度/大小不变量；
* 产生可落库的 :class:`StoredRevision`。

引擎不接触数据库具体实现，只依赖 :class:`Repository` 协议
（见 repository.py），便于用内存夹具做纯算法测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ComputationFailed, ResourceExhausted, StateConflict
from .textmodel import (
    Comp,
    apply_stream,
    byte_len,
    stream_len_delta,
    validate_against_base,
)


@dataclass(frozen=True, slots=True)
class StoredRevision:
    doc_id: str
    rev: int
    client_id: str
    client_op_id: int
    base_rev: int
    ops: list[Comp]
    length_before: int
    length_after: int
    checksum: str
    ops_json: str = ""

    def verify(self, repository) -> None:
        """核对校验和；存储被篡改/损坏时报计算失败。"""
        stored_json = repository.ops_json_for(self)
        expect = rev_checksum(self.rev, self.doc_id, stored_json, self.length_after)
        if expect != self.checksum:
            raise ComputationFailed(
                f"修订 r{self.rev} 校验和不匹配（历史可能损坏）",
                reason="CORRUPT_HISTORY",
                details={"rev": self.rev, "expected": expect, "got": self.checksum},
            )


@dataclass(slots=True)
class CommitResult:
    revision: StoredRevision
    head_text: str
    transformed_ops: list[Comp] = field(default_factory=list)
    iddoc_json: str = ""


def rev_checksum(rev: int, doc_id: str, ops_json: str, length_after: int) -> str:
    """修订校验和：防存储损坏/手工改库。非加密强度，足够诊断。"""
    import hashlib

    h = hashlib.sha256()
    h.update(f"{doc_id}|{rev}|{length_after}|".encode())
    h.update(ops_json.encode("utf-8"))
    return h.hexdigest()[:16]


class Engine:
    def __init__(
        self,
        repository,
        *,
        max_components: int = 10_000,
        max_doc_chars: int = 10_000_000,
        max_doc_bytes: int = 50_000_000,
    ):
        self.repo = repository
        self.max_components = max_components
        self.max_doc_chars = max_doc_chars
        self.max_doc_bytes = max_doc_bytes

    # -------------------------------------------------- 版本重建

    def iddoc_at(self, doc_id: str, rev: int):
        """重建 rev 版本的字符身份文档（rev=0 为初始）。

        优先用 HEAD 缓存的 IdDoc 回退/直接返回；否则从快照文本重建。
        历史修订的 ops 携带 char_ids，逐版应用即可恢复身份结构。
        """
        from .iddoc import IdDoc
        from .repository import _iddoc_from_json

        head = self.repo.head_rev(doc_id)
        if rev < 0 or rev > head:
            raise StateConflict(
                f"版本 {rev} 不存在（当前 HEAD={head}）",
                reason="UNKNOWN_REVISION", details={"rev": rev, "head": head},
            )
        if rev == head:
            raw = self.repo.head_iddoc_json(doc_id)
            if raw:
                return _iddoc_from_json(raw)
        snap = self.repo.latest_snapshot_at_or_before(doc_id, rev)
        doc = IdDoc.initial(snap.text)
        for r in range(snap.rev + 1, rev + 1):
            stored = self.repo.get_revision(doc_id, r)
            stored.verify(self.repo)
            doc = doc.commit(stored.ops)
        return doc

    def text_at(self, doc_id: str, rev: int) -> str:
        """重建文档在 rev 版本的文本（rev=0 为初始文本）。"""
        return self.iddoc_at(doc_id, rev).render()

    # -------------------------------------------------- 提交

    def commit(
        self,
        doc_id: str,
        base_rev: int,
        client_id: str,
        client_op_id: int,
        ops: list[Comp],
    ) -> CommitResult:
        """把一条已通过格式校验的规范流提交为新版本。"""
        head = self.repo.head_rev(doc_id)
        if base_rev < 0:
            raise StateConflict("base_rev 不能为负", reason="BAD_BASE_REV")
        if base_rev > head:
            raise StateConflict(
                f"base_rev={base_rev} 领先于服务端 HEAD={head}",
                reason="BASE_AHEAD_OF_HEAD",
                details={"base_rev": base_rev, "head": head},
            )
        baseline = self.repo.baseline_rev(doc_id)
        if base_rev < baseline:
            # 历史裁剪条款：旧基线必须重建后再提交
            raise StateConflict(
                f"base_rev={base_rev} 已低于裁剪基线 {baseline}，必须先重建基线",
                reason="BASELINE_TRIMMED",
                details={"base_rev": base_rev, "baseline": baseline, "head": head},
            )

        if len(ops) > self.max_components:
            raise ResourceExhausted(
                f"单条消息组件数 {len(ops)} 超过上限 {self.max_components}",
                reason="TOO_MANY_COMPONENTS",
                details={"count": len(ops), "limit": self.max_components},
            )
        # 插入文本总量也计入组件配额防护
        total_ins = sum(len(c.text) for c in ops if c.is_ins)
        if total_ins > self.max_doc_chars:
            raise ResourceExhausted(
                f"单次插入 {total_ins} 码点超过文档上限",
                reason="INSERT_TOO_LARGE",
                details={"chars": total_ins, "limit": self.max_doc_chars},
            )

        # 1) 针对声明基线做坐标/区间校验
        base_doc = self.iddoc_at(doc_id, base_rev)
        base_text = base_doc.render()
        validate_against_base(base_text, ops)

        # 2) 顺序无关重放：在基线上解析、向 HEAD 身份文档集成。
        #    插入按全局 char_id 在稳定槽位上排序，删除按 id 命中，
        #    因此结果与 (base_rev, head] 之间修订的接收顺序无关。
        head_doc = self.iddoc_at(doc_id, head)
        new_doc = head_doc.rebase_change(base_doc, ops)

        # 3) 导出可落库的坐标组件（HEAD 文本 -> 新文本）
        transformed = new_doc.diff_components(head_doc)
        new_text = new_doc.render()

        # 交叉校验：用纯位置应用器应用导出的组件，必须得到同一文本
        try:
            alt = apply_stream(head_doc.render(), transformed)
        except StateConflict:
            raise ComputationFailed(
                "变换结果应用失败：算法不变量被破坏",
                reason="TRANSFORM_APPLY_FAILED",
            )
        if alt != new_text:
            raise ComputationFailed(
                "id 模型与位置应用结果不一致",
                reason="ID_POSITION_MISMATCH",
                details={"id_result": new_text, "position_result": alt},
            )

        # 4) 资源限额
        if len(new_text) > self.max_doc_chars:
            raise ResourceExhausted(
                f"文档将达到 {len(new_text)} 码点，超过上限 {self.max_doc_chars}",
                reason="DOC_TOO_LARGE_CHARS",
                details={"chars": len(new_text), "limit": self.max_doc_chars},
            )
        if byte_len(new_text) > self.max_doc_bytes:
            raise ResourceExhausted(
                "文档 UTF-8 字节数超过上限",
                reason="DOC_TOO_LARGE_BYTES",
                details={"bytes": byte_len(new_text), "limit": self.max_doc_bytes},
            )

        # 5) 长度不变量核对
        head_text = head_doc.render()
        expected_len = len(head_text) + stream_len_delta(transformed)
        if expected_len != len(new_text):
            raise ComputationFailed(
                "长度不变量不一致", reason="LENGTH_INVARIANT",
                details={"expected": expected_len, "actual": len(new_text)},
            )

        new_rev = head + 1
        from .repository import _iddoc_to_json
        from .textmodel import comps_to_json

        ops_json = comps_to_json(transformed)
        iddoc_json = _iddoc_to_json(new_doc)
        checksum = rev_checksum(new_rev, doc_id, ops_json, len(new_text))
        revision = StoredRevision(
            doc_id=doc_id,
            rev=new_rev,
            client_id=client_id,
            client_op_id=client_op_id,
            base_rev=base_rev,
            ops=transformed,
            length_before=len(head_text),
            length_after=len(new_text),
            checksum=checksum,
            ops_json=ops_json,
        )
        return CommitResult(revision=revision, head_text=new_text,
                            transformed_ops=transformed, iddoc_json=iddoc_json)
