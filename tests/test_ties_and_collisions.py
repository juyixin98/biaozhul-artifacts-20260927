"""同分按稳定规范键排序 + 规范化碰撞。

覆盖：
- 同 key 碰撞（全角/不同大小写/Unicode ß）：分数相同，按 (key, surface, id) 稳定全序；
- 不同 key 同分时 key 字典序小者排前；
- 显示原文始终原样返回（不返回规范化串作为 surface）。
"""
from __future__ import annotations


def test_normalization_collision_tie_order(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    result, trace = trie.top_k("multicast", 10, collect_trace=True)
    got = [(e.id, e.surface) for e in result]
    # 四个词规范化后都是 "multicast"，同分 40；顺序必须是稳定全序：
    # key 相同 -> surface 升序 -> id 升序。
    expected = [
        ("col-2", "MULTIcast"),
        ("col-3", "MuLtIcAsT"),
        ("mp-08", "multicast"),
        ("col-1", "ＭＵＬＴＩcast"),
    ]
    log("THEN", "GIVEN", prefix="multicast", expected=expected, actual=got)
    assert got == expected, f"规范化碰撞同分排序错误: {got}，预期 {expected}"
    # 显示原文必须原样保留。
    surfaces = [e.surface for e in result]
    assert "ＭＵＬＴＩcast" in surfaces and "multicast" in surfaces
    assert all(e.key == "multicast" for e in result)
    log("PASS", "PASS", collision_size=len(result), surfaces_preserved=True)


def test_eszett_collision(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    result, _ = trie.top_k("strasse", 5)
    got = [e.id for e in result]
    expected = ["uni-3", "uni-1", "uni-2"]  # STRASSE < Straße < strasse（原始码点序）
    assert got == expected, f"ß 碰撞排序错误: {got}，预期 {expected}"
    log("PASS", "PASS", expected=expected, actual=got)


def test_equal_score_across_keys_sorted_by_key(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    # mp-08 multicast 与 mp-09 multiplex 同为 40，key: multicast < multiplex。
    result, _ = trie.top_k("multi", 20)
    by_id = {e.id: i for i, e in enumerate(result)}
    assert by_id["mp-08"] < by_id["mp-09"], "同分时必须按规范化键升序"
    # 80 分的 mp-02(multiprocessor) 与 mp-03(multiprogramming)：
    # multiprocessor < multiprogramming。
    assert by_id["mp-02"] < by_id["mp-03"]
    log("PASS", "PASS", rule="score desc, then key/surface/id asc")


def test_tie_break_is_repeatable(trie_with_corpus, log):
    trie, _ = trie_with_corpus
    runs = []
    for _ in range(20):
        res, _ = trie.top_k("multicast", 4)
        runs.append([e.id for e in res])
    assert all(r == runs[0] for r in runs), "同分排序必须可复现"
    log("PASS", "PASS", repeats=20, order=runs[0])


def test_surface_returned_verbatim_via_api(client, raw_corpus, log):
    # 批量灌入后，查询返回的 surface 必须与输入逐字符相同。
    resp = client.post("/entries/bulk", json={"items": raw_corpus})
    assert resp.status_code == 200, resp.text
    r = client.get("/complete", params={"prefix": "multicast", "k": 4})
    entries = r.json()["entries"]
    assert [e["surface"] for e in entries] == [
        "MULTIcast", "MuLtIcAsT", "multicast", "ＭＵＬＴＩcast"
    ]
    assert all(e["normalized_key"] == "multicast" for e in entries)
    log("PASS", "PASS", surfaces=[e["surface"] for e in entries])
