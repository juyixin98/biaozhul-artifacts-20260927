# 固定键宽稀疏 Merkle 状态服务（Fixed-Key-Width Sparse Merkle State Service）

一个本地可复现的稀疏 Merkle 状态服务：固定 32 字节键宽（256 位深）、SHA-256、
Ed25519 版本检查点；支持**更新/删除、成员证明、非成员证明、压缩路径展开、
历史版本证明、确定化批处理、独立离线回放与篡改检测**。全部数据为本地合成夹具，
不依赖任何生产账号或真实业务数据。

技术栈：Python 3.11+ · FastAPI · SQLite · `cryptography`（Ed25519）· 标准库
`hashlib`（SHA-256）· pytest/httpx。

---

## 1. 五分钟上手

```bash
# 1) 建虚拟环境并装依赖（系统 Python 受 PEP 668 保护，必须用 venv）
make install          # = python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) 跑全部 76 个测试（单元 + 集成）
make test             # = .venv/bin/python -m pytest -v

# 3) 生成合成开发密钥、播种样例数据、起服务
make keygen           # configs/dev_signing_key.pem(.pub)
make seed             # data/sample.db，3 个版本（含删除/复原/空值）
make run              # http://127.0.0.1:8080
```

另开一个终端：

```bash
KEY=$(printf 'ab%.0s' $(seq 1 32))   # 32 字节键
curl -s -X POST localhost:8080/updates -H 'Content-Type: application/json' \
  -d "{\"updates\":[{\"key\":\"$KEY\",\"value\":\"0102\"}]}"
curl -s "localhost:8080/proof/$KEY"                       # 取证明信封
curl -s -X POST localhost:8080/verify -H 'Content-Type: application/json' \
  -d "{\"proof\": $(curl -s localhost:8080/proof/$KEY | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["proof"]))'), \"expect_membership\":true}"
```

离线核验（受信公钥带外提供，**绝不从数据库读取**）：

```bash
make replay           # 从 data/smt.db 直接回放 + 活动库存活键交叉出证
# 或：导出 JSONL（可拷到无库环境）再回放
.venv/bin/python scripts/export_journal.py --db data/smt.db --out data/journal.jsonl
.venv/bin/python -m app.offline --journal data/journal.jsonl \
    --public-key configs/dev_signing_key.pem.pub
```

退出码：`ACCEPT=0`、`REJECT=2`、`INCONCLUSIVE=3`，可直接用于 CI 门禁。

---

## 2. 模块划分（真实职责，非单文件脚本）

```
app/
├── coding/            # ① 编码与验签（无树逻辑，内核与离线验证器共同依赖）
│   ├── hashing.py     #   SHA-256 原语；叶/分支/空槽三类域标签；逐层空摘要表
│   ├── keys.py        #   定宽键解析、MSB 位读取、首分歧层
│   ├── nodes.py       #   LeafNode（绑定键+值）/ BranchNode（绑定深度+左右摘要）
│   ├── params.py      #   TreeParams(key_len, depth) 与空摘要调度
│   ├── serialization.py  # 规范化 JSON（签名载荷跨实现逐字节一致）
│   └── signing.py     #   Ed25519 生成/PEM/hex；版本检查点签名与验签
├── core/              # ② 链状态内核（不依赖 FastAPI/SQLite）
│   ├── smt.py         #   规范递归 SMT：上浮叶、分裂、删除坍缩、出证
│   ├── proof.py       #   压缩证明 LogicalProof + 纯函数核验（失败分类）
│   ├── batch.py       #   确定化批处理：后者覆盖、按键排序、批根=逐条根
│   ├── store.py       #   NodeStore 协议 + InMemoryStore
│   └── errors.py
├── storage/
│   └── sqlite_store.py# ③ 索引存储：nodes/versions/key_index/journal/idempotency
├── api/               #   服务编排与 HTTP
│   ├── state_service.py  # “一个版本=一个事务”：批处理+签名+索引+幂等
│   ├── proof_envelope.py # 证明线格式（封闭字段集，绑定根/键/深度）
│   ├── routes.py / schemas.py
├── offline/           # ④ 离线回放（刻意不导入 app.core 的树逻辑）
│   ├── verifier.py    #   独立重写的证明展开/核验（只依赖 app.coding）
│   ├── replay.py      #   JSONL 重放：根/签名/父子链 + 活动库交叉核验
│   ├── replay_cli.py / __main__.py
├── diagnostics.py     # 三态判定 ACCEPT/REJECT/INCONCLUSIVE + 机器可读 reason
├── logging_setup.py   # 结构化日志；值永不打印，键只打 SHA-256 指纹
└── main.py            # 应用工厂、请求标识中间件、全局异常脱敏
scripts/               # keygen / seed / golden vectors / journal 导出
tests/
├── unit/              # 55 个单元用例（哈希/树/批处理/证明/签名/交叉验证）
├── integration/       # FastAPI+SQLite 端到端、离线回放、篡改检测（21 用例）
└── reference/
    └── naive_smt.py   # 第三份独立实现（仅 hashlib）：金标向量与交叉比对的来源
samples/               # golden_vectors.json（具体哈希金标）
configs/dev.env        # 本地合成环境变量样例
```

### 三套实现互相印证（防止“参考答案由被测实现自己生成”）

1. **被测内核** `app/core/proof.py`；
2. **独立离线验证器** `app/offline/verifier.py`（不导入 `core`，重写展开逻辑）；
3. **朴素参考** `tests/reference/naive_smt.py`（仅依赖 `hashlib`，从键集合直接
   重建整树并独立出证/验证），金标 `samples/golden_vectors.json` 由它生成。

`tests/unit/test_independent_verifier.py` 在多组夹具（含 256 位随机簇）上断言
三者给出相同根、相同证明与相同篡改失败类别。

---

## 3. 关键密码学规则（与题目要求逐条对应）

### 3.1 空节点逐层定义；叶内绑定键值；不存在 ≠ 值为空

单字节域标签永久固定：

| 节点 | 摘要 |
|---|---|
| 叶 `LEAF` | `SHA256(0x00 ‖ len(key) ‖ key ‖ value)` |
| 分支 `BRANCH` | `SHA256(0x01 ‖ depth(2B) ‖ left(32B) ‖ right(32B))` |
| 叶层空槽 `EMPTY` | `SHA256(0x02)` |

空摘要逐层向上派生：`E[D]=empty_leaf`，
`E[d] = branch(d, E[d+1], E[d+1])`，空根即 `E[0]`。

* 分支**深度入摘要**，杜绝跨层换节点；左右孩子恒为定长 32 字节（空孩子代入
  `E[d+1]`，不是省略），独立验证器只需空摘要表即可展开任意压缩路径。
* **空值叶** `leaf(k, "")` 与**空槽** `E[D]` 标签不同，摘要必然不同：
  “键存在但值为空字节串”和“键不存在”在密码学上不可能混同。
* 单键子树“上浮”：只含一个键的子树，其根摘要就是叶摘要（叶可停在任意层）。

### 3.2 批更新的确定性规则

1. 同批同键多次 → **后者覆盖（last-write-wins）**；
2. 按键字节序升序规范化后顺序应用；
3. 空批根不变。配合删除时的单叶提升规则，**同一键值集合 ⇒ 唯一结构 ⇒ 唯一根**，
   与更新顺序、分批方式无关。测试 `test_batch_root_equals_one_by_one_*` 用
   独立内核实例在 8 位与 256 位（随机 12 键 + 重复 + 删除 + 乱序）上断言
   **批更新根 == 逐条更新根 == 朴素集合根**。

### 3.3 证明绑定根、键、深度；压缩路径必须可展开

线格式信封 `smt-proof/v1`（字段封闭，多余/缺失字段一律拒绝）：

```json
{ "schema":"smt-proof/v1", "key_len":32, "depth":256,
  "key":"<hex>", "root":"<32B hex>", "end":"leaf|empty|wrong_key",
  "bitmap":"<D/8 字节>", "siblings":["<32B hex>", …],
  "value":null, "collision_key":null, "collision_value":null }
```

* `bitmap` 按 MSB 打包，置位层 = 路径经过的实体分支层；规范编码要求置位层是
  严格前缀 `{0..L-1}`，尾部填充位必须为 0（防同一证明两种编码）。
* `siblings` 是每层路径另一侧孩子的 32B 摘要（单边链处即 `E[d+1]`）。
  验证器从“上浮槽”（叶摘要 / `E[L]` / 碰撞叶摘要）自下而上逐层
  `branch(d, …, …)` 展开，最终必须等于信封内根——不需要服务端补任何数据。
* **成员**：`end=leaf`，起点 `leaf_digest(key,value)`，叶内绑定键值；
  调用方还可带 `expect_value`，不符即 `VALUE_MISMATCH`。
* **非成员（空槽）**：`end=empty`，起点 `E[L]`。
* **非成员（碰撞）**：`end=wrong_key`，给出路径上实际存在的叶，并要求
  碰撞叶键的前 L 个路径位与被证键一致（**路径前缀绑定**），否则
  `KEY_BINDING_MISMATCH`。
* 深度/键宽随信封绑定；验证器以不同参数核验即失败。

### 3.4 旧根仍可验证历史证明

节点按内容寻址存储（`INSERT OR IGNORE`，append-only），更新只新增节点，
旧根引用的节点永不物理删除。`GET /proof/{key}?version=N` 可取任意历史版本
的证明，并在该历史根上独立核验（集成测试 `test_historical_root_verifies_old_proof`：
v2 改值后，v1 证明仍在 v1 根上 ACCEPT，而用 v2 的期望值核验被 VALUE_MISMATCH）。

### 3.5 版本检查点与离线回放

每版对规范化 JSON
`{schema:"smt-checkpoint/v1", version, root, parent_root, batch_id}` 做
Ed25519 签名（version 入消息，防检查点跨版本重放）。离线回放四查：

1. 用**独立内核**从空根重放流水，逐版重算根比对；
2. 逐版验检查点签名；
3. 验 `parent_root` 版本链连续；
4. 给活动库时，对 `key_index` 每个存活键现取现验（抓“流水没问题但活动库
   索引/节点被直接篡改”）。

---

## 4. HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `GET /health` | 存活与当前版本 |
| `GET /root?version=N` | 指定版本（默认最新）根与检查点元数据 |
| `GET /versions/{N}` | 版本链检查点详情 |
| `GET /value/{key_hex}` | 当前值；`exists=false/value=null` 表示不存在；`value=""` 表示空值 |
| `GET /proof/{key_hex}?version=N` | 证明信封（默认最新，可查历史） |
| `POST /updates` | 批更新/删除；`value=null` 删除，`""` 空值；支持 `idempotency_key` |
| `POST /verify` | 调用**独立离线验证器**核验证明信封 |

`POST /updates` 体：

```json
{"updates": [{"key": "<64 hex 字符>", "value": "0a1b"}],
 "idempotency_key": "可选"}
```

幂等：同键同载荷返回同一版本（`idempotent_replay=true`），同键异载荷
`409`。

### 判定状态码与失败类别（测试断言具体 reason，不只看能调用）

| 场景 | HTTP | decision | reason（节选） |
|---|---|---|---|
| 核验通过 | 200 | `ACCEPT` | `MEMBERSHIP_VERIFIED` / `NON_MEMBERSHIP_VERIFIED` |
| 重算根不符（篡改根/兄弟/叶值） | 422 | `REJECT` | `ROOT_MISMATCH`（响应同时给 claimed 与 recomputed 根） |
| 声明值不符 | 422 | `REJECT` | `VALUE_MISMATCH` |
| 碰撞叶路径绑定被破坏 | 422 | `REJECT` | `KEY_BINDING_MISMATCH` |
| 成员/非成员类型不符 | 422 | `REJECT` | `KIND_MISMATCH` |
| 检查点签名错 / 版本链断 | 回放 2 | `REJECT` | `SIGNATURE_INVALID` / `PARENT_ROOT_MISMATCH` |
| 信封格式坏、字段不封闭、hex 非法 | 400 | `INCONCLUSIVE` | `ENVELOPE_MALFORMED` / `ENCODING_ERROR` |
| 未知版本/根 | 404 | — | `UNKNOWN_ROOT` |
| 公钥不可信/缺失 | 回放 3 | `INCONCLUSIVE` | `PUBLIC_KEY_UNAVAILABLE` |

---

## 5. 诊断与敏感数据

* 每个请求都有 `X-Request-ID`（接受传入或服务端生成 UUID），错误体与日志
  全程携带，便于凭 ID 追溯“为什么接受/拒绝/无法判定”。
* 拒绝响应给出关键状态：`claimed_root`、`recomputed_root`、`version`、
  `key_fingerprint`、失败层级 `detail.levels`。
* **脱敏**：值永远不写日志/错误；键只输出 `SHA-256(key)` 前 12 个 hex 字符
  （`app/logging_setup.py:key_fingerprint`），不可逆且足够人工对应。

---

## 6. 题目必答问题 → 测试映射

| 要求 | 测试 |
|---|---|
| 两键长公共前缀 | `test_two_key_long_common_prefix_compressed_proof`（255 位共享、256 层压缩展开）、`test_depth8_two_keys_share_five_prefix_bits` |
| 删除复原 | `test_delete_then_restore_returns_exact_prior_root`、回放种子 v3、`test_replay_accepts_clean_history` |
| 非成员证明 | `test_non_membership_empty_tree/_collision_leaf/_empty_slot_below_branch/_floated_other_leaf_is_wrong_key` |
| 压缩证明篡改（具体失败类别） | `test_tamper_root/value/leaf_value/collision_key/sibling_*`、`test_non_canonical_bitmap_rejected`、`test_bitmap_padding_bits_must_be_zero` |
| 独立验证器核验 | `test_three_implementations_agree_on_depth8[*]`、`test_randomized_cross_implementation_agreement`、`/verify` 全部集成用例、离线回放 |
| 批更新 vs 逐个更新根 | `test_batch_root_equals_one_by_one_small_tree/_full_depth_random`、`test_multiple_batches_chain_deterministically`、`test_batch_root_equals_one_by_one_roots`（HTTP） |
| 参考答案不由被测实现生成 | `tests/reference/naive_smt.py`（独立 hashlib 实现）+ `samples/golden_vectors.json` 具体常量断言 |
| 空 vs 空值 | `test_empty_value_leaf_differs_from_absent_empty_slot`、`test_empty_byte_value_distinct_from_absent`、`test_empty_value_distinct_from_absent_over_api` |
| 旧根验证历史证明 | `test_historical_root_verifies_old_proof` |
| 断言具体结果/失败类别 | 全部用例使用具体 hex 根、`Reason` 枚举值与 `decision` 断言，无“仅调用成功”式断言 |

---

## 7. 测试命令与真实输出结论

```bash
$ .venv/bin/python -m pytest -q
........................................................................... [100%]
76 passed in 1.37s
```

离线回放（样例库，真实输出）：

```json
{
  "decision": "ACCEPT",
  "reason": "REPLAY_VERIFIED",
  "message": "全部 3 个版本根、签名与父子链核验一致",
  "versions_checked": 3,
  "live_proofs_checked": 4,
  "failures": []
}
```

篡改流水后回放：进程退出码 `2`，
`decision=REJECT`，`failures` 含 `ROOT_MISMATCH`（版本链也会报
`PARENT_ROOT_MISMATCH`），精确定位到首个不一致版本号与 claimed/recomputed 根。

---

## 8. 配置

`configs/dev.env`（或环境变量；`make run` 前可 `set -a; source configs/dev.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SMT_DB_PATH` | `data/smt.db` | SQLite 文件 |
| `SMT_SIGNING_KEY_PATH` | `configs/dev_signing_key.pem` | Ed25519 PEM；不存在则自动生成开发密钥（0600） |
| `SMT_SIGNING_KEY_HEX` | （空） | 32B Ed25519 私钥 hex，优先级高于 PEM |
| `SMT_KEY_LEN` / `SMT_DEPTH` | 32 / 256 | 固定键宽与树深（depth ≤ 8·key_len） |
| `SMT_HOST` / `SMT_PORT` | 127.0.0.1 / 8080 | 监听地址 |
| `SMT_LOG_REDACT` | 1 | 日志脱敏开关 |

生产部署应通过秘钥管理系统注入 `SMT_SIGNING_KEY_HEX`；离线核验方的公钥必须
带外信任。

## 9. 设计取舍备注

* 固定 256 位深下单点更新最坏 ~256 次存储往返；SQLite 本地库与批量场景下
  完全够用。若追求更高写吞吐，可在 `NodeStore` 协议后换批量写后端，
  树数学不变。
* 采用“上浮叶 + 单边链”的紧凑表示（类似 CometBFT/Celestia 节点存储风格），
  配合内容寻址 store，天然保留全部历史状态。
* 证明不含 Merkle 签名；信任根来自 Ed25519 签名的版本检查点，证明只承担
  “状态隶属于根”的密码学绑定。
