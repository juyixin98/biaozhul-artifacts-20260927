# 固定本地测试链 · 头部轻客户端

一个面向**固定本地测试链**的头部轻客户端：通过**带外可信检查点（trusted checkpoint）**
引导，只接受由**当前有效委员会**以**权重阈值签名（floor(2W/3)+1）**背书、且
严格延续当前可信末端的头部；委员会轮换必须由**上一任有效委员会**授权；离线超过
**信任期**后拒绝任何对等更新并要求新的检查点。

> ⚠️ **这是一个简化的教学协议（simplified educational protocol），不是、也不兼容
> 任何现有公链客户端。** 数据均为本地合成夹具，无生产账号、无真实业务数据、无网络
> 共识。签名原语使用成熟的 [`cryptography`](https://cryptography.io)（Ed25519），
> 阈值由本协议的“委员会多签 + 权重计数”实现。

---

## 1. 目录结构与模块边界

模块之间单向依赖，数据契约（`types.py` 的 dataclass）与错误契约
（`errors.py` 的 code/category）显式定义：

| 模块 | 职责 | 不做什么 |
| --- | --- | --- |
| `src/lc/types.py` | 协议数据类型与**形状解析**（Header / Committee / Certificate / Checkpoint） | 不做任何授权判断 |
| `src/lc/encoding.py` | 确定性小端定长编码、域分离哈希（头部根、委员会承诺、签名消息） | 不碰密钥 |
| `src/lc/crypto.py` | Ed25519 原语封装、**成员资格 / 逐签名 / 权重阈值**验证 | 不读链状态、不决定是否接受 |
| `src/lc/store.py` | SQLite **索引存储**、`BEGIN IMMEDIATE` 事务、tip 快照 | 不含业务规则 |
| `src/lc/chain.py` | **链状态内核**：唯一修改可信状态处，按规则顺序校验并原子提交 | 不直接做 HTTP/JSON |
| `src/lc/replay.py` | 离线回放 JSON 束解析为类型化批次 | 不验证、不写库 |
| `src/lc/app.py` | FastAPI 服务入口，错误码 ↔ HTTP 状态映射 | 不含判定逻辑 |
| `src/lc/config.py` `clock.py` `errors.py` | 配置、可注入时钟、统一错误契约 | — |
| `scripts/oracle.py` | **独立参考预言机**（不 import 任何 `lc.*`），生成黄金夹具与期望裁决 | 被测代码不能生成答案 |
| `scripts/demo.py` | 本地端到端演示 | |
| `scripts/run_server.py` / `run_tests.sh` | 服务入口 / 测试报告脚本 | |

### 数据契约（JSON 线格式）

- 所有字节字段为 `0x` 前缀的**定宽**小写 hex（根/承诺 32B、公钥 32B、签名 64B）。
- `Header = {round, parent_root, body_root, timestamp, next_committee_commitment}`。
- `Committee = {members:[{public_key, weight}]}`，按公钥升序为规范序。
- `Certificate = {header_root, signatures:[{public_key, signature}]}`。
- 头部根 `SHA256(DOM_HEADER ‖ enc(header))`；委员会承诺
  `SHA256(DOM_COMMITTEE ‖ enc(committee))`；成员签名消息
  `DOM_CERT_SIGN(8B) ‖ header_root(32B)`。域标签不同，杜绝跨结构哈希碰撞。

## 2. 内核校验规则（顺序即优先级）

1. 形状/定宽解析 → `input_error`
2. 信任期（按批次整体评估）→ `trust_expired`
3. **父链接**：父必须是当前可信 tip；父为已知非 tip 头 = 同高度分叉（equivocation）；
   父未知 = 不可信分支 → `state_conflict`
4. **轮次严格递增**（回退 `STALE_ROUND`，同轮另一头 `CONFLICT_EQUIVOCATION`）
5. 时间戳单调、不允许超过时钟 + 30s（`HEADER_BACKDATED` / `HEADER_FUTURE`）
6. 证书 `header_root` 与头部根绑定；轮换头必须随附委员会，且其承诺必须等于
   头部声明值（`CERT_BIND_MISMATCH` / `ROTATION_*`）
7. 每个签名者必须属于**当前授权委员会**（轮换后旧委员会签名即 `SIGNER_UNKNOWN`）
8. 每条 Ed25519 签名必须有效（`CRYPTO_BAD_SIGNATURE`）
9. **去重后**签名权重 `≥ floor(2W/3)+1`（`INSUFFICIENT_WEIGHT`，附带
   `signed/required/total` 中间值）
10. 全部通过才在**一个事务**内写头/委员会并推进 tip；任一失败整体回滚

## 3. 错误语义（五类，可机读、互不混淆）

每个错误体为：

```json
{"error": {"code": "...", "category": "...", "message": "...",
           "details": {...}, "reasons": []},
 "run_id": "run-xxxx", "trace": ["...中间判断..."]}
```

| category | 含义 | HTTP | 代表 code |
| --- | --- | --- | --- |
| `input_error` | 输入形状/编码/绑定错误 | 400 | `MALFORMED_HEADER`, `CERT_BIND_MISMATCH`, `ROTATION_COMMITMENT_MISMATCH`, `HEADER_FUTURE` |
| `state_conflict` | 输入本身有效但与可信状态冲突 | 409 | `UNTRUSTED_BRANCH`, `CONFLICT_EQUIVOCATION`, `STALE_ROUND`, `NOT_INITIALIZED`, `CHECKPOINT_CONFLICT`, `ALREADY_KNOWN`, `HEADER_BACKDATED` |
| `computation_failure` | 密码学/阈值计算失败 | 422 | `SIGNER_UNKNOWN`, `CRYPTO_BAD_SIGNATURE`, `INSUFFICIENT_WEIGHT` |
| `resource_exhausted` | 资源/体量限制或存储故障 | 413 | `BATCH_TOO_LARGE`, `COMMITTEE_TOO_LARGE`, `STORAGE_FAILURE` |
| `trust_expired` | 超出信任期，需新检查点 | 410 | `TRUST_EXPIRED`（`details.needs_new_checkpoint=true`） |

**任何拒绝都保证可信 tip 字节级不变**（事务回滚 + 内核在异常后比对快照，
若发现状态被改动会抛出 `RuntimeError`——这是原子性自检，不是正常路径）。

## 4. 信任模型与边界

- 检查点是**带外断言**安装的，只允许在未初始化时安装一次；不能通过更新接口换根。
- 头部只有“延续当前 tip + 当前有效委员会达阈值”才被信任；
  **带有效证书的非延续分支也绝不移动根**（分支判定先于密码学判定）。
- 轮换头由**旧委员会**达阈值授权后，新委员会才生效；此后旧委员会签名一律
  `SIGNER_UNKNOWN`。
- tip 时间戳距时钟 `now` 满足 `age ≤ trust_period`（默认 7 天）才算 fresh。
  边界是**包含**的：`age == period` 仍接受，`age == period + 1ms` 即
  `TRUST_EXPIRED`。长时间离线后，即便对端给出连续合法链，也必须先获得
  **新的带外检查点**（本实现即使用全新数据库重新引导）；仅回放历史头不能“刷新”。

## 5. 安装与运行

需要 Python ≥ 3.10。

```bash
python3 -m venv --system-site-packages .venv        # 利用系统已装的 cryptography
.venv/bin/python -m pip install -r requirements.txt # fastapi/uvicorn/pytest/httpx

# 若环境中没有 cryptography： .venv/bin/python -m pip install cryptography

# 1) 端到端演示（合成夹具，打印每一步裁决与运行日志路径）
.venv/bin/python scripts/demo.py

# 2) 起服务
LC_DB_PATH=./data/lc.db LC_LOG_DIR=./logs .venv/bin/python scripts/run_server.py --port 8000
#    或:  .venv/bin/uvicorn lc.app:app --app-dir src --port 8000

# 3) 跑测试并把带时间戳的报告写入 logs/
./scripts/run_tests.sh
```

黄金夹具由独立预言机生成（已提交 `tests/fixtures/golden.json`；可随时重新生成，
应当字节稳定）：

```bash
.venv/bin/python scripts/oracle.py
```

### HTTP 速览

```bash
curl -s localhost:8000/health
curl -s localhost:8000/trust
# 安装检查点（仅一次）
curl -s -X POST localhost:8000/checkpoint -H 'Content-Type: application/json' \
     --data @<(python3 -c "import json;print(json.dumps(json.load(open('tests/fixtures/golden.json'))['checkpoint']))")
```

## 6. 复现步骤（对应每一条需求）

测试均断言**具体 code + category + 拒绝后 tip 快照不变**，而非“接口能调用”：

| 需求 | 复现测试 |
| --- | --- |
| 连续合法头跟进 | `test_continuous_legal_chain_advances` |
| 权重门槛（40 < 41 / 40 ≥ 34 边界） | `test_insufficient_weight_at_boundary`、`test_new_committee_underweight`、`test_just_over_threshold_after_rotation_accepted`、`test_threshold_formula` |
| 父链接 / 轮次单调 / 同轮冲突 | `test_unknown_parent_untrusted_branch`、`test_stale_round`、`test_conflicting_header_at_same_round` |
| 委员会变更由前一委员会授权；旧委员会签新头 | `test_old_committee_signs_new_header_is_unknown`、`test_rotation_commitment_mismatch_rejected`、`test_rotation_without_committee_object_rejected` |
| 拒绝从未信任分支直接更新根 | `test_conflicting_certificate_cannot_move_root`、`test_unknown_parent_untrusted_branch` |
| 长离线超信任期 → 需新检查点 | `test_one_ms_past_period_requires_new_checkpoint`、`test_exact_boundary_age_equals_period_is_fresh`、`test_long_offline_peer_chain_is_rejected`、`test_historical_replay_cannot_refreshen` |
| 冲突头 / 坏签名 / 证书错绑 | `test_conflicting_header_at_same_round`、`test_bad_signature_rejected`、`test_cert_bind_mismatch_rejected` |
| 拒绝后可信状态不变（含整批回滚） | 所有 `expect_reject` + `test_rejected_batch_rolls_back_all_headers` |
| 边界输入不“悄悄算错”（前导零 hex） | `tests/test_boundary_hex.py` |
| 资源耗尽可区分 | `test_oversized_batch_is_resource_error`、`test_oversized_committee_is_resource_error`、`test_storage_failure_mapped_to_resource` |
| HTTP 状态/类别映射 | `tests/test_api.py` |

运行单例复现：

```bash
.venv/bin/python -m pytest tests/test_kernel_golden.py -k "weight or rotation or branch" -q
.venv/bin/python -m pytest tests/test_trust_boundary.py -q
```

## 7. 可重放日志

`RunRecorder` 为每个运行分配 `run_id`，并在设置 `LC_LOG_DIR`（或演示脚本）时
把每个决策镜像成 JSONL：包含 `run_id / seq / event / tip_before / tip_after /
cert 权重中间值 / trace（逐步判断理由）/ failure_code / state_unchanged`。
仅凭一个失败运行的 JSONL 即可重建输入位置、关键中间状态和判定理由。

## 8. 独立性说明（为什么答案不是“被测代码自己生成的”）

`scripts/oracle.py` **不 import `lc.` 任何模块**：它用独立的一份编码/域标签/
打包代码、直接调用 `cryptography` 的 Ed25519 原语签名，并从协议第一性原理给出
每个夹具的期望根、承诺、阈值数值与 accept/reject + category + code。测试要求
被测核心在这些外部黄金向量上**复现预言机结论**（含根与承诺的逐字节一致，
见 `test_oracle_and_core_agree_on_*`），反向不成立。
