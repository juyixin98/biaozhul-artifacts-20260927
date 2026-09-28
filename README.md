# 集中式纯文本 OT 后端（仅插入 / 删除）

一个从零实现的 Operational Transformation 服务：多个客户端基于各自看到的
文档版本提交“在统一字符索引上的插入/删除”，服务端把并发操作变换（transform）
后串行提交，保证 **任意接收顺序都收敛到同一文档**。技术栈 Python +
FastAPI + SQLite，全部数据与参与者均为本地合成夹具，无需外部账号。

---

## 1. 它满足哪些业务契约

1. **统一字符索引**：所有位置是 0 基 Unicode **码点**偏移（不是字节、不是
   UTF-16 单元）。emoji、中文等多字节字符与 ASCII 字符同等占一个位置。
2. **并发同点插入稳定排序**：每个插入字符带全局身份
   `<client_id>#<client_op_id>:<seq>`，同一锚点上的并发插入按身份字符串
   总序排列。裁决只依赖身份，与操作到达服务端的顺序无关，因此两个接收
   方向、甚至三方的全部 6 种接收顺序都得到同一结果。
3. **删除重叠区不重复扣除**：删除按字符身份命中（墓碑），两个删除覆盖
   同一字符只生效一次，不会出现负长度或坐标错位。
4. **意图保持**：插入若落在对方删除区间内部，删除会在插入处裂成两段，
   插入字符保留（“删者删原文，插者的新字符留下”）。
5. **历史裁剪前旧客户端必须重建基线**：裁剪抬高 `baseline_rev` 并删除更旧
   修订；此后 `base_rev < baseline_rev` 的提交被拒绝（`BASELINE_TRIMMED`），
   客户端必须先 `GET /catchup` 重建。
6. **幂等提交**：`(doc_id, client_id, client_op_id)` 唯一；重复提交同一
   操作体返回首次结果，复用同一序号但操作体不同则 `REUSED_OP_ID`。
7. 不包含任何聊天 / 编辑器界面。

### 为什么除了位置变换还需要“字符身份”

只对一对操作做位置版 TP1 变换，在 *两个* 并发操作时足以收敛；但当三个
以上插入以不同顺序被**链式**变换时，若它们锚定的字符被别的操作删除，
整数位置会把它们压到同一点，相对先后随提交顺序漂移。生产实现因此在
服务端使用 `otbackend/iddoc.py` 的**槽位 + 字符身份模型**：

- 初始文档有 `n+1` 个跨版本稳定的**槽位**（基字符之间的间隙）；
- 插入挂到它在基版本上看到的槽位，同槽位按字符 id 排序；
- 删除按字符 id 命中，天然幂等。

线上协议与客户端 API 仍是纯整数位置；身份模型只在服务端提交时内部使用。
位置版变换（`otbackend/transform.py` 的 `xform`/`compose`）保留给单步
变换、本地组合与客户端重定基。两套表示由引擎交叉校验
（`ID_POSITION_MISMATCH` 防护）。

---

## 2. 工程结构与数据/错误契约

```
otbackend/
  textmodel.py    文本规范：码点索引、Comp 基元、规范流、应用/校验
  transform.py    位置版 OT：xform（TP1 对称变换）、compose（顺序组合）
  iddoc.py        字符身份 + 槽位模型：顺序无关的服务端集成与差异导出
  engine.py       版本重建、基线校验、提交、长度/交叉不变量、限额
  repository.py   存储接口 + SQLite 实现 + 内存实现（修订/快照/幂等/校验和）
  service.py      事务编排：幂等提交、历史裁剪、追赶、查询
  api.py          FastAPI：REST 路由与统一错误体
  config.py       环境变量配置
  errors.py       四类可区分错误
  clientsim/      两个本地模拟客户端 + 传输（直连 / 脚本乱序 / HTTP）
tests/
  oracle.py       独立参考答案：标记原子 + 独立的槽位/id 服务器（不引用被测核心）
  test_exhaustive_pairs.py  短串全部操作对（含多字节）双重顺序收敛
  test_three_ops.py         部分三操作 × 全部 6 种接收顺序
  test_error_classes.py     四类失败的具体 reason 断言
  test_persistence_idempotency.py  多字节/幂等/重叠删除/SQLite 持久化/校验和
  test_clients.py           两客户端、乱序、重传、裁剪重建
  test_http_api.py          HTTP 端到端与错误码
examples/         本地演示脚本
scripts/          启动与测试脚本
```

### 修订（revision）数据契约

| 字段 | 含义 |
|---|---|
| `rev` | 从 1 递增的版本号；`rev=0` 是初始文档 |
| `client_id` / `client_op_id` | 来源身份（同点总序、回声识别、幂等） |
| `base_rev` | 该消息声明的基线版本 |
| `length_before/after` | 应用前/后码点长度（长度不变量） |
| `checksum` | `sha256(doc|rev|after_len|ops_json)[:16]`，存储损坏诊断 |
| `ops` | 变换到 HEAD 之后的规范组件 JSON（`ins`/`del`） |

### 错误分类（HTTP 状态码 + 机器可读 reason）

| 类别 | code | 状态码 | 典型 reason |
|---|---|---|---|
| 输入错误 | `INPUT_INVALID` | 400 | `BAD_JSON` `BAD_POS` `EMPTY_INS` `BAD_LENGTH` `OVERLAP_IN_MESSAGE` `BAD_CLIENT_ID` `REUSED_OP_ID`(见下) |
| 状态冲突 | `STATE_CONFLICT` | 409 | `DELETE_OUT_OF_RANGE` `INSERT_OUT_OF_RANGE` `BASE_AHEAD_OF_HEAD` `BASELINE_TRIMMED` `REUSED_OP_ID` |
| 资源耗尽 | `RESOURCE_EXHAUSTED` | 413 | `TOO_MANY_COMPONENTS` `INSERT_TOO_LARGE` `DOC_TOO_LARGE_CHARS/BYTES` |
| 计算失败 | `COMPUTATION_FAILED` | 500 | `CORRUPT_HISTORY` `LENGTH_INVARIANT` `ID_POSITION_MISMATCH` `TRANSFORM_APPLY_FAILED` |
| 不存在 | `DOC_NOT_FOUND` | 404 | `DOC_NOT_FOUND` |

错误体形状统一：

```json
{"error": {"code": "...", "reason": "...", "message": "...",
           "details": {...}, "request_id": "..."}}
```

每个响应还带 `x-request-id` 头；收敛测试把**运行编号、关键中间状态、
判定理由**写入 `test_runs.jsonl`（见第 5 节），失败可用 run_id 重放。

---

## 3. 快速开始

需要 Python 3.10+（开发实测 3.12.3）。

```bash
# 1) 建虚拟环境并安装锁定依赖
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock.txt        # 或 requirements.txt

# 2) 跑全部测试（会真实执行，54 个用例）
bash scripts/run_tests.sh -q
# 或：python -m pytest -q

# 3) 启动服务
bash scripts/run_server.sh                  # 默认 127.0.0.1:8000
# 或：OT_DB_PATH=/tmp/d.db OT_PORT=8123 python -m otbackend.api

# 4) 本地演示（不需要起服务）
python examples/two_clients_demo.py         # 三方 × 6 种顺序收敛
OT_START_SERVER=1 python examples/http_calls.py   # 起服务并走 HTTP
```

---

## 4. HTTP API 与示例调用

| 方法 路径 | 作用 |
|---|---|
| `POST /documents` | 建文档 `{doc_id, initial_text?}` |
| `GET  /documents/{id}` | 当前视图（`rev` / `baseline_rev` / `text` / `length`） |
| `POST /documents/{id}/submit` | 幂等提交，见下 |
| `GET  /documents/{id}/history?since=&limit=` | 版本查询/诊断 |
| `GET  /documents/{id}/revisions/{rev}` | 单修订详情（含 checksum、ops） |
| `POST /documents/{id}/trim` | `{keep_from_rev}` 写快照并抬高裁剪基线 |
| `GET  /documents/{id}/catchup` | 旧客户端重建基线 |
| `GET  /healthz` | 健康检查 |

提交体：

```json
{
  "base_rev": 0,
  "client_id": "alice",
  "client_op_id": 1,
  "ops": [
    {"type": "ins", "pos": 5, "text": "!"},
    {"type": "del", "pos": 0, "length": 2}
  ]
}
```

一条消息可含多个组件；删除区间在该消息内不得重叠（否则
`OVERLAP_IN_MESSAGE`），插入文本不能为空。

用 `curl` 的完整往返：

```bash
curl -s localhost:8000/documents -H 'content-type: application/json' \
  -d '{"doc_id":"d","initial_text":"hello"}'
curl -s localhost:8000/documents/d/submit -H 'content-type: application/json' \
  -d '{"base_rev":0,"client_id":"alice","client_op_id":1,
       "ops":[{"type":"ins","pos":5,"text":"!"}]}'
# 两个基于 r0 的并发提交，服务端负责收敛：
curl -s localhost:8000/documents/d/submit -H 'content-type: application/json' \
  -d '{"base_rev":0,"client_id":"bob","client_op_id":1,
       "ops":[{"type":"ins","pos":0,"text":"("}]}'
curl -s localhost:8000/documents/d/history
```

裁剪与重建：

```bash
curl -s localhost:8000/documents/d/trim -H 'content-type: application/json' \
  -d '{"keep_from_rev":2}'
# 此后 base_rev<2 的提交得到 409 BASELINE_TRIMMED；客户端先：
curl -s localhost:8000/documents/d/catchup
```

---

## 5. 验证方案（如何证明它是对的）

### 5.1 独立参考答案，不用被测核心自我背书

`tests/oracle.py` 是**独立实现**：

- `Token`：带身份的标记原子，收敛判定比较的是 token **身份序列**，
  连“同形不同源”的字符也能区分；
- `OracleServer`：用与生产代码独立编写的“槽位 + 字符 id”模型模拟服务器，
  全程**不导入** `otbackend.id_doc/transform`。

### 5.2 穷举与排列

- `test_exhaustive_pairs`：基线 `""`、`"a"`、`"ab"`、`"abc"`，枚举每个
  位置的插入（含 ASCII 与 emoji `😀`）和**全部非空删除子区间**（覆盖
  重叠/包含/相接/相同），对每对操作检查两种接收顺序：
  1. 位置版 TP1 两侧字符串相等；
  2. 被测结果 == 独立 oracle 两种顺序的身份序列渲染；
  3. 两种 oracle 顺序身份序列完全相同。
- `test_three_ops`：选取覆盖同点插入、重叠删除、插入落删除区间、多字节
  的候选消息，对三方各取一个，跑 **3! = 6 种接收顺序全排列**，断言六个
  服务端结果两两相等且等于 oracle。

### 5.3 多字节、重复提交、旧基线拒绝

- 多字节：`a😀b` / 中文按码点索引插入与删除，断言具体字符串；
- 幂等：同一 `(client,op_id)` 重放（含不同 base_rev 的重传）只产生一个
  修订；同号异体被拒；
- 裁剪：`BASELINE_TRIMMED` 被拒、`catchup` 后恢复；
- 重叠删除：并集只扣一次；
- SQLite 文件落盘后新连接重新打开，历史/幂等仍在。

### 5.4 失败类别可区分、断言具体结果

测试断言具体最终字符串/身份序列**和**具体 `reason`，不只是“接口可调
用”。输入错误、状态冲突、资源耗尽、计算失败分别有独立用例。

### 5.5 可重放日志

收敛套件通过 `tests/runlog.py` 把每个 run 写到 **`test_runs.jsonl`**
（路径可用 `OT_TEST_LOG` 覆盖），每行包含：

```
run_id, suite, inputs(基线/操作空间), steps(检查数/失败数/各顺序中间结果),
expect, actual, verdict(PASS/FAIL), reason
```

失败时 `AssertionError` 消息带 `run_id`，可据此从 `inputs` 重放。

---

## 6. 配置

环境变量（见 `otbackend/config.py`）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `OT_DB_PATH` | `ot.db` | SQLite 路径（`:memory:` 为内存） |
| `OT_HOST` / `OT_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `OT_MAX_COMPONENTS` | `10000` | 单消息组件上限 |
| `OT_MAX_DOC_CHARS` | `10000000` | 文档码点上限 |
| `OT_MAX_DOC_BYTES` | `50000000` | 文档 UTF-8 字节上限 |
| `OT_HISTORY_PAGE_LIMIT` | `1000` | history 分页上限 |
| `OT_TEST_LOG` / `OT_TEST_LOG_KEEP` | `test_runs.jsonl` / 清空 | 测试日志路径 / 追加模式 |

---

## 7. 已知限制（如实说明）

1. **单进程写串行**：SQLite 用进程锁 + `BEGIN IMMEDIATE` 保证“读历史→
   变换→写修订”原子；多进程部署需要把串行化上移到单写者/队列。
2. **快照只按显式 trim 生成**：版本重建从最近快照重放修订；文档极大、
   历史极长时重放成本线性增长，需要周期性快照（接口已具备，未做自动
   调度）。
3. **删除不携带“被删文本”校验**：服务端以基线文档为准校验坐标与长度，
   但不比对客户端声称的删除内容（协议中删除只有 `pos/length`）。
4. **历史裁剪不可逆**：低于基线的修订物理删除，旧客户端只能 catchup
   重放当前全文，不能增量补洞。
5. **身份总序的键空间**：同点裁决依赖 `(client_id, client_op_id)` 全局
   唯一且不可复用；这是协议前提（复用同号异体已被拒绝），但不防恶意
   伪造身份——本系统无鉴权，定位为本地/可信网络模拟。
6. **无富文本/游标语义**：只处理纯文本码点，不处理样式、注释、光标
   一致性广播等（按范围明确排除）。
