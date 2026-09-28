"""两个本地模拟客户端：并发编辑、乱序投递、重传、裁剪重建。"""

from itertools import permutations


from otbackend.clientsim import ScriptedNetwork, SimClient
from otbackend.repository import MemoryRepository
from otbackend.service import OTService


def test_two_clients_converge_in_order():
    svc = OTService(MemoryRepository())
    svc.create_document("doc", "hello")
    ta = SimClient("alice", _Direct(svc, "doc"), "doc", "hello")
    tb = SimClient("bob", _Direct(svc, "doc"), "doc", "hello")
    ta.local_insert(5, "!")
    ta.flush()
    tb.local_insert(0, "(")
    tb.local_insert(6, ")")
    tb.flush()
    ta.poll(); tb.poll()
    tb.flush()                 # queued 提升为在途
    ta.poll(); tb.poll()
    server = svc.get_document("doc").text
    assert ta.text == tb.text == server
    # bob 的 '(' 与 ')' 锚在它本地视图（未含 alice 的 '!'），组合后
    # ')' 位于 hello 之后、'!' 之前 —— 三端收敛到同一确定文本。
    assert server == "(hello!)"


def test_scripted_network_reorders_to_convergence():
    for order in permutations(range(3)):
        svc = OTService(MemoryRepository())
        svc.create_document("doc", "abc")
        net = ScriptedNetwork(svc)
        ta = SimClient("alice", net, "doc", "abc")
        tb = SimClient("bob", net, "doc", "abc")
        tc = SimClient("carol", net, "doc", "abc")
        ta.local_insert(0, "A"); ta.flush()
        tb.local_insert(1, "b"); tb.flush()
        tc.local_delete(0, 2); tc.flush()
        net.deliver_all(list(order))
        ta.poll(); tb.poll(); tc.poll()
        server = svc.get_document("doc").text
        assert ta.text == tb.text == tc.text == server, (order, ta.text, tb.text, tc.text)


def test_duplicate_network_delivery():
    svc = OTService(MemoryRepository())
    svc.create_document("doc", "abc")
    net = ScriptedNetwork(svc)
    t = SimClient("alice", net, "doc", "abc")
    t.local_insert(0, "X"); t.flush()
    # 队列里仍有一条未投递请求；复制后投第一份，再投重复副本
    dup = net.deliver_duplicate(0)       # 复制（不消费队列）
    first = net.deliver_one(0)           # 原件投递
    assert first["response"]["rev"] == dup["response"]["rev"]
    assert svc.get_document("doc").rev == 1


def test_client_rebuilds_after_trim():
    svc = OTService(MemoryRepository())
    svc.create_document("doc", "abc")
    transport = _Direct(svc, "doc")
    # 客户端停留在 r0（不 poll），期间服务端推进并裁剪到 r2
    other = SimClient("other", transport, "doc", "abc")
    other.local_insert(0, "Z"); other.flush(); other.poll()
    other.local_insert(1, "W"); other.flush(); other.poll()
    svc.snapshot_and_trim("doc", 2)

    stale = SimClient("alice", transport, "doc", "abc")  # server_rev=0
    stale.local_insert(0, "Y")
    out = stale.flush()      # base_rev=0 < baseline=2 -> 自动重建
    assert out.get("rebuilt") is True
    assert stale.rebuilds == 1
    # 重建后文本与服务端一致（在途编辑被丢弃，需要客户端重发——符合
    # “裁剪前旧客户端必须重建基线”的契约）
    assert stale.text == svc.get_document("doc").text


class _Direct:
    """直连 OTService 的最小传输（无队列），复用 service 调用。"""

    def __init__(self, svc, doc_id):
        self.svc = svc
        self.doc_id = doc_id

    def submit(self, doc_id, base_rev, client_id, client_op_id, ops):
        ack = self.svc.submit(doc_id, base_rev, client_id, client_op_id, ops)
        return {"rev": ack.rev, "head_rev": ack.head_rev}

    def history(self, doc_id, since):
        revs = self.svc.history(doc_id, since=since, limit=10_000)
        return {
            "baseline_rev": self.svc.repo.baseline_rev(doc_id),
            "head_rev": self.svc.repo.head_rev(doc_id),
            "revisions": [
                {"rev": r.rev, "client_id": r.client_id,
                 "client_op_id": r.client_op_id, "base_rev": r.base_rev,
                 "length_before": r.length_before, "length_after": r.length_after,
                 "checksum": r.checksum, "ops": [c.to_dict() for c in r.ops]}
                for r in revs],
        }

    def catchup(self, doc_id):
        v = self.svc.catchup(doc_id)
        return {"rev": v.rev, "baseline_rev": v.baseline_rev, "text": v.text}
