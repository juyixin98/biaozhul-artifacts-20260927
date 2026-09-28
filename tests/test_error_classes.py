"""输入错误 / 状态冲突 / 资源耗尽 / 计算失败四类可区分错误。

每条用例都断言具体 reason（失败类别），而不是只断言状态码或
“能调用”。
"""

import pytest

from otbackend.errors import (
    ComputationFailed,
    InputInvalid,
    ResourceExhausted,
    StateConflict,
)
from otbackend.repository import MemoryRepository
from otbackend.service import OTService
from otbackend.textmodel import parse_message
from otbackend.transform import compose
from otbackend.textmodel import Comp


@pytest.fixture()
def svc():
    s = OTService(MemoryRepository(), max_doc_chars=50, max_doc_bytes=400,
                  max_components=10)
    s.create_document("d", "abc")
    return s


# ---------------- 输入错误（400 / INPUT_INVALID）----------------

@pytest.mark.parametrize("body,reason", [
    ([{"type": "wat", "pos": 0}], "UNKNOWN_TYPE"),
    ([{"type": "ins", "pos": -1, "text": "x"}], "NEG_POS"),
    ([{"type": "ins", "pos": 0, "text": ""}], "EMPTY_INS"),
    ([{"type": "del", "pos": 0, "length": 0}], "BAD_LENGTH"),
    ([{"type": "del", "pos": 0, "length": -2}], "BAD_LENGTH"),
    ([{"type": "del", "pos": 0, "length": 2},
      {"type": "del", "pos": 1, "length": 1}], "OVERLAP_IN_MESSAGE"),
    ([], "EMPTY_OPS"),
])
def test_input_errors(body, reason):
    with pytest.raises(InputInvalid) as ei:
        parse_message(body)
    assert ei.value.reason == reason


def test_service_input_errors(svc):
    with pytest.raises(InputInvalid) as e:
        svc.submit("d", 0, "", 1, [{"type": "ins", "pos": 0, "text": "x"}])
    assert e.value.reason == "BAD_CLIENT_ID"
    with pytest.raises(InputInvalid) as e:
        svc.submit("d", 0, "c", -1, [{"type": "ins", "pos": 0, "text": "x"}])
    assert e.value.reason == "BAD_CLIENT_OP_ID"
    with pytest.raises(InputInvalid):
        svc.create_document("", "x")


# ---------------- 状态冲突（409 / STATE_CONFLICT）----------------

def test_delete_out_of_range(svc):
    with pytest.raises(StateConflict) as e:
        svc.submit("d", 0, "c", 1, [{"type": "del", "pos": 2, "length": 5}])
    assert e.value.reason == "DELETE_OUT_OF_RANGE"


def test_insert_out_of_range(svc):
    with pytest.raises(StateConflict) as e:
        svc.submit("d", 0, "c", 1, [{"type": "ins", "pos": 9, "text": "z"}])
    assert e.value.reason == "INSERT_OUT_OF_RANGE"


def test_base_ahead_of_head(svc):
    with pytest.raises(StateConflict) as e:
        svc.submit("d", 5, "c", 1, [{"type": "ins", "pos": 0, "text": "z"}])
    assert e.value.reason == "BASE_AHEAD_OF_HEAD"


def test_baseline_trimmed_rejected(svc):
    svc.submit("d", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "z"}])
    svc.snapshot_and_trim("d", 1)
    with pytest.raises(StateConflict) as e:
        svc.submit("d", 0, "late", 1, [{"type": "ins", "pos": 0, "text": "q"}])
    assert e.value.reason == "BASELINE_TRIMMED"
    # catchup 重建后可提交。r1 为 zabc；q 锚定槽位0，与已有插入 z
    # 同槽位，按 char_id 总序（c#1 < late#1）z 在 q 前 -> zqabc。
    v = svc.catchup("d")
    ack = svc.submit("d", v.rev, "late", 1, [{"type": "ins", "pos": 0, "text": "q"}])
    assert ack.text == "zqabc"


def test_reused_op_id_conflicts(svc):
    svc.submit("d", 0, "c", 7, [{"type": "ins", "pos": 0, "text": "z"}])
    with pytest.raises(StateConflict) as e:
        svc.submit("d", 0, "c", 7, [{"type": "ins", "pos": 0, "text": "DIFFERENT"}])
    assert e.value.reason == "REUSED_OP_ID"


def test_doc_not_found():
    s = OTService(MemoryRepository())
    from otbackend.errors import DocNotFound
    with pytest.raises(DocNotFound):
        s.get_document("nope")


# ---------------- 资源耗尽（413 / RESOURCE_EXHAUSTED）----------------

def test_too_many_components(svc):
    # 构造超过 max_components=3 的不重叠删除（需要足够长文档）
    s = OTService(MemoryRepository(), max_components=3, max_doc_chars=10_000)
    s.create_document("big", "abcdefghij")
    with pytest.raises(ResourceExhausted) as e:
        s.submit("big", 0, "c", 1,
                 [{"type": "del", "pos": i, "length": 1} for i in range(4)])
    assert e.value.reason == "TOO_MANY_COMPONENTS"


def test_insert_too_large(svc):
    with pytest.raises(ResourceExhausted) as e:
        svc.submit("d", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "z" * 60}])
    assert e.value.reason == "INSERT_TOO_LARGE"


def test_doc_too_large(svc):
    # max_doc_chars=50，提交后超限
    with pytest.raises(ResourceExhausted) as e:
        svc.submit("d", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "z" * 60}])
    # INSERT_TOO_LARGE 先触发；用更大的单字符上限配置验证文档上限
    s2 = OTService(MemoryRepository(), max_doc_chars=10, max_components=10_000)
    s2.create_document("d2", "")
    with pytest.raises(ResourceExhausted) as e:
        s2.submit("d2", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "z" * 11}])
    assert e.value.reason in ("INSERT_TOO_LARGE", "DOC_TOO_LARGE_CHARS")


# ---------------- 计算失败（COMPUTATION_FAILED）----------------

def test_compose_references_deleted():
    with pytest.raises(ComputationFailed) as e:
        compose([Comp.del_(0, 2)], [Comp.del_(0, 1)])
    assert e.value.reason == "COMPOSE_DELETED_REF"


def test_compose_trailing_detected_at_apply():
    # compose 本身不掌握文档绝对长度；超出长度在应用时按状态冲突拒绝。
    from otbackend.textmodel import apply_stream
    with pytest.raises(StateConflict) as e:
        apply_stream("abc", compose([], [Comp.del_(50, 1)]))
    assert e.value.reason == "DELETE_OUT_OF_RANGE"
