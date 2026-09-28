# 架构与模块（Architecture）

```
                ┌──────────────────────────────────────────────┐
 本地夹具/HTTP → │ api (FastAPI, schemas, wire RLP/JSON, 日志)    │
                └───────────────┬──────────────────────────────┘
                                │
              ┌─────────────────┴──────────────────┐
              ▼                                     ▼
   encoding/                          kernel/（链状态内核）
   - rlp.py（规范编解码）               - eip1559.py（纯费用递推）
   - serialization.py（摘要/哈希）       - execution.py（有效性+状态转移）
   - crypto.py（ecdsa: 签/恢复/验签）    - chain.py（genesis/父块连接）
              │                                     │
              └──────────────┬──────────────────────┘
                             ▼
                   storage/（SQLite 持久索引）
                             ▲
              replay/（离线回放引擎）── scripts/replay_blocks.py（CLI）
                             ▲
                   reference/oracle.py（独立预言机，禁止 import basefee）
                   fixtures/（手算向量 + 签名合成链）
```

## 模块职责

| 模块 | 承担的实际工作 |
|---|---|
| `basefee/encoding` | RLP 规范序列化（拒绝非最短编码）、SHA-256 标识、secp256k1 签名/公钥恢复/严格验签 |
| `basefee/kernel/eip1559.py` | 纯费用算术：下一块基础费用、实际小费/实际价格；可解释的每一步中间值 |
| `basefee/kernel/execution.py` | 交易恢复与验证（费帽/溢出/gas/nonce/余额）、原子区块状态转移、费用守恒记账 |
| `basefee/kernel/chain.py` | 创世、父块号/哈希衔接、区块头哈希、链头维护 |
| `basefee/storage` | SQLite 原子写入：块、交易、回执、无效交易、余额、head 元数据；大数用十进制 TEXT |
| `basefee/replay` | 读取本地 JSON 场景，逐块执行+落库，产出分离失败/警告的回放报告 |
| `basefee/api` | FastAPI：纯费用预览、区块提交/查询、健康检查；请求身份关联与结构化日志 |

## 独立性如何保证（防止"自证正确"）

1. **手算向量** `fixtures/hand_vectors.json`：期望值逐步手算并附算术草稿，
   核心实现不参与生成。
2. **独立预言机** `reference/oracle.py`：用标准库 + 直接调用 ecdsa，以不同写法
   重新推导费用规则，**禁止** import `basefee`（有测试守卫）；夹具期望值由它生成，
   并在 400+ 随机输入上与内核交叉比对。
3. 测试签名全部由预言机直接产出，再喂给内核验证，避免"用被测代码自己签名"。

## 失败分类与可解释性

* 每个失败有稳定错误码（`src/basefee/errors.py`），测试断言**具体类别**。
* 每个响应/日志携带 `request_id`、`protocol_version`、`component`（处理位置），
  费用递推返回 `delta_numerator / 两次 floor 中间值 / direction / 是否触发最小增量`。
* `failures[]`（硬失败）与 `warnings[]`（非阻断不确定提示）始终是两个独立数组。
