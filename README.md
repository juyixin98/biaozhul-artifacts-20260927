# 集中式文本 OT 后端（仅插入 / 删除）

一个自包含的协作文本编辑内核：集中式服务器对**插入**和**删除**两种操作做
OT（Operational Transformation），两个（或更多）本地模拟客户端可以离线编辑、
任意顺序提交，最终收敛到同一份文本。技术栈 **Python 3.12 + FastAPI + SQLite**，
全部数据为本地合成夹具，无任何外部业务依赖。

---

## 1. 它保证什么

* **字符索引**：所有位置与长度按 **Unicode 码点**（Python `str` 索引单位）计算。
  中文、emoji 与 ASCII 一样一个单位（已知限制见 §9）。
* **TP1 收敛**：对基于同一版本的两个操作 `a`、`b`，`transform(a,b) -> (a',b')` 满足

      apply(apply(S, b), a') == apply(apply(S, a), b')

  服务器把迟到操作依次 transform 穿过自其基线以来已落库的操作。客户端采用
  **"最多一个在途操作（inflight）"** 的同步纪律（一笔未确认前不发下一笔），
  因此同一并发区间每个客户端至多贡献一个操作；在该纪律下逐对变换即收敛，
  **与两个客户端操作的到达顺序无关**。
* **并发同点插入稳定排序**：插入携带来源键 `origin = (client_id, client_seq)`，
  同一锚点的并发插入按来源键字典序排列，键小者在合并文本中靠前。该规则
  确定性、与到达顺序无关。
* **删除重叠不重复扣减**：删除是"基线字符身份集合的删除"。双方删除重叠
  区域时重叠字符只扣一次；变换后的对侧操作不再删除已被对方删除的字符。
  若一方的删除意图被并发操作完全满足，变换结果是一个 no-op 标记版本。
* **意图保持**：一方的插入不会被另一方针对原基线的删除删掉；删除字符不会
  因并发插入而"复活"。
* **历史裁剪与基线重建**：服务器可把旧操作裁剪成某版本全文快照。基于裁剪
  水位之前版本的提交/拉取会被**明确拒绝**（`stale_baseline`，HTTP 410），
  客户端必须用当前快照重建基线后才能继续。
* **幂等重传**：每次编辑有客户端内单调 `client_seq`，配合 `Idempotency-Key`
  头，重复提交返回首次结果而不产生重复内容或重复版本。

### 关于 TP2 / 三个以上客户端的边界（明确的已知边界）

纯逐对 OT 有一个经典边界：当 **三个或更多** 个并发操作中，删除把两个不同
插入锚点之间的基线字符全部删掉时，这些插入"塌缩"到同一点，其相对顺序无法
仅靠逐对变换保证顺序无关（TP2 谜题）。本项目的处理是：

* 生产服务在**单 inflight 纪律**下运行，两个（乃至逐个同步的更多）客户端
  不会产生这种三向塌缩竞态，`transform` 已被穷举与随机差分验证收敛；
* 同时提供顺序无关的 :func:`app.ot.global_merge` 作为权威 N 路合并参考
  实现（删除并集、锚点分组按位置序、同锚点按 origin 序），并有专门测试
  验证 ≥3 客户端、全到达排列下塌缩插入仍收敛。要支持"多客户端多在途"的
  部署，可把服务端重放从逐对 `transform` 切换为该全局合并。

---

## 2. 目录结构（模块边界与数据契约）

```
app/
  errors.py    四类错误的稳定 code、category、HTTP 状态码与异常契约
  models.py    文本规范：Component / Op（规范化、码点索引、JSON 序列化）
  ot.py        算法索引：apply() 与 transform()（纯函数，无 IO）
  storage.py   版本存储：SQLite 文档/操作/幂等/快照表、事务、裁剪
  service.py   服务编排：提交重放、水位校验、幂等、配额、查询与诊断
  api.py       FastAPI 路由与统一错误响应
  client.py    两个本地模拟客户端 + 进程内 / HTTP 两种传输
  config.py    环境变量配置（配额、DB 路径、诊断开关）
scripts/
  demo.py            进程内端到端演示（含裁剪与基线重建）
  example_calls.sh   针对真实 HTTP 服务的 curl 示例
tests/
  oracle.py            独立预言机（原子打标 + 身份对齐，刻意不 import app）
  helpers.py           生产 Op 与预言机裸元组操作的桥接（仅测试层）
  test_ot_core.py        硬编码具体期望的核心用例
  test_ot_exhaustive.py  短串穷举操作对 + 三操作全排列收敛
  test_random_fuzz.py    真实双客户端随机差分 + N 路全局合并顺序无关
  test_service_api.py    服务层 + HTTP 错误分类/幂等/裁剪/配额
  test_client_sim.py     两个本地客户端的同步循环
  test_storage_boundary.py 裁剪水位边界 + SQLite 持久化重开
  test_http_e2e.py       起真实 uvicorn + httpx 的网络端到端
  conftest.py          运行编号日志（test-runs/）
```

数据流：`HTTP/client → service（锁 + 事务边界）→ ot（纯算法）+ storage（SQLite）`。
`ot.py` 不依赖存储与网络，可独立测试；`storage.py` 不做算法判断；错误分类集中
在 `errors.py`。

---

## 3. 操作的线上格式

一个操作是**从左到右覆盖整个基线文档**的组件数组（末尾 retain 不能省，
`base_len` 必须等于基线码点长度）：

```json
{
  "client_id": "alice",
  "client_seq": 3,
  "base_revision": 12,
  "components": [
    {"type": "retain", "n": 5},
    {"type": "insert", "text": "你好", "client_id": "alice", "seq": 3},
    {"type": "retain", "n": 4},
    {"type": "delete", "n": 2}
  ]
}
```

* `retain n`：保留基线接下来 n 个码点；`insert text`：在游标处插入（不消耗
  基线字符，必须带来源）；`delete n`：删除基线接下来 n 个码点。
* 规范化：相邻同类合并（相邻 insert 仅在**同源**时合并），纯 no-op 编辑
  （只有 retain）禁止由客户端提交。

---

## 4. 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 精确版本见 requirements-lock.txt

# 跑全部测试（50 个）
python -m pytest                          # 也会生成 test-runs/ 日志

# 进程内端到端演示（两个客户端 + 裁剪）
python -m scripts.demo

# 启动真实 HTTP 服务
OT_DB_PATH=ot.db uvicorn app.api:app --host 127.0.0.1 --port 8000
# 另一终端：
BASE=http://127.0.0.1:8000 bash scripts/example_calls.sh
```

### HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/documents` | 建文档 `{doc_id, text?}` |
| GET  | `/documents` | 列出文档 |
| GET  | `/documents/{id}` | 当前全文、head、裁剪水位 |
| POST | `/documents/{id}/ops` | 提交操作（可带 `Idempotency-Key` 头） |
| GET  | `/documents/{id}/ops?after=N&limit=M` | 拉取增量（分页） |
| POST | `/documents/{id}/prune?new_horizon=N` | 裁剪历史并写快照 |
| GET  | `/diagnostics` | 文档/版本/保留操作数/配额等诊断 |
| POST | `/internal/faults/{id}` | 测试用：注入一次计算失败 |

提交成功返回：`revision, head_revision, base_revision, rebased, replay, text, op`。

---

## 5. 错误契约（四类可区分）

错误响应统一为：

```json
{"error": {"code": "stale_baseline", "category": "state_conflict",
           "message": "...", "details": {...}}}
```

| category | 代表 code | HTTP | 触发场景 |
| --- | --- | --- | --- |
| `input_error` | `doc_not_found` / `malformed_operation` / `empty_insert` | 404/400 | 未知文档、坏组件、索引/基线长度不符、no-op 编辑 |
| `state_conflict` | `stale_revision` / `revision_ahead` / `duplicate_request` / `stale_baseline` | 409/410 | 版本落后/超前、幂等键冲突、**裁剪后旧基线** |
| `resource_exhausted` | `document_too_large` / `operation_too_large` | 413 | 文档或单次操作超过配额 |
| `compute_failed` | `transform_invariant` / `storage_failure` | 500 | 核心不变量被破坏（正常输入不应出现） |

> 注：FastAPI 请求体 schema 校验失败返回 422，是输入错误家族的框架层表现；
> 业务层输入错误统一用 400。

客户端侧，发送基线已被裁剪时 `LocalClient.sync()` 抛 `ClientResetRequired`，
调用 `rebuild_baseline()` 用当前快照恢复后再继续（离线未确认编辑会丢失，
这是裁剪的既定代价）。

---

## 6. 验证方案（参考答案不是被测实现自己生成的）

1. **硬编码核心用例**（`test_ot_core.py`）：手算的精确变换组件序列与收敛
   文本——同点排序、身份键反向、删除重叠、删除/插入交错、多字节、规范化。
2. **独立预言机穷举对拍**（`tests/oracle.py`）：预言机用与生产核心**完全
   不同**的算法——给每个基线码点与每次插入分配唯一原子身份，删除按身份
   集合过滤，同锚点按 `origin` 排序，再用原子身份差两指针对齐反解出
   retain/insert/delete。它刻意不 `import` 任何 `app.*` 代码。对
   `"" / "a" / "ab" / "abc"` 以及 `"你🌟" / "a你"` 上的**全部原始编辑操作对**
   （共数百对）逐组件断言相等，并验证两个重放顺序收敛。
3. **三操作全排列**：固定 4 组三客户端编辑，穷举 6 种到达顺序，服务式
   transform 重放必须得到同一文本。
4. **随机差分模糊**（`test_random_fuzz.py`）：一是 120 步随机"编辑/同步"
   交错驱动两个真实客户端与服务端，断言三方文本与版本最终一致（多种子
   参数化；另在开发期用 150 个随机种子×150 步做过压力验证）；二是对 ≥3
   个基于同一版本的并发操作，验证 `global_merge` 对全部到达排列同结果
   （含删除塌缩锚点）。
5. **服务 / HTTP**：并发收敛、重叠删除经重放、多字节、重复提交幂等、
   同 seq 不同体冲突、配额耗尽回滚、四类错误各自的 code/category/状态码、
   裁剪后旧基线 410、拉取分页、诊断。
6. **两个客户端模拟**：离线不同区域编辑、同点排序两种到达顺序、多字节、
   重复同步无重复、一方排队多笔 + 另一方插入、裁剪触发
   `ClientResetRequired` 与重建后继续协作。
7. **存储边界与持久化**：裁剪水位上下的提交/读取、快照前向重放、SQLite
   落盘关闭重开后 head/水位/快照保持且可继续追加。
8. **真实网络 e2e**：在测试中起 uvicorn，用 httpx 经真实套接字验证路由、
   JSON 格式、幂等头、410。

### 可重放测试日志

每次 `pytest` 运行分配单调 **run_id**，事件写入 `test-runs/run-<id>.jsonl`，
并刷新 `test-runs/latest.jsonl` 与 `test-runs/index.json`。关键用例额外记录
输入、**关键中间状态**（每种到达顺序的中间文本、rev 各版本文本）与**判定
理由**；失败用例带错误长描述。例如：

```
test_triple_sequences_all_orders_converge / triple-orders
  inputs.orders_checked = 全部 6 种排列
  states.per_order      = 每个排列的重放文本
```

重放某个问题：用 run_id 找到对应 jsonl，取其中 `inputs` 即可复现操作序列。

---

## 7. 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OT_DB_PATH` | `ot.db` | SQLite 文件路径（测试用 `:memory:`） |
| `OT_MAX_DOC_CHARS` | `1000000` | 单文档最大码点数 |
| `OT_MAX_OP_CHARS` | `100000` | 单操作最大插入/删除码点数 |
| `OT_MAX_PULL_BATCH` | `1000` | 单次拉取上限 |
| `OT_FAULT_INJECTION` | 关 | 诊断开关（也可用内部接口按文档注入） |

---

## 8. 并发与持久化语义

* 服务用一把进程内锁串行化写事务，SQLite 单连接 + WAL；适合单机/本地模拟，
  不是多副本分布式存储。
* 每次提交在一个 SQLite 事务内追加操作、推进 head、登记幂等键；配额失败会
  回滚，不留半截版本。
* 裁剪在事务内写水位快照、删旧操作与旧快照、推进 `pruned_horizon`。

---

## 9. 已知限制

* **不做字形簇（grapheme cluster）归一化**：索引按码点，组合音标、ZWJ
  emoji 序列、肤色修饰符可能被拆到多个位置。若要面向终端用户光标，应在
  码点索引之上再加一层字形簇映射。
* **单 inflight 同步纪律**：生产服务依赖"每客户端至多一个未确认操作"。
  它覆盖两个客户端的全部协作以及更多客户端的逐笔同步；若要支持同一客户端
  多笔在途或 ≥3 客户端在删除塌缩锚点处的强并发（TP2 边界），需把服务端
  重放切换为已随附的 `global_merge`（见 §1 末尾）。
* 单进程锁，无水平扩展、无鉴权/多租户、无长连接推送（客户端用轮询拉取）。
* 裁剪是粗粒度的：水位之前的操作整体丢弃，旧客户端只能整体重建基线，
  无法对裁剪前的未确认编辑做续传（会收到明确的 reset 信号而非静默错乱）。
* 不保留操作的富文本/注释语义，只有纯文本插入与删除。
