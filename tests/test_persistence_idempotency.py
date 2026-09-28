"""幂等提交、多字节码点、存储持久化与校验和。"""


import pytest

from otbackend.repository import MemoryRepository, SqliteRepository
from otbackend.service import OTService


@pytest.fixture(params=["memory", "sqlite-mem", "sqlite-file"])
def repo(request, tmp_path):
    if request.param == "memory":
        return MemoryRepository()
    if request.param == "sqlite-mem":
        return SqliteRepository(":memory:")
    return SqliteRepository(str(tmp_path / "test.db"))


def test_duplicate_submit_is_idempotent(repo):
    s = OTService(repo)
    s.create_document("d", "abc")
    body = [{"type": "ins", "pos": 1, "text": "X"}]
    r1 = s.submit("d", 0, "alice", 5, body)
    r2 = s.submit("d", 0, "alice", 5, body)          # 超时重传
    r3 = s.submit("d", 1, "alice", 5, body)          # 带新 base 的重传
    assert r1.rev == r2.rev == r3.rev == 1
    assert s.get_document("d").rev == 1
    # 历史只有一条修订
    assert [r.rev for r in s.history("d", 0)] == [1]


def test_same_op_id_different_body_rejected(repo):
    from otbackend.errors import StateConflict
    s = OTService(repo)
    s.create_document("d", "abc")
    s.submit("d", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "X"}])
    with pytest.raises(StateConflict) as e:
        s.submit("d", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "Y"}])
    assert e.value.reason == "REUSED_OP_ID"


def test_multibyte_code_points(repo):
    s = OTService(repo)
    s.create_document("d", "a😀b")          # 3 个码点
    assert s.get_document("d").length_chars == 3
    # 在码点 1（emoji）处插入中文，再删 emoji（码点，不是字节）
    s.submit("d", 0, "c", 1, [{"type": "ins", "pos": 1, "text": "中"}])
    s.submit("d", 1, "c", 2, [{"type": "del", "pos": 2, "length": 1}])
    v = s.get_document("d")
    assert v.text == "a中b", v.text
    assert v.length_chars == 3
    # 字节长度与码点长度不同，验证坐标从不按字节
    assert len(v.text.encode("utf-8")) > v.length_chars


def test_multibyte_concurrent_inserts_converge(repo):
    s = OTService(repo)
    s.create_document("d", "あ")
    s.submit("d", 0, "alpha", 1, [{"type": "ins", "pos": 0, "text": "い"}])
    s.submit("d", 0, "beta", 1, [{"type": "ins", "pos": 0, "text": "う"}])
    v = s.get_document("d").text
    # 确定总序：alpha < beta，い(alpha) 在左
    assert v == "いうあ", v


def test_overlapping_deletes_counted_once(repo):
    s = OTService(repo)
    s.create_document("d", "abcdef")
    s.submit("d", 0, "a", 1, [{"type": "del", "pos": 1, "length": 3}])  # bcd
    s.submit("d", 0, "b", 1, [{"type": "del", "pos": 2, "length": 2}])  # cd 重叠
    # 并集删除 bcd，剩 aef；不会因重复扣除产生负长度/错位
    assert s.get_document("d").text == "aef"


def test_sqlite_persistence_across_connections(tmp_path):
    db = str(tmp_path / "persist.db")
    s1 = OTService(SqliteRepository(db))
    s1.create_document("d", "hello")
    s1.submit("d", 0, "c", 1, [{"type": "ins", "pos": 5, "text": "!"}])
    # 用新连接重新打开同一文件
    s2 = OTService(SqliteRepository(db))
    v = s2.get_document("d")
    assert v.text == "hello!" and v.rev == 1
    # 历史与幂等表也持久化
    assert s2.submit("d", 0, "c", 1,
                     [{"type": "ins", "pos": 5, "text": "!"}]).rev == 1


def test_checksum_present_and_distinct(repo):
    s = OTService(repo)
    s.create_document("d", "abc")
    s.submit("d", 0, "c", 1, [{"type": "ins", "pos": 0, "text": "X"}])
    s.submit("d", 1, "c", 2, [{"type": "del", "pos": 1, "length": 1}])
    sums = {r.rev: r.checksum for r in s.history("d", 0)}
    assert len(sums) == 2 and sums[1] != sums[2]
    assert all(len(c) == 16 for c in sums.values())
