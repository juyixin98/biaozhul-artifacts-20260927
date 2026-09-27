"""诊断层：把存储行还原为可审计结构，并强制“索引版本绑定原文摘要”。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grapheme.grapheme_property_group import get_group as gcb_group

from .errors import IndexCorruptionError, UnicodeVersionMismatchError
from .indexing import TextIndex, build_index
from .storage import VersionStore, decode_array
from .textnorm import sha256_hex


@dataclass(frozen=True)
class LoadedVersion:
    doc_id: str
    version: int
    index: TextIndex
    content_sha256: str
    gcb_table_version: str
    unidata_version: str
    build_mode: str
    edit_info: dict[str, Any] | None


def load_version(
    store: VersionStore,
    doc_id: str,
    version: int,
    *,
    raw_content: bytes,
    expected_gcb: str,
    expected_unidata: str,
) -> LoadedVersion:
    """从存储还原索引，并执行三重完整性校验：

    1. 原文 SHA-256 与版本绑定摘要一致；
    2. Unicode 数据版本与当前运行环境一致（同原文不同边界不可接受）；
    3. 存储的索引数组与对原文完整重算的结果逐数组相等。
    """
    row = store.get_version_row(doc_id, version)
    if row is None:
        raise IndexCorruptionError(
            "版本行缺失", details={"doc_id": doc_id, "version": version}
        )

    digest = sha256_hex(raw_content)
    if digest != row["content_sha256"]:
        raise IndexCorruptionError(
            "原文摘要与版本绑定摘要不一致：索引可能不属于该原文",
            details={
                "doc_id": doc_id,
                "version": version,
                "stored_sha256": row["content_sha256"],
                "actual_sha256": digest,
            },
        )

    if row["gcb_table_version"] != expected_gcb or row["unidata_version"] != expected_unidata:
        raise UnicodeVersionMismatchError(
            "构建时 Unicode 数据版本与当前运行环境不一致",
            details={
                "stored": {
                    "gcb_table_version": row["gcb_table_version"],
                    "unidata_version": row["unidata_version"],
                },
                "running": {
                    "gcb_table_version": expected_gcb,
                    "unidata_version": expected_unidata,
                },
            },
        )

    cp_to_byte = tuple(decode_array(row["cp_to_byte"]))
    cp_to_cluster = tuple(decode_array(row["cp_to_cluster"]))
    cluster_to_cp = tuple(decode_array(row["cluster_to_cp"]))
    cluster_to_byte = tuple(cp_to_byte[c] for c in cluster_to_cp)

    text = raw_content.decode("utf-8")  # 调用方已保证合法（写入时已严格校验）
    index = TextIndex(
        text=text,
        cp_to_byte=cp_to_byte,
        cp_to_cluster=cp_to_cluster,
        cluster_to_cp=cluster_to_cp,
        cluster_to_byte=cluster_to_byte,
    )

    # 结构自洽 + 与重算结果一致
    rebuilt = build_index(text)
    problems: list[str] = []
    if (index.byte_count, index.cp_count, index.cluster_count) != (
        row["byte_count"], row["cp_count"], row["cluster_count"]
    ):
        problems.append("counts_mismatch")
    if index.cp_to_byte != rebuilt.cp_to_byte:
        problems.append("cp_to_byte_mismatch")
    if index.cp_to_cluster != rebuilt.cp_to_cluster:
        problems.append("cp_to_cluster_mismatch")
    if index.cluster_to_cp != rebuilt.cluster_to_cp:
        problems.append("cluster_to_cp_mismatch")
    if problems:
        raise IndexCorruptionError(
            "存储索引与原文重算结果不一致",
            details={"doc_id": doc_id, "version": version, "problems": problems},
        )

    import json
    edit_info = json.loads(row["edit_info"]) if row["edit_info"] else None
    return LoadedVersion(
        doc_id=doc_id,
        version=version,
        index=index,
        content_sha256=digest,
        gcb_table_version=row["gcb_table_version"],
        unidata_version=row["unidata_version"],
        build_mode=row["build_mode"],
        edit_info=edit_info,
    )


def describe_clusters(index: TextIndex, limit: int | None = None) -> list[dict[str, Any]]:
    """逐簇还原，供诊断核对。每个簇给出三种坐标、码点、UTF-8 字节长度。"""
    out: list[dict[str, Any]] = []
    total = index.cluster_count
    upto = total if limit is None else min(limit, total)
    for cid in range(upto):
        cp_s, cp_e = index.cluster_to_cp[cid], index.cluster_to_cp[cid + 1]
        b_s, b_e = index.cluster_to_byte[cid], index.cluster_to_byte[cid + 1]
        cluster = index.text[cp_s:cp_e]
        out.append(
            {
                "cluster": cid,
                "codepoint_start": cp_s,
                "codepoint_end": cp_e,
                "byte_start": b_s,
                "byte_end": b_e,
                "text": cluster,
                "codepoints": [f"U+{ord(ch):04X}" for ch in cluster],
                "gcb_groups": [
                    gcb_group(ch).value for ch in cluster
                ],
                "byte_length": b_e - b_s,
            }
        )
    return out
