# 夹具（Fixtures）

## `hand_vectors.json` — 手算向量

* 每条向量带 `scratch`（逐步整数算术草稿），期望值由人工推导，不经过被测内核。
* 覆盖：目标负载、空块、满块、75%/25%、极低基础费（7 粘住、1 最小 +1）、
  floor 除法方向钉死、6 块多区块递推、交易扣费与失败类别（E020/E030/E032）。

## `chain_fixture.json` — 合成链

由 `scripts/build_fixtures.py` 用 `reference/oracle.py`（独立实现，直接调 ecdsa）
生成并**已提交**，因此无需重算即可复现：

* 固定种子账户 alice/bob/carol/dave/eve（种子 1..5，私钥仅用于本地合成）。
* 4 个块：小负载（含费帽不足 E020、坏签名 E011）→ 满块（含余额不足 E033）→
  空块 → 低费小负载（含 nonce 错 E031）。
* 3 个**应被整块拒绝**的案例：E040（gas 超上限）、E043（base fee 不衔接）、
  E041（声明 gas_used 与有效交易不符）。
* 父哈希占位符 `"<computed: block N hash>"` 由回放引擎按已接受块实时解析。

重新生成：`make fixtures`（会覆盖本文件；签名是确定性的，相同代码生成相同字节）。
