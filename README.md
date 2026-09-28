# 门槛分片与恢复服务（Threshold Secret Sharing Service）

基于 **Shamir 秘密分享（GF(2⁸) 逐字节多项式）+ 独立 HMAC 完整性层** 的本地演示服务。
技术栈：Python 3.12 · FastAPI · SQLite · `cryptography` · `pyfinite`。
所有参与者与秘密均为 `fixtures/` 下的本地合成数据，无生产账号、无真实业务数据。

## 模块职责（不是单文件脚本，也不是空接口工程）

| 模块 | 真实职责 |
| --- | --- |
| `threshold_service/gf.py` | 有限域参数与运算（GF(2⁸)/AES 多项式 0x11B），委托成熟库 pyfinite |
| `threshold_service/shamir.py` | 分片与拉格朗日插值（只做数学，不做信任判断） |
| `threshold_service/integrity.py` | **独立完整性层**：份额 HMAC 标签 + 秘密承诺 |
| `threshold_service/models.py` | 规则/证据的数据表示：份额信封、规范序列化、指纹 |
| `threshold_service/policy.py` | **证据解析与安全策略**：逐条分类、集合一致性、去重、门限判定 |
| `threshold_service/kernel.py` | **安全内核**：编排分片/恢复、承诺核对、尽力而为归因诊断 |
| `threshold_service/repository.py` | **状态隔离**：SQLite 持久化，份额静态加密（Fernet），按 set_id 隔离 |
| `threshold_service/audit.py` | **审计接口**：JSONL 记录、指纹白名单去敏、按请求/集合检索 |
| `threshold_service/app.py` | 薄 HTTP 适配层（FastAPI），安全决策全部在内核/策略层 |
| `tests/oracle_reference.py` | **独立参考实现**（手写 GF 运算/插值 + LCG），答案不由被测核心生成 |
| `fixtures/` | 合成参与者、合成测试秘密 |
| `config/` | 环境配置样例（与代码分离） |

## 安全模型与信任边界（务必先读）

**原始 Shamir 只提供保密性，不防篡改。** 任意一份恶意份额都会让拉格朗日插值
得到一个看似正常但完全错误的常数项，且数学上没有任何告警。本服务在其外
**独立**增加两层完整性机制（均为 HMAC-SHA256，恒定时间比较）：

1. **份额标签**：信封创建时对「版本+集合身份+横坐标+份额值+门限+字段参数」
   的规范 JSON 计算 HMAC。恢复时逐条重算比对，拒绝篡改、跨集合搬运和
   非本服务签发的份额。
2. **秘密承诺**：分片时对原秘密（带集合身份域分隔）计算 HMAC 并保存；
   重组出候选秘密后核对，作为「恢复是否真的成功」的判据。

信任边界与**不**提供的性质：

- HMAC 是**对称**认证：它证明「份额由持有主密钥的一方签发且未改」，
  **不**提供持份者之间的不可否认性，也不能防止服务端/分发者自身作恶。
- 持标签的坏份额（例如主密钥泄露后伪造，或内鬼用合法密钥重签）能通过逐条
  校验，但会让插值结果与承诺不符。此时服务返回 **INDETERMINATE（无法判定）**，
  绝不输出插值出的错误秘密。
- **恢复失败 ≠ 定位全部恶意参与者。** 内核仅在份额数较少时做有预算上限的
  门限子集枚举（`SUBSET_ENUM_BUDGET=256`），给出「哪些子集与承诺自洽」的
  排查线索；多个共谋者、组合空间超限都可能导致归因不完整。
- 要抵抗分发者/服务端作恶，需要 Feldman/Pedersen 等可验证秘密分享（VSS）
  或门限签名——**超出本版本支持范围**。
- 本地演示采用「服务端代管份额」模式以便端到端演练，份额在 SQLite 中以
  Fernet（AES-128-CBC+HMAC）静态加密。真实部署中份额应由各参与者自持，
  永不集中存储。

其他设计约束：

- 份额信封**绑定集合身份、门限和字段参数**，任何一项被改即标签失效。
- **重复横坐标不重复计数**：完全相同的份额重复提交只计一次（指纹去重）；
  同一横坐标出现不同内容则冲突双方都排除。
- 门限按「去重后的、逐条通过认证的不同横坐标数」计算；不足门限硬拒绝。
- 审计与日志**只含份额指纹**（SHA-256 截断），写入前经白名单去敏，
  秘密、份额 y、标签、主密钥均不落日志。

### 失败类别

| 类别 | 含义 | HTTP |
| --- | --- | --- |
| `MALFORMED_EVIDENCE` | 信封无法解析（坏 JSON/缺字段/类型错/base64 错） | 422 |
| `MIXED_SET` | 一次提交引用多个集合身份（硬拒绝） | 422 |
| `UNKNOWN_SET` | 集合在服务端无记录 | 404 |
| `FIELD_INCOMPATIBLE` | 字段参数与集合记录不符（逐条排除） | 422* |
| `THRESHOLD_MISMATCH` | 门限与集合记录不符（逐条排除） | 422* |
| `BAD_TAG` | 完整性标签校验失败（逐条排除） | 422* |
| `BAD_LENGTH` | 份额长度与秘密长度不符（逐条排除） | 422* |
| `DUPLICATE_X_CONFLICT` | 同 x 不同份额，双方排除 | （计入门限） |
| `BELOW_THRESHOLD` | 可用横坐标不足门限（硬拒绝） | 403 |
| `COMMIT_MISMATCH` | 插值候选未通过承诺 → `INDETERMINATE` | 200（三态响应） |

\* 逐条排除类问题本身仍可能因其他份额凑够门限而成功；最终硬拒绝状态码
由整体结果决定，每条证据的指纹在 `diagnostics` 中可见。

## 支持范围与关键取舍

- 字段固定为 **GF(2⁸)/0x11B（AES 多项式）**：份额值天然是字节串；
  单集合最多 255 份（x∈[1,255]，0 预留给秘密截距）。门限范围 2..255。
- 秘密长度任意（≥1 字节），按字节独立构造多项式。
- 不做份额刷新（share refresh/proactive security）、不做 VSS、不做异步工作流。
- 主密钥：非 dev 环境必须由 `TSS_MASTER_KEY`（32 字节 hex）注入；
  dev 环境使用固定盐 PBKDF2 派生的演示密钥，**仅限本地**。

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 或 requirements.in
TSS_ENV=dev TSS_DATA_DIR=./data ./scripts/run_dev.sh
# http://127.0.0.1:8000/docs 为 Swagger UI
```

## 示例请求

创建 2-of-3 集合（秘密 base64）：

```bash
curl -s -X POST 127.0.0.1:8000/sets -H 'content-type: application/json' -d '{
  "secret_b64": "c2VjcmV0",
  "threshold": 2,
  "share_count": 3,
  "labels": {"1": "alpha-local", "2": "bravo-local"}
}'
```

用任意 2 份恢复（`shares` 原样回传信封对象）：

```bash
curl -s -X POST 127.0.0.1:8000/recover -H 'content-type: application/json' -d '{
  "shares": [ {创建响应里的第 1 份信封}, {第 2 份信封} ]
}'
# outcome=ACCEPTED 时 secret_b64 即原秘密；不足门限返回 403 + BELOW_THRESHOLD
```

审计检索（只含指纹与判定理由）：

```bash
curl -s "127.0.0.1:8000/audit?set_id=<set_id>"
curl -s "127.0.0.1:8000/audit?request_id=req_xxxx"
```

一键端到端冒烟（创建→恢复→不足门限→混集合→审计）：

```bash
BASE=http://127.0.0.1:8000 ./scripts/smoke.sh
```

## 测试

```bash
.venv/bin/python -m pytest
```

- 58 个用例，断言**具体结果与失败类别**，而非「接口能调用」。
- `tests/test_shamir.py` 枚举小配置（2-of-3 / 3-of-5 / 4-of-4）下**所有**
  门限子集恢复同一秘密，并验证门限以下无法恢复。
- 混集合、重复份额（同指纹重复 / 同 x 冲突）、坏标签、坏长度、
  参数不兼容、未知集合、畸形证据各有独立用例。
- **参考答案独立性**：`tests/oracle_reference.py` 与生产代码零复用，
  GF 乘法用教科书 xtime 手写实现并对照 FIPS-197 已知答案自校验；
  测试双向交叉（预言机造份额→库恢复；库造份额→预言机恢复）。
- INDETERMINATE 用例证明：标签合法但内容被改的份额导致承诺失配时，
  服务拒绝给出秘密，且响应文本显式声明这不构成对全部恶意方的归因。

## 目录

```
config/             环境配置样例
fixtures/           合成参与者与测试秘密
scripts/            run_dev.sh / smoke.sh
tests/              pytest 套件 + 独立预言机
threshold_service/  服务本体（见上表）
docs/TEST_REPORT.md 最近一次测试运行记录（含未执行项说明）
```
