"""服务层 + HTTP 接口测试。

断言具体结果与失败类别（四类错误），并验证 run_id 回放记录包含
关键中间状态；增量编辑后逐版本核对。
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.errors import ErrorCategory


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ── 服务层：创建与查询 ───────────────────────────────────────────────────────
def test_create_and_convert(service):
    res = service.create_document("doc1", b64("a🇺🇳b".encode()), run_id="r1")
    assert res.version == 0
    assert res.stats["codepoint_count"] == 4  # a RI RI b
    assert res.content_sha256 == hashlib.sha256("a🇺🇳b".encode()).hexdigest()

    # 旗帜整体占 1 簇；字节 1..8 属于该簇
    info = service.get_version("doc1", None)
    assert info["lengths"] == {"bytes": 10, "codepoints": 4, "clusters": 3}

    assert service.convert_position("doc1", 0, 10, "byte", "cluster")["output_position"] == 3
    assert service.convert_position("doc1", 0, 9, "byte", "cluster")["output_position"] == 2
    assert service.convert_position("doc1", 0, 1, "cluster", "byte")["output_position"] == 1
    assert service.convert_position("doc1", 0, 2, "cluster", "byte")["output_position"] == 9
    assert service.convert_position("doc1", 0, 1, "cluster", "codepoint")["output_position"] == 1


def test_create_invalid_utf8_records_input_error(service):
    bad = b64(b"ok\xff")
    with pytest.raises(Exception) as ei:
        service.create_document("docX", bad, run_id="run-bad")
    assert ei.value.category == ErrorCategory.INPUT
    assert ei.value.code == "INVALID_UTF8"
    assert ei.value.details["offset"] == 2
    # 失败也入操作日志，可凭 run_id 回放
    replay = service.replay_run("run-bad")
    event = replay["events"][0]
    assert event["success"] is False
    assert event["error_category"] == "input_error"
    assert event["error_code"] == "INVALID_UTF8"
    assert event["detail"]["phase"] == "decode"


def test_create_invalid_base64(service):
    with pytest.raises(Exception) as ei:
        service.create_document("doc2", "@@not-base64@@", run_id="r2")
    assert ei.value.code == "INVALID_BASE64"
    assert ei.value.category == ErrorCategory.INPUT


def test_empty_field(service):
    with pytest.raises(Exception) as ei:
        service.create_document("doc3", "", run_id="r3")
    assert ei.value.code == "EMPTY_FIELD"


def test_limit_exceeded_is_resource_error(service):
    big = b64(b"x" * 5000)  # 设置上限 max_bytes=4096
    with pytest.raises(Exception) as ei:
        service.create_document("doc4", big, run_id="r4")
    assert ei.value.category == ErrorCategory.RESOURCE
    assert ei.value.code == "LIMIT_EXCEEDED"
    assert ei.value.details["resource"] == "bytes"
    assert ei.value.details["actual"] == 5000
    assert ei.value.details["limit"] == 4096


def test_duplicate_doc_is_state_conflict(service):
    service.create_document("dup", b64(b"x"), run_id="r5")
    with pytest.raises(Exception) as ei:
        service.create_document("dup", b64(b"y"), run_id="r5b")
    assert ei.value.category == ErrorCategory.STATE
    assert ei.value.code == "DOCUMENT_CONFLICT"


def test_missing_doc_and_version(service):
    with pytest.raises(Exception) as ei:
        service.get_version("nope", None)
    assert ei.value.code == "DOCUMENT_NOT_FOUND"
    service.create_document("d", b64(b"x"), run_id="r6")
    with pytest.raises(Exception) as ei2:
        service.convert_position("d", 9, 0, "byte", "cluster")
    assert ei2.value.code == "VERSION_NOT_FOUND"


# ── 服务层：增量编辑、版本链与乐观锁 ─────────────────────────────────────────
def test_edit_version_chain_and_optimistic_lock(service):
    service.create_document("ed", b64("abc".encode()), run_id="re")
    r1 = service.edit_document(
        "ed", expected_version=0, start=1, end=1, space="codepoint",
        replacement_base64=b64("🇽".encode()), run_id="re",
    )
    assert r1["version"] == 1
    assert r1["parent_version"] == 0
    assert r1["build_mode"] == "incremental_verified"
    # "a🇽bc"：4 码点；单个 RI 自成一簇 → 4 簇
    info = service.get_version("ed", 1)
    assert info["lengths"] == {"bytes": 7, "codepoints": 4, "clusters": 4}

    # 过期版本号 → 状态冲突
    with pytest.raises(Exception) as ei:
        service.edit_document(
            "ed", expected_version=0, start=0, end=0, space="codepoint",
            replacement_base64=b64(b""), run_id="re",
        )
    assert ei.value.code == "VERSION_CONFLICT"
    assert ei.value.details == {"expected": 0, "current": 1}

    # 版本链
    vs = service.list_versions("ed")["versions"]
    assert [v["version"] for v in vs] == [0, 1]
    assert vs[1]["content_sha256"] != vs[0]["content_sha256"]


def test_edit_rejects_non_boundary(service):
    service.create_document("fl", b64("🇺🇳".encode()), run_id="rf")
    with pytest.raises(Exception) as ei:
        service.edit_document(
            "fl", expected_version=0, start=1, end=1, space="codepoint",
            replacement_base64=b64(b""), run_id="rf",
        )
    assert ei.value.code == "NOT_A_BOUNDARY"
    assert ei.value.category == ErrorCategory.INPUT


def test_edit_invalid_utf8_replacement(service):
    service.create_document("g", b64(b"ab"), run_id="rg")
    with pytest.raises(Exception) as ei:
        service.edit_document(
            "g", expected_version=0, start=0, end=0, space="codepoint",
            replacement_base64=b64(b"\xff"), run_id="rg",
        )
    assert ei.value.code == "INVALID_UTF8"


def test_edit_result_sha_binds_new_content(service):
    service.create_document("h", b64(b"ab"), run_id="rh")
    r = service.edit_document(
        "h", expected_version=0, start=1, end=2, space="codepoint",
        replacement_base64=b64("é".encode()), run_id="rh",
    )
    new_raw = "aé".encode()
    assert r["content_sha256"] == hashlib.sha256(new_raw).hexdigest()
    # 摘要绑定：用新原文可加载；用旧原文加载被拒
    from app.errors import IndexCorruptionError
    from app.diagnostics import load_version
    with pytest.raises(IndexCorruptionError):
        load_version(
            service.store, "h", 1, raw_content=b"ab",
            expected_gcb=service.settings.gcb_table_version,
            expected_unidata=service.settings.unidata_version,
        )


# ── 计算失败类别：增量结果与全量重建不符时必须报 computation_failure ─────────
def test_computation_failure_when_incremental_diverges(service, monkeypatch):
    import app.service as svc_mod

    service.create_document("c", b64(b"abc"), run_id="rc")

    real_apply = svc_mod.apply_edit

    def faulty_apply(index, edit):
        result = real_apply(index, edit)
        # 人为篡改：丢掉第一个簇起点，构造一个与全量重建不一致的增量结果
        import dataclasses
        bad_starts = result.cluster_to_cp[1:]
        bad = dataclasses.replace(result, cluster_to_cp=bad_starts)
        return bad

    monkeypatch.setattr(svc_mod, "apply_edit", faulty_apply)
    with pytest.raises(Exception) as ei:
        service.edit_document(
            "c", expected_version=0, start=1, end=1, space="codepoint",
            replacement_base64=b64(b"x"), run_id="rc",
        )
    assert ei.value.category == ErrorCategory.COMPUTATION
    assert ei.value.code == "INDEX_INCONSISTENT"
    # 没有新版本落库（计算失败不产生版本）
    assert [v["version"] for v in service.list_versions("c")["versions"]] == [0]
    replay = service.replay_run("rc")
    last = [e for e in replay["events"] if not e["success"]][-1]
    assert last["error_category"] == "computation_failure"
    assert last["detail"]["phase"] == "full_rebuild_verify"


def test_edit_without_verification_marked_unverified(settings, store):
    import dataclasses
    from app.service import TextIndexService
    svc = TextIndexService(store, dataclasses.replace(settings, verify_incremental=False))
    svc.create_document("u", b64(b"ab"), run_id="ru")
    r = svc.edit_document(
        "u", expected_version=0, start=1, end=1, space="codepoint",
        replacement_base64=b64(b"c"), run_id="ru",
    )
    assert r["version"] == 1
    assert r["build_mode"] == "incremental_unverified"
    info = svc.get_version("u", 1)
    assert info["build_mode"] == "incremental_unverified"


# ── HTTP 接口 ────────────────────────────────────────────────────────────────
def test_http_full_flow(client):
    # health 暴露固定 Unicode 版本
    h = client.get("/health").json()
    assert h["gcb_table_version_pinned"] == "13.0.0"
    assert h["unidata_version_pinned"] == "15.0.0"

    resp = client.post(
        "/documents",
        json={"doc_id": "u1", "content_base64": b64("aé\r\nb".encode())},
        headers={"X-Run-ID": "http-run-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.headers["X-Run-ID"] == "http-run-1"

    # 转换：CRLF 簇起始字节 = 3（a=1, é=2），簇序号 2
    conv = client.get(
        "/documents/u1/convert",
        params={"position": 3, "from_space": "byte", "to_space": "cluster"},
    )
    assert conv.status_code == 200
    assert conv.json()["output_position"] == 2

    # 非法字节位置（é 内部偏移 2）
    bad = client.get(
        "/documents/u1/convert",
        params={"position": 2, "from_space": "byte", "to_space": "codepoint"},
    )
    assert bad.status_code == 422
    body = bad.json()
    assert body["category"] == "input_error"
    assert body["code"] == "NOT_A_BOUNDARY"
    assert body["run_id"]

    # 非法 UTF-8 建文档
    bad2 = client.post("/documents", json={"doc_id": "u2", "content_base64": b64(b"\xff")})
    assert bad2.status_code == 400
    assert bad2.json()["code"] == "INVALID_UTF8"

    # pydantic 校验失败也归一到统一信封
    bad3 = client.post("/documents", json={"doc_id": "", "content_base64": "x"})
    assert bad3.status_code == 422
    assert bad3.json()["code"] == "VALIDATION_ERROR"

    # 资源耗尽
    big = client.post(
        "/documents", json={"doc_id": "big", "content_base64": b64(b"z" * (3 * 1024 * 1024))}
    )
    assert big.status_code == 413
    assert big.json()["category"] == "resource_exhausted"

    # 不存在文档
    assert client.get("/documents/nope").status_code == 404

    # 诊断：逐簇
    cl = client.get("/documents/u1/clusters").json()
    assert [c["text"] for c in cl["clusters"]] == ["a", "é", "\r\n", "b"]

    # 回放
    replay = client.get("/diagnostics/runs/http-run-1").json()
    assert replay["run_id"] == "http-run-1"
    assert any(e["op"] == "create" and e["success"] for e in replay["events"])


def test_http_edit_and_versions(client):
    client.post("/documents", json={"doc_id": "v", "content_base64": b64("x🇺🇳y".encode())})
    r = client.post(
        "/documents/v/edits",
        json={
            "expected_version": 0,
            "start": 1, "end": 2, "space": "cluster",
            "replacement_base64": b64("👨‍👩".encode()),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    conv = client.get(
        "/documents/v/convert",
        params={"position": 0, "from_space": "cluster", "to_space": "byte", "version": 1},
    )
    # 首簇 x 在 0，ZWJ 表情簇起点字节为 1
    assert conv.json()["output_position"] == 0
    vs = client.get("/documents/v/versions").json()
    assert len(vs["versions"]) == 2
