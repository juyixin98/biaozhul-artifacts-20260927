# 协议说明：字段级承诺与选择性披露

协议版本：`audit-commit-v1`。版本号进入每一个域分离标签，跨版本的证明/根
无法互相接受。

## 1. 参与角色与数据流

全部参与者均为本地合成角色，无外部账号：

* **提交方（prover/service）**：接收一批类型化记录，为每个字段生成承诺与
  随机盐，计算批次根并落库（公开数据与私有盐分表存储）。
* **持有方（同一本地服务）**：按需披露单个字段，出具绑定批次根与字段路径的
  披露证明。
* **验证方（verifier）**：只需要**信任渠道获得的批次根**和证明本身。验证
  函数是纯函数，不访问服务、数据库或任何盐存储；`independent/verifier.py`
  是仅用 Python 标准库（`hashlib`）重写的第二份实现。

## 2. 类型化字段编码

六种类型，每个值编码为 `(类型标签, 规范化载荷字节)`：

| 类型 | JSON 形态 | 规范化规则 |
|---|---|---|
| `text` | string | UTF-8 字节，不做 Unicode 折叠 |
| `int` | JSON 整数 | 十进制 ASCII；**拒绝 bool 与 float** |
| `bool` | JSON true/false | `1` / `0`；拒绝字符串 |
| `decimal` | string | 定点十进制；拒绝指数/NaN/Infinity/float；去多余 0，`-0`→`0` |
| `date` | `YYYY-MM-DD` | 正则+真实日历校验，零填充 |
| `timestamp` | 带时区的 ISO-8601（含 `Z`） | 必须显式时区，统一规范化为 UTC `...Z` |

三种存在状态（与载荷分离）：`present`、`null`、`missing`。

* 空字符串是 `present`（载荷为空字节），与 `null` 不冲突；
* `null` 与 `missing` 使用不同状态标签，承诺永不相撞；
* 任何有歧义的输入（NaN、指数、朴素时间戳、bool 冒充 int）一律报
  `TYPE_ENCODING_ERROR`，不做隐式强制转换。

## 3. 字段承诺（域分离 + 随机盐）

使用成熟哈希（默认 SHA-256，配置白名单 `sha256/384/512`，经
`cryptography.hazmat` 调用）。每个字段承诺的哈希输入包含：

```
H_label(field-commitment,
        u16 batch_id,        # 批次上下文，防止跨批次重放
        u64 record_index,    # 位置身份：记录下标
        u64 position,        # 位置身份：模式内字段位置（路径排序后）
        str path,            # 名称身份：规范字段路径
        str field_type,      # 类型身份
        str state,           # present/null/missing
        payload,             # present 时为规范化值
        salt)                # present 必带；null 可带；missing 无盐
```

所有部件以 `u16 长度前缀` 分帧，标签为
`audit-commit-v1|field-commitment`，部件个数也参与哈希，避免不同元组形状的
拼接碰撞。因此：

* 不同字段名同值 → 承诺不同（路径绑定）；
* 不同位置同值 → 承诺不同（位置绑定）；
* 调换两个字段 → 两个承诺的路径/位置绑定同时改变；
* 盐来自 `secrets.token_bytes`（OS CSPRNG），默认 16 字节，配置下限 16。

### 低熵字段的明确限制（重要）

加盐承诺只能在**盐保密期间**隐藏值，它不能把一个小取值空间变成秘密：

* 布尔、枚举等低熵字段一旦盐暴露，验证方可对每个候选值重算承诺完成枚举；
* **无盐**的低熵承诺则对任何持有承诺者都可直接枚举。

服务默认拒绝无盐承诺（`AUDIT_ALLOW_UNSALTED=false`），并对 `bool` 类型及
声明了 `value_space <= 2^20` 的字段在批次响应中给出 `LOW_ENTROPY_FIELD`
告警。该限制由 `tests/test_independent_crosscheck.py` 中的
`test_low_entropy_unsalted_is_enumerable_salted_is_not` 具体演示：无盐布尔
承诺两次尝试即可命中；真实（加盐）承诺用两个猜测都无法命中。

## 4. 两层 Merkle 树与批次根

* **字段树（field tree）**：叶为字段承诺，按规范位置排序。
* **记录树（record tree）**：叶为各字段树根，按记录下标排序；其根即
  **批次根**。

规则：

* 叶哈希绑定 `index` 与声明的 `leaf_count`
  （`label | u64 idx | u64 count | node`），证明不能换位置、不能针对不同
  叶数的树重放；
* 内部节点为两个子节点的标签哈希；奇数节点复制最后一个节点晋升，复制位置
  在证明中以显式 `null` 标记，验证方拒绝在该槽位提供任意“缺失兄弟”；
* 空树使用独立标签的空根标记；字段树与记录树使用不同标签（域分离）。

## 5. 披露证明的绑定内容

一次只披露一个字段。证明 JSON 包含：

* `batch_id`、`digest`、**`batch_root_hex`**；
* `claim`：`record_index`、`position`、`path`、`field_type`、`state` 以及该
  字段已发布的 `commitment_hex`（公开叶哈希）；
* `field_tree`：字段数、兄弟路径、所在记录根；
* `record_tree`：记录数、兄弟路径；
* `reveal`：仅当被披露字段 `present` 时为 `{value, salt_hex}`；`null` 时
  为 `{value: null, salt_hex}`；`missing` 时为 `null`，**不携带任何盐或值**。

**保密边界**：除被披露的这一个字段外，任何其他字段的盐与原值都不会出现在
证明、批次公开视图或审计事件中（测试用扫描器断言）。

## 6. 验证判定顺序与失败类别

验证是纯函数 `verify_proof(proof, trusted_batch_root_hex, expected_path?,
expected_record_index?)`，逐步检查并记录 `checked_steps`：

1. 结构与版本/摘要算法 → `PROOF_MALFORMED`
2. 身份字段良构、`expected_path`/`expected_record_index` 固定 →
   `PROOF_MALFORMED` / `IDENTITY_MISMATCH`
3. 下标在声明叶数范围内、计数一致 → `IDENTITY_MISMATCH`
4. 解码披露材料（非法 hex 等）→ `PROOF_MALFORMED`，类型无法编码 →
   `TYPE_ENCODING_ERROR`
5. 用值+盐+身份**重算字段承诺**，与证明声称的叶承诺常量时间比较 →
   **`COMMITMENT_MISMATCH`**（错误盐、错误值、偷换路径/位置后无法复现叶）
6. 字段 Merkle 路径 → **`MERKLE_PATH_MISMATCH`**
7. 记录 Merkle 路径 → **`MERKLE_PATH_MISMATCH`**
8. 证明内嵌批次根与调用方信任根常量时间比较 → **`ROOT_MISMATCH`**

这保证：证明自身完全自洽但来自别的批次时，仍以 `ROOT_MISMATCH` 拒绝；
密码学校验失败与“请求了错误字段”是不同类别；任何意外异常都包装为
`INTERNAL_ERROR` 的失败判定，**绝不返回成功**。

### 为什么不能替换字段身份

* `expected_path` / `expected_record_index` 在任何哈希之前先固定请求身份；
* 即使攻击者用另一字段的合法证明替换，路径/记录不匹配即
  `IDENTITY_MISMATCH`；
* 若攻击者只改 `claim` 中的路径/位置而沿用旧盐值，第 5 步重算承诺不一致
  → `COMMITMENT_MISMATCH`；
* 若攻击者偷换 `commitment_hex` 为邻居叶，第 5 步即失败；第 6 步的字段树
  路径也无法在旧身份下锚定到记录根；
* 叶哈希内含 `index/count`，记录叶内含记录下标树结构，跨位置/跨批次重放
  均失败。

## 7. 存储状态隔离

SQLite 三张表：

* `batches`：公开元数据、批次根、公开 JSON（无盐无值）；
* `batch_secrets`：私有盐/值，仅披露流程按主键查询，列表/读取接口永不访问；
* `audit_events`：只追加审计，`detail_json` 白名单内容（步骤、类别、关联
  指纹），不含原值与盐。
