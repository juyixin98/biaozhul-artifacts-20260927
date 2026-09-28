# Unicode 词条压缩 Trie 补全后端

基于 **Python + FastAPI + SQLite** 的前缀补全服务：

- 规范化版本固定（NFKC + casefold + 空白折叠），**显示原文原样保留**；
- 内存索引为**压缩 Trie（Patricia trie）**，每节点维护子树最大词频 `subtree_max` 作为可靠上界；
- top-k 用 best-first 分支限界 + 大小为 k 的堆，**同分按稳定规范键排序**，结果精确；
- 热词增/删/改后自底向上重算上界，**不会因过期上界错误剪枝**；
- SQLite 持久化 + 在线备份式**持久快照**；查询只走相关子树，**不全量扫描再排序**。

所有数据均为本地合成夹具，无外部服务、无生产账号。

## 目录结构

```
app/
  normalizer.py     文本规范化（版本固定为 norm-1.0.0）
  trie.py           压缩 Trie、子树上界、精确 top-k 分支限界、完整性校验
  oracle.py         独立暴力参照实现（全量筛选+排序，测试标准答案，不复用 trie 代码）
  store.py          SQLite 存储、修订号、持久快照（在线备份 API）
  engine.py         编排：校验 → 落盘事务 → 内存索引；诊断对拍；快照恢复重建
  main.py           FastAPI 路由、统一错误处理、request_id
  schemas.py        Pydantic 请求/响应模型
  config.py         环境变量配置层
  logging_setup.py  JSONL 结构化日志
  errors.py         错误码/错误类别
tests/              pytest 独立测试层（含 fixtures/ 合成夹具）
scripts/demo.py     本地端到端演示（临时目录起真实 HTTP 服务）
requirements.txt
```

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 跑测试（实际执行；journal 写到 logs/tests-<run_id>.jsonl）
TRIE_TEST_RUN_ID=$(date +%Y%m%dT%H%M%S) python -m pytest

# 本地演示（自动选端口，临时数据目录）
python scripts/demo.py

# 启动服务（默认数据在 ./data，日志在 ./logs）
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或：python -m uvicorn app.main:app --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 版本、修订号、词条/节点数 |
| GET | `/versions` | `normalizer_version` / `schema_version` / `revision` |
| PUT | `/entries/{id}` | upsert 单条，body `{id, surface, score}`（路径 id 与 body 需一致） |
| POST | `/entries/bulk` | 批量 upsert，整体校验、原子写入 |
| POST | `/entries/{id}/score` | 直接设置词频（非负整数） |
| POST | `/entries/{id}/adjust` | 词频增减 `{delta}`，结果不得为负 |
| DELETE | `/entries/{id}` | 删除 |
| GET | `/complete?prefix=&k=&trace=` | 精确 top-k；`trace=true` 返回每步计算与剪枝依据 |
| POST | `/diagnostics/verify?deep=true` | 结构/上界校验；deep 时与独立 oracle 对拍并核算每条剪枝上界 |
| POST | `/snapshots` | `{name, note}` 创建持久快照（独立 .db 文件） |
| GET | `/snapshots` | 快照清单 |
| POST | `/snapshots/{name}/restore` | 恢复快照并重建内存索引（恢复后立即自检） |

### 示例

```bash
curl -s -X PUT localhost:8000/entries/1 -H 'content-type: application/json' \
  -d '{"id":"1","surface":"multiprocessing","score":95}'
curl -s 'localhost:8000/complete?prefix=multi&k=3&trace=true' | python -m json.tool
curl -s -X POST 'localhost:8000/diagnostics/verify?deep=true' | python -m json.tool
```

`/complete` 的 `trace` 中每个剪枝步骤都给出：

- `subtree_prefix`：被整棵剪掉的子树；
- `upper_bound`：该子树所有词频的最大值（trie 节点 `subtree_max`）；
- `best_k_score`：当前堆中第 k 名分数；
- `justification`：剪枝判据为 **`upper_bound < best_k_score`（严格小于）**。
  等号时绝不剪——同分候选可能凭稳定规范键进入 top-k。

## 排序与剪枝语义

- **结果排序**：`(score 降序, normalized_key 升序, surface 升序, id 升序)`，全序、可复现。
- **规范化碰撞**：如 `MULTIcast` / `multicast` / `ＭＵＬＴＩcast` 都归一到 `multicast`，
  它们共存于同一 term 桶，同分按 surface/id 稳定排序；返回的 `surface` 始终是原文。
- **空前缀**：`prefix=""` 合法，表示全局 top-k。
- **可靠上界**：`subtree_max = max(本桶词频, 各孩子 subtree_max)`，
  插入/降权/删除后沿受影响路径自底向上全部重算；删除导致的单孩子链会被重新压缩。
  `verify_integrity()` 独立 DFS 重算所有上界并检查结构不变量，任何漂移报 `E_INDEX_CORRUPT`。
- **查询复杂度**：定位前缀 O(键长)，之后只展开上界可能进入 top-k 的子树；
  不相关兄弟子树整体剪掉，绝不读取全词典再排序（效率断言见 `tests/test_efficiency.py`）。

## 错误语义（绝不把异常/未知状态返回成成功）

所有错误响应统一形如：

```json
{"ok": false, "request_id": "…", "error": {"code": "E_…", "category": "…", "message": "…", "details": {…}}}
```

| code | HTTP | 触发条件 |
|---|---|---|
| `E_INVALID_INPUT` | 400 | 请求体/查询参数 schema 校验失败（含负词频、k 越界、非整数、id 路径与 body 不一致） |
| `E_INVALID_SURFACE` | 400 | 原文为空/过长/含 NUL/规范化后为空（纯空白） |
| `E_INVALID_SCORE` | 400 | 词频为负、非整数，或增减后为负 |
| `E_INVALID_LIMIT` | 400 | k 不在 1..1000（引擎层；HTTP 层越界由 E_INVALID_INPUT 覆盖） |
| `E_ENTRY_NOT_FOUND` | 404 | 对不存在的 id 设频/增减/删除 |
| `E_DUPLICATE_ID` | 409 | 批量中同一 id 对应不同 surface |
| `E_NORMALIZER_VERSION_MISMATCH` | 409 | 库/快照的规范化版本与当前二进制不同——拒绝打开，必须重建 |
| `E_SNAPSHOT_CONFLICT` | 409 | 快照名冲突或名字非法 |
| `E_SNAPSHOT_NOT_FOUND` | 404 | 恢复不存在的快照 |
| `E_INDEX_CORRUPT` | 409 | 结构/上界校验失败，或 deep 对拍发现结果/剪枝上界与 oracle 不一致 |
| `E_INTERNAL` | 500 | 未预期异常（带堆栈写日志），不吞异常、不假成功 |

成功响应始终带 `"ok": true`；4xx/5xx 一定 `"ok": false` 且有具体 code。

## 版本与快照

- `normalizer_version`（`norm-1.0.0`）是固定常量。修改 NFKC/折叠/空白规则**必须**升版本；
  打开版本不一致的 db 或恢复不一致的快照都会报 `E_NORMALIZER_VERSION_MISMATCH`。
- 每次写事务递增 `revision`（可在 `/health`、`/versions` 查询，测试日志中记录）。
- 快照是主库经 SQLite 在线备份生成的**独立文件**（`data/snapshots/<name>.db`），
  含词条与全部 meta；恢复即覆盖主库并从落盘数据完整重建内存 trie，随后强制完整性自检。

## 测试与复现

测试要求：断言**具体结果与失败类别**，且参考答案不由被测核心自身生成：

- `app/oracle.py` 是独立暴力实现（全量 `startswith` + Python 排序），与 trie 零代码复用；
- 小夹具的期望值在测试里**手写**（如 `["mp-01","mp-02",...]`），双重防自证；
- `tests/test_hotword_pruning.py` 含 5 个随机种子的差分对拍（随机增删改上千轮）。

覆盖场景（对应验收要求）：

1. **长公共前缀**：压缩节点数、前缀子树 top-k、inside-edge 定位；
2. **同分**：跨 key 与碰撞桶内的稳定全序、20 次重复一致性；
3. **热词降权/删除**：降权/删除后补位正确，上界逐条与暴力子树最大值核对；
4. **规范化碰撞**：全角拉丁、大小写、ß/ss，显示原文逐字符保留；
5. **空前缀**：全局 top-k，且仍有剪枝而非全量入堆。

每个测试用例向 `logs/tests-<run_id>.jsonl` 写记录，字段包含：
`run_id`、`nodeid`、`normalizer_version`、`step`、输入、进度统计
（访问节点数/剪枝数/淘汰数）、每次剪枝的上界与第 k 名分数、`verdict`。
可用 `TRIE_TEST_RUN_ID` 注入运行身份以便 CI 关联。

```bash
# 复现：
TRIE_TEST_RUN_ID=repro-1 python -m pytest -q
grep '"verdict": "FAIL"' logs/tests-repro-1.jsonl   # 无输出即无失败记录
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRIE_DATA_DIR` | `./data` | db 与快照的根目录 |
| `TRIE_DB_PATH` | `$TRIE_DATA_DIR/trie.db` | SQLite 主库路径 |
| `TRIE_SNAPSHOT_DIR` | `$TRIE_DATA_DIR/snapshots` | 快照目录 |
| `TRIE_LOG_DIR` | `./logs` | JSONL 日志目录 |
| `TRIE_LOG_LEVEL` | `INFO` | 日志级别 |
| `TRIE_MAX_LIMIT` | `1000` | k 上限 |
| `TRIE_MAX_SURFACE_LEN` | `512` | 原文长度上限 |
| `TRIE_MAX_BATCH` | `10000` | 单批条目上限 |
