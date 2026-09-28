# 不可变表快照 · 开发分支与主分支三方合并后端

实现"共同祖先（base）/ 开发分支头（ours）/ 主分支头（theirs）"三方合并后端。
技术栈：**Python 3.12 · FastAPI · PyArrow（Parquet 快照）· SQLite（元数据事务）**，
全部使用本地合成数据，无外部账号依赖。

## 这是什么

对一张有主键的不可变表，给定三个快照：

```
                 base (共同祖先)
                /              \
   ours=develop 头              theirs=main 头
                \              /
                 merged snapshot（两条父引用）
```

系统以**主键识别记录身份、逐字段比较值**（不是按文件名或行号），把每条记录分入：

| 三方情形 | 分类 | 处理 |
| --- | --- | --- |
| 两边都没改 | `unchanged` | 自动 |
| 仅一边修改 | `ours_modified` / `theirs_modified` | 自动 |
| 仅一边新增 | `ours_added` / `theirs_added` | 自动 |
| 两边新增相同行 | `add_add_identical` | 自动 |
| 两边新增同键不同值 | `add_add_conflict` | **冲突** |
| 两边改**不同字段** | `field_merge` | 字段级自动合并 |
| 两边把**同一字段改成不同值** | `field_value_conflict` | **冲突** |
| 一边删除、另一边修改 | `delete_modify_conflict` | **冲突（明确保留，不静默胜出）** |
| 一边删除、另一边未动 | `ours_deleted` / `theirs_deleted` | 自动删除 |
| 两边都删 | `both_deleted` | 自动删除 |

关键约束：

- **共同祖先**区分独立变化与冲突（未显式给定时自动从血缘 DAG 求最低共同祖先 LCA；
  LCA 不唯一时报错要求显式指定，绝不静默猜测）。
- **无冲突分区自动合并；冲突必须显式解决**，且解决方案**绑定开启合并时的三方快照身份**。
- **提交后保留两条父引用**（position 0=开发头，1=主头）。
- 快照只增不改，内容寻址（schema+规范化行的 SHA-256）；分支只能条件前进，
  **禁止回退、禁止用重新导入覆盖分支历史**。
- 合并提交在单个 SQLite `BEGIN IMMEDIATE` 事务中完成（新快照 + 父边 + 分支指针 + run 落库）。

## 工程分层

```
src/merge3/
├── config.py                 # 独立配置层（config/config.yaml + MERGE3_CONFIG）
├── errors.py                 # 稳定 error.code 与 HTTP 状态；未知异常不伪装成功
├── domain/models.py           # 领域模型（纯数据）
├── adapters/arrow_format.py   # 格式适配：Schema 校验、规范化、Parquet、内容寻址
├── kernel/
│   ├── diff3.py               # 执行内核：逐键三方分类/字段级合并/冲突判定（纯函数）
│   └── lineage.py             # 血缘 DAG：LCA、祖先可达
├── storage/
│   ├── sqlite_store.py        # 元数据事务（不可变、条件前进、原子提交）
│   └── run_log.py             # JSONL 运行日志（运行身份/版本/步骤/判定/失败）
├── service/merge_service.py   # 用例编排
└── api/                       # FastAPI 验证接口
tests/
├── reference_oracle.py        # ★ 独立参考实现（不 import 被测内核）
├── unit/                      # 内核/适配/事务/服务单元测试
└── integration/               # HTTP 端到端 + 运行日志测试
sample_data/orders_snapshot.json  # 样例三方夹具与期望结论
scripts/run_demo.py            # 端到端演示（断言样例期望）
scripts/serve.sh               # 启动脚本
config/config.yaml             # 启动/存储配置
```

这不是单文件实现，也没有"固定返回值"：内核是被独立参考实现和枚举状态空间交叉验证的。

## 首次使用

需要 Python 3.10+（开发于 3.12）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 1) 跑测试（真实命令）

```bash
.venv/bin/python -m pytest
```

当前结论（本仓库实际执行）：

```
54 passed in 1.69s
```

### 2) 端到端演示（自动构造三方、分类、解决、提交并核对期望）

```bash
.venv/bin/python scripts/run_demo.py
```

输出包含每个记录键的分类、判定依据、冲突解决、两条父引用、最终行集与 `RESULT: PASS`。
运行日志写入 `demo_data/data/logs/run-*.jsonl`。

### 3) 启动 HTTP 服务

```bash
bash scripts/serve.sh          # 默认 127.0.0.1:8000，配置见 config/config.yaml
```

## API 速览（均返回 JSON；错误为 `{"error":{"code","message"}}`）

```
POST /api/v1/tables                                   # 注册表（主键+字段类型）
POST /api/v1/tables/{table}/snapshots                 # 写不可变快照（内容寻址）
GET  /api/v1/tables/{table}/snapshots/{id}/rows       # 读快照行
POST /api/v1/tables/{table}/branches/{name}            # 建分支指向快照
POST /api/v1/tables/{table}/branches/{name}/commits    # 线性开发提交（条件前进）
POST /api/v1/merges                                    # 开启三方合并（可传 base_snapshot_id）
GET  /api/v1/merges/{run_id}                           # 计划/冲突/计数/字段来源
POST /api/v1/merges/{run_id}/resolve                   # 解决一个冲突（须回传 bound_snapshots）
POST /api/v1/merges/{run_id}/commit                    # 提交（有未决冲突 -> 409）
POST /api/v1/merges/{run_id}/abandon
GET  /api/v1/tables/{table}/lineage                    # 节点/边/分支指针
```

### curl 最小流程

```bash
B=http://127.0.0.1:8000
curl -s -X POST $B/api/v1/tables -H 'Content-Type: application/json' -d '{
  "name":"t","primary_key":["id"],
  "fields":[{"name":"id","type":"int64","nullable":false},{"name":"v","type":"string"}]}'
# ...写 base 快照、建 main/develop、各自 commits（见 scripts/run_demo.py 的 HTTP 等价物）
curl -s -X POST $B/api/v1/merges -H 'Content-Type: application/json' -d '{"table":"t"}'
# 响应含 counts/conflicts/entries 与 bound_snapshots
curl -s -X POST $B/api/v1/merges/$RUN/resolve -H 'Content-Type: application/json' -d '{
  "key":[1],"kind":"value","custom_row":{"v":"x"},"bound_snapshots":[...]}'
curl -s -X POST $B/api/v1/merges/$RUN/commit -H 'Content-Type: application/json' -d '{}'
```

冲突解决 `kind`：`ours` / `theirs`（删除侧即删除）、`value`（显式行；
取值冲突只允许覆盖冲突字段，其余字段已自动合并）、`delete`、`keep`（仅删除/修改冲突，
保留未删除侧版本）。

### 失败类别（测试断言具体 code，而非"接口能调用"）

`not_found(404)` · `validation_error(422)` · `conflict(409)` ·
`unresolved_conflicts(409)` · `resolution_binding_mismatch(409)` ·
`merge_already_committed(409)` · `resolution_rejected(422)` ·
`internal_error(500)`（未知异常不统一返回成功）。

## 可核验性

- **合并行集**：单元测试与集成测试断言具体最终行、字段来源（base/ours/theirs/agreed/
  resolution/conflict）与删除集合；演示脚本对样例期望逐键核对。
- **血缘**：断言合并节点恰有两条父边且祖先（base 与两个头）仍可达，旧头不被覆盖。
- **独立参考答案**：`tests/reference_oracle.py` 用朴素算法独立实现；内核在
  5×5×5=125 种三方存在性/取值枚举上与其结论一致（见 `test_kernel_matches_independent_oracle_across_enumerated_states`）。
- **日志**：每行带 `run_id`、`service_version`、输入三方快照 ID、阶段、逐键判定依据；
  失败以 `status=failed` + `error.code` 记录，有专门测试确保失败不会被记成成功。

## 数据落点

默认根目录 `./data`（配置可改）：

```
data/metadata.db            # SQLite 元数据（WAL）
data/snapshots/snap_*.parquet  # 不可变快照文件（内容寻址）
data/logs/run-*.jsonl       # 每次合并运行的结构化日志
```
