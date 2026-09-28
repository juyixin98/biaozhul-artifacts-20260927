# 门槛分片与恢复服务（Shamir Secret Sharing over GF(p)）

一个本地、自包含的 **(t, n) 门槛秘密分享与恢复服务**：把秘密切成 `n` 个份额，
任意 `t` 个可恢复，少于 `t` 个在数学上不可恢复；并在原始 Shamir 之上叠加
**独立的完整性校验**，给出可审计、可分类的接受/拒绝结论。

- **栈**：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）·
  [`cryptography`](https://cryptography.io) · 成熟有限域库
  [`galois`](https://github.com/mhostetter/galois)
- **数据**：全部为本地合成夹具（`tests/conftest.py`、`scripts/smoke.py`），
  无任何生产账号或真实业务数据。
- **有限域**：GF(p)，`p = 2^256 − 2^32 − 977`（secp256k1 的域素数），
  本原元 `g = 3`。每 31 字节秘密编码为一个域元素，支持任意长度秘密。

> 这是教学/评估用途的本地服务。它演示的是**协议与安全内核**，不是一套带
> 身份认证、密钥管理、高可用、权限模型的生产级秘密管理平台。

---

## 1. 模块职责（不是单文件脚本，也不是空接口工程）

```
app/
  config.py            进程配置（环境变量），不含任何安全决策
  parsing.py           规则/证据解析：份额结构、集合身份/阈值/字段绑定、
                       重复横坐标处理策略
  core/
    field.py           成熟有限域后端（galois 封装）+ 固定字段参数
    envelope.py        份额信封、规范编码、独立 HMAC 完整性、份额指纹
    shamir.py          Shamir 分割/恢复（分块、随机多项式、Lagrange、状态判定）
    kernel.py          安全内核：编排 解析→完整性→有限域→状态，并写审计
  state.py             状态隔离：SQLite 持久化，集合与份额外键隔离
  audit.py             审计接口：仅记录份额指纹的事件日志
  api.py / main.py     薄薄的 FastAPI 接线（这里不做任何安全判断）
tests/
  conftest.py          本地合成夹具
  oracle.py            独立参考实现（教科书 GF(p)，仅用内置 pow，不 import
                       galois，也不 import 被测核心 app.core）
  test_field_math.py   galois 核心 × 独立 oracle × 字面量向量 交叉验证
  test_shamir_core.py  分块编解码、HMAC 完整性、指纹
  test_kernel_recovery.py  枚举子集/不足阈值/混集合/重复份额/坏校验/参数不兼容
  test_state_audit.py  状态隔离、指纹-only 审计、诊断可关联性
  test_api.py          FastAPI TestClient 端到端
scripts/
  run_local.sh         本地启动
  example_requests.sh  示例 curl 请求
  smoke.py             不依赖 pytest/HTTP 的内核冒烟脚本
```

四个关注点**真有职责**：

- **规则/证据解析（parsing）**：决定一个份额"凭什么算入恢复集"，可在不引入
  galois/SQLite 的情况下单测。
- **安全内核（core）**：有限域运算、Shamir 数学、独立完整性、恢复编排。
- **状态隔离（state）**：所有份额查询都带 `collection_id` 与外键作用域，
  一个集合的份额永远无法满足另一个集合的恢复。
- **审计接口（audit）**：事件落 SQLite（`audit` 表）并可镜像到 stderr。

---

## 2. 份额绑定了什么

每个份额（`ShareEnvelope`）都以**规范字节编码**携带并接受 HMAC 保护：

| 字段 | 含义 |
|---|---|
| `collection_id` | 集合身份 |
| `threshold` / `total` | 该份额承诺的门槛参数 |
| `x` | 横坐标（正整数） |
| `ys` | 每个秘密块一个域元素 |
| `field` | 字段参数：版本、素数（十进制字符串）、素数位宽、分块大小 |
| `mac` | 对上述所有字段的 HMAC-SHA256 |

恢复时对每个份额依次执行：结构解析 → 字段参数一致 → 集合身份一致 →
`(t,n)` 绑定一致 → 块数一致 → **HMAC 校验通过** → 去重后才计数。

**重复横坐标不重复计数**：相同 `(x, ys)` 的重复份额被丢弃并记为
`duplicate_x_ignored`；同一 `x` 携带**不同** `ys`（即便两者 MAC 都合法）
记为 `duplicate_x_conflicting_value`，绝不静默二选一。门槛只统计
**去重后**的合法份额数。

---

## 3. 不足阈值明确拒绝；恢复结果是"分类"，不是布尔值

恢复返回明确的状态类别（HTTP 层放在 `status` 字段，`accepted` 布尔只是便利）：

| 状态 | 含义 |
|---|---|
| `recovered_verified` | 用了 `t` 个份额，且**额外份额全部落在同一条多项式上**，交叉验证通过 |
| `recovered_unverifiable` | 恰好 `t` 个份额：数学上可恢复，但没有冗余可校验 |
| `rejected_insufficient_threshold` | 去重后合法份额 `< t`（含坏份额被剔除后跌破门槛） |
| `rejected_inconsistent_shares` | MAC 都合法，但份额在底层多项式上不一致 |

单份额被拒的原因类别：`malformed_share` / `wrong_collection` /
`field_parameter_incompatible` / `threshold_total_mismatch` /
`bad_integrity_mac` / `duplicate_x_ignored` / `duplicate_x_conflicting_value`
/ `block_count_mismatch`。

测试断言的是**具体值与具体失败类别**，而不是"接口能调用"。

---

## 4. 独立完整性校验与信任边界（重点）

**原始 Shamir 不防篡改**：任意一个份额被改，Lagrange 通常只会静默还原出垃圾，
不报错。因此本服务加入一个**与有限域数学相互独立**的完整性层：

- 创建集合时生成一把 256 位随机 **HMAC-SHA256 密钥（每集合一把）**；
- 每个份额带对其全部绑定字段的 MAC；
- 恢复在做任何域运算**之前**先验 MAC（常量时间比较）。

**信任边界（务必理解）**：

1. HMAC 是"**服务端持钥**"模型。它能检测：传输/落库损坏、以及任何**不持有
   该集合密钥**的人对份额的篡改（→ `bad_integrity_mac`）。
2. 它**不提供不可抵赖/归因**。合法持钥方能签发一个 MAC 合法、但内容错误的份额；
   服务端无法仅凭自己判定"谁是恶意方"。
3. 若服务端密钥本身泄露，伪造份额也会通过 MAC。
4. 因此本服务**刻意不声称**"恢复失败 = 定位了全部恶意参与者"。
   - 有冗余（>t）时，能检测到集合不一致并给出 `mismatched_xs`，但这只是
     "这些点不在基线上"，**不证明这些点的持有者就是攻击者**（调换基线子集
     可能改变被点名的点）。
   - 恰好 t 个 MAC 合法份额时，没有任何冗余可供交叉检查，服务端要么给出
     `recovered_unverifiable` 的值，要么在解码失败时给 `inconsistent`，
     两种情况下都无法归因。该行为由
     `test_failure_does_not_identify_all_malicious_parties` 固化。

要在对抗性的真实参与方之间获得可识别作弊/可抵赖的属性，需要的是 **Verifiable
Secret Sharing（如 Feldman/Pedersen 承诺）**、数字签名份额，或区块链/多方一致性——
那超出本服务范围，属明确的后续方向（见 §9）。

> `Kernel.issue_share_with_value(...)` 仅用于测试/演示"持钥者作恶"，
> **不通过 HTTP 暴露**。

---

## 5. 审计与脱敏

- 审计事件只记录：`request_id`、`collection_id`、`action`、`verdict`、
  解释性 `detail`、以及份额**指纹列表**。
- 指纹 = `"sha256:" + SHA256(份额规范字节 || MAC)` 前 16 个十六进制字符。
  它是单向摘要，**不含秘密、不含份额 `ys`、不含 MAC 密钥**。
- 诊断带记录/请求标识与关键状态（接受的去重横坐标、用于恢复的横坐标、
  额外/不匹配横坐标、各类拒绝原因），解释"为何接受/拒绝/无法判定"。
- 测试 `test_audit_*` 会断言秘密、所有 `ys`、MAC 都不出现在任何审计行中。

---

## 6. 本地启动

需要 Python 3.12（其它 3.10+ 大概率可行但未锁定验证）。

```bash
make install        # 建 .venv 并安装锁定依赖（等价见下）
# 或手动：
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make test           # 运行全部测试
make smoke          # 不依赖 HTTP/pytest 的内核冒烟
make run            # 启动 http://127.0.0.1:8000（文档 /docs）
```

环境变量：`SHAMIR_DB_PATH`（默认 `data/shamir.db`）、
`SHAMIR_AUDIT_STDERR`（默认 `1`）、`SHAMIR_MAX_TOTAL_SHARES`（默认 64）。

### 依赖锁定

- `requirements.in`：人工维护的直接依赖（宽松）。
- `requirements.txt`：`pip freeze` 生成的**精确锁定**（含传递依赖、用于复现）。

---

## 7. 示例请求

启动后另开终端：

```bash
BASE=http://127.0.0.1:8000 bash scripts/example_requests.sh
```

手动调用：

```bash
# 1) 创建 3-of-5（秘密以 hex 传入）
SECRET=$(python3 -c "print('the quick brown fox'.encode().hex())")
curl -s -X POST http://127.0.0.1:8000/collections \
  -H 'Content-Type: application/json' \
  -d "{\"secret_hex\":\"$SECRET\",\"threshold\":3,\"total\":5}"

# 2) 用第 1,3,5 个份额恢复（任意 3 个即可）
curl -s -X POST http://127.0.0.1:8000/collections/<CID>/recover \
  -H 'Content-Type: application/json' \
  -d '{"shares":[ ...份额1, 份额3, 份额5... ]}'

# 3) 审计（按 request_id 或集合）
curl -s "http://127.0.0.1:8000/audit?request_id=<REQ_ID>"
```

成功恢复（恰好阈值）示例片段：

```json
{
  "status": "recovered_unverifiable",
  "accepted": true,
  "distinct_xs": [1, 3, 5],
  "used_xs": [1, 3, 5],
  "rejected_shares": [],
  "secret_hex": "74686520717569636b2062726f776e20666f78"
}
```

不足阈值：

```json
{ "status": "rejected_insufficient_threshold", "accepted": false,
  "distinct_xs": [1, 2], "rejected_shares": [],
  "diagnostic": "2 distinct admissible < threshold" }
```

篡改份额：`rejected_shares[].reason = "bad_integrity_mac"`。

---

## 8. 测试如何"参考答案不由被测核心自己生成"

- 生产核心的所有域运算都走成熟库 **galois**。
- 测试参考实现 `tests/oracle.py` 是**独立教科书实现**：用 Python 内置
  `pow(a, p-2, p)` 求逆、手写 Horner 与 Lagrange；它**既不 import galois，
  也不 import `app.core`**。
- 三类证据相互印证：
  1. **字面量已知答案**：如 `f(x)=7+4x ⇒ f(0)=7`、含模回绕的二次多项式。
  2. **galois ↔ oracle 交叉**：对多个 `(t,n)` 与 25+ 组随机多项式，逐点
     求值得相同 `y`，且**枚举每个门槛子集**都还原同一常数。
  3. **应用层断言**：恢复出的字节等于夹具里的字面量秘密
     （`b"the quick brown fox"`、67 字节多块秘密、空秘密）。

### 已保留的验证过程（对应任务要求）

- **枚举小配置的全部合法子集恢复相同秘密**：
  `test_every_threshold_subset_recovers_same_secret`（3-of-5 的全部
  C(5,3)=10 个子集）、`test_every_larger_than_threshold_subset_is_verified`
  （4、5 个份额，验证 `recovered_verified`）、多块秘密的全部 2-of-4 子集。
- **不足阈值**：`test_below_threshold_rejected`，以及 `<t` 子集在数学层
  就还原不出常数（`test_less_than_threshold_does_not_recover`）。
- **混集合**：`test_share_from_other_collection_rejected`、
  `test_recovery_is_scoped_to_collection`。
- **重复份额**：相同重复不计数（`test_duplicate_shares_do_not_inflate_count`），
  同 x 不同值冲突（`test_conflicting_duplicate_x_is_flagged_not_preferred`）。
- **坏校验（篡改）**：`test_tampered_share_fails_integrity`、
  有冗余时的一致性检测 `test_tampered_above_threshold_is_detected_via_consistency`。
- **参数不兼容**：异素数字段 `test_foreign_field_parameters_rejected`、
  门槛绑定 `test_threshold_binding_mismatch_rejected`、
  块数 `test_block_count_mismatch_rejected`。
- **失败 ≠ 定位全部恶意者**：
  `test_failure_does_not_identify_all_malicious_parties`。

运行：

```bash
source .venv/bin/activate
python -m pytest                       # 全量
python -m pytest -k integrity -v       # 只看完整性相关
```

---

## 9. 支持范围与关键取舍

**支持**
- 任意长度字节秘密（按 31 字节分块，逐块独立随机多项式）；空秘密也支持。
- 任意 `1 ≤ t ≤ n`（默认上限 `n ≤ 64`，可配置）。
- 份额可经 JSON 迁移（`ys`/`prime` 用十进制字符串，避开大整数 JSON 差异）。
- 本地 SQLite 持久化、指纹审计、按 request/collection 关联诊断。

**关键取舍**
- **字段固定为 secp256k1 素数**：让"字段参数"成为可比较、可绑定的常量，
  异字段份额直接拒绝。31 字节/块的分块方式保证编码值严格 `< p`。
- **galois 建域加速**：galois 对大素数默认会重新做素性/本原元证明并搜索
  本原元（实测约 110 秒）。我们固定素数与已知本原元 `3` 并传
  `verify=False`，使建域降至亚毫秒；常量正确性由
  `test_pinned_field_constants_are_actually_valid` 与全部交叉测试守护。
- **HMAC 而非 VSS**：选择"独立于数学的对称完整性"，因为它简单、快速、依赖
  成熟的 `cryptography`，且能清晰展示信任边界。代价是不防持钥者、不具
  不可抵赖性（见 §4）。
- **失败即拒绝，不做"纠错式"静默恢复**：检测到不一致就拒绝，不自动挑一个
  能成功的子集，避免悄悄偏向某方。
- **恢复接口接收客户端提交的份额**（也提供从本地库取份额的便捷路径
  `GET /collections/{cid}`），以贴近"外部参与者上交份额"的模型；本演示不
  做参与者身份认证。
- **无真实随机性之外的随机源问题**：分割系数用 `os.urandom`（CSPRNG）。

**明确不做 / 后续方向**
- 不做 Feldman/Pedersen 可验证秘密分享（VSS）、份额签名、阈值签名。
- 不做密钥轮换、HSM/KMS 托管、多副本高可用、参与者认证授权。
- 不做恒定时间的整个应用层（MAC 比较用常量时间；域运算不面向计时攻击建模）。

---

## 10. 验证结果（实际运行记录）

- 依赖安装：`pip install -r requirements.in`（成功，锁定见 `requirements.txt`）。
- 单元/集成测试：`python -m pytest` → **55 passed**（含 1 个第三方弃用告警，
  来自 starlette TestClient 对 httpx 的提示，不影响结果）。
- 内核冒烟：`python scripts/smoke.py` → **ALL SMOKE CHECKS PASSED**。
- 真实服务：`uvicorn app.main:app` 起服务后用 `scripts/example_requests.sh`
  与在线 curl 验证：合法 3-of-5 子集恢复正确、不足阈值拒绝、篡改→
  `bad_integrity_mac`、异素数→`field_parameter_incompatible`、混集合→
  `wrong_collection`。

> 开发过程中曾出现过的真实失败与处置（已修复，保留以说明"跑过且有失败"）：
> galois 编译模式参数取值（`python` 应为 `python-calculate`）、
> 大素数建域 ~110s（改为固定本原元 + `verify=False`）、相对导入路径、
> 多块夹具字节数算错、构造恶意份额时把字符串 `ys` 当整数、以及测试偏移
> 落在块内零填充区导致内容字节未变（改为作用于真实内容字节的权重）。
> 截至当前，**无未通过或未执行的测试**；如运行环境无法联网安装依赖，则
> 测试无法执行——这是唯一已知的"未执行"前置条件。
