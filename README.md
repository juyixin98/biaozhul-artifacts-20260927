# 非重叠替换规划服务（Non-overlapping Replacement Planner）

在**大文本**上，基于**成熟的无回溯正则引擎 Google RE2**与**受限捕获模板**，
生成“同轮非重叠替换计划”，并提供版本存储、计划绑定、流式应用与诊断。
全部本地运行，数据均为进程内合成夹具，无任何生产账号/真实业务数据依赖。

---

## 1. 本地验证命令

```bash
# 1) 建虚拟环境并安装固定依赖（PyPI，wheel 直装，无需系统库）
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2) 一键跑全部测试（71 个），同时在 test-logs/ 生成可重放结构化日志
python -m pytest tests/ -v

# 3) 重放最近一次运行：运行编号、关键中间状态、判断理由、失败类别
python scripts/replay.py                 # 最近一次
python scripts/replay.py <run_id>        # 指定运行编号（meta.json 里的 run_id）
python scripts/replay.py --failed        # 只看失败/出错
python scripts/replay.py --case zero_width   # 只看标题含某子串的用例

# 4) 端到端演示（服务层，多字节 + 流式 + 版本不符拒绝）
python scripts/demo.py

# 5) 启动 HTTP 服务并用 curl 验证（见下文 API）
NRP_DB_PATH=./data/demo.db uvicorn app.api:app --port 8000
```

**预期判断方式**

* `pytest` 结尾为 `71 passed`，退出码 0；`test-logs/latest` 指向最新运行，
  其 `summary.json` 的 `counts` 应为 `{"passed":71,"failed":0,...}`。
* 未通过/未运行：`summary.json.failed_cases` 给出失败用例与异常文本；
  `scripts/replay.py --failed` 逐条还原中间状态与期望/实际值。
  本仓库提交状态下**无未通过、无跳过**的用例。
* `demo.py` 最后一步必须打印
  `拒绝成功: STATE_SOURCE_VERSION_MISMATCH (state) HTTP 409`。

### curl 速验（服务启动后）

```bash
SID=$(curl -s -X POST localhost:8000/sources -H 'content-type: application/json' \
  -d '{"text":"a@x.com b@y.org 你好"}' | python -c 'import sys,json;print(json.load(sys.stdin)["source_id"])')

curl -s -X POST localhost:8000/plans -H 'content-type: application/json' -d "{
  \"source_id\":\"$SID\",
  \"rules\":[{\"rule_id\":\"mail\",\"pattern\":\"(\\w+)@(\\w+)\\.com\",\"template\":\"[\\1@\\2]\",\"priority\":10}]
}"
# 流式应用（响应头含 X-Run-Id / X-Replaced-Count）
curl -s -X POST localhost:8000/plans/<plan_id>/apply
```

---

## 2. 模块关系（依赖方向自底向上，禁止反向依赖）

```
config        可调限制（文本/程序/条目预算）、运行编号
errors        稳定的错误契约：code / category / http_status / details
schemas       Pydantic 数据契约（源、规则、计划、替换、诊断的线上表示）
textutil      文本规范(str/UTF-8)、源摘要(sha256+长度)、码点↔原始字节范围(O(1)前缀表)
engine        RE2 封装：编译分类、捕获快照、bump-along 非重叠扫描
template      受限捕获模板：计划前先验证引用，命中后再渲染（严格/宽松缺失组）
planning      多规则同轮扫描 + 确定性重叠消解 + 字节范围验证（纯算法，不落库）
storage       SQLite 版本存储：不可变源版本、计划/条目/淘汰、诊断事件
apply         计划的流式应用 + 源摘要/版本守卫（生成器，分块 UTF-8）
diagnostics   结构化诊断记录器（run_id、stage、level、中间状态）
service       用例编排：事务边界、状态机 planned→applied（仅一次）
api           FastAPI：路由 + ServiceError → HTTP 的统一渲染
```

`planning` / `engine` / `template` / `apply` / `textutil` **不依赖**存储与
Web 层，可被测试独立调用。测试另有一份**完全独立的参考实现**
`tests/oracle.py`（只用标准库 `re`/`hashlib`，不 import `app.*`）。

---

## 3. 算法假设（确定性契约）

### 3.1 文本与位置

* 规范文本是 Unicode 码点序列（Python `str`），持久化为 UTF-8。
* 正则匹配位置一律是**码点偏移**半开区间 `[char_start, char_end)`；
  对外同时给 **UTF-8 原始字节范围**。计划构建时对每条命中做往返验证：
  `utf8[byte_start:byte_end].decode() == text[char_start:char_end]`。
* 码点→字节用**逐码点前缀字节长度表**做 O(1) 查询（纯 ASCII 零额外内存）。
  这避免了大文本 + 空模式逐码点查询时的 O(n²) 退化。

### 3.2 引擎与零宽前进（bump-along）

* 使用 Google RE2（编译为自动机，**对文本线性时间、无指数回溯**）；
  编译受 `max_mem` 预算约束，超限得到独立错误 `COMPUTE_REGEX_PROGRAM_TOO_LARGE`。
* 不支持回溯语义特性：反向引用 `\1`、命名反向引用 `(?P=n)`、环视
  `(?=…)` `(?!…)` `(?<=…)` 在**编译期**以 `COMPUTE_REGEX_COMPILE` 拒绝。
* 单规则非重叠扫描（与 Python 3.7+ `re.finditer` 同语义）：
  从游标找最左匹配；命中后若 `end>start` 游标跳到 `end`，
  若 `end==start`（零宽）游标前进 **1 个码点**（串尾仍产出一次空匹配）。
  因此空模式在 `"ab"` 上产出 `(0,0)(1,1)(2,2)`。
* **替换内容不重新进入同轮匹配**：所有命中都来自原始文本，规划是单轮的。
* 字符类语义：RE2 默认 `\w \d \s` 仅 ASCII（参考实现以 `re.ASCII` 对齐）。

### 3.3 多规则同轮优先级消解

所有规则各自在原文上扫描，得到候选集合后，按确定性排序键考察：

```
(char_start,
 消耗型先于零宽,      # 同一起点，真正吃字符的匹配优先于纯插入
 priority,           # 声明的同轮优先级，数字小者优先
 declaration_order,  # 同优先级按声明顺序（稳定、可重放）
 -span_len)          # 同点同优先级，跨度更长者优先
```

按序贪心选择，与已选区间相交即淘汰（记录到 `displaced`，含原因
`covered_by_higher_priority` / `covered_by_earlier_same_priority`）。
实现为 **O(n log n) 扫描线**（按末端的最小堆 + 最深覆盖区间），
在 12 万码点多字节文本 + 空模式（约 13 万候选）上约 1–2 秒。

零宽边界规则：零宽插入允许落在消耗区间的两端点，但严格落在区间内部、
或同一点已有零宽动作时淘汰（相邻零宽只取排序最前者）。

### 3.4 捕获模板（受限）

仅支持字面量、`\g<name>`、`\g<1>`、`\1`…`\99`、`\\`。
**计划前**解析并校验所有引用（未定义组/悬空反斜杠/非法转义 →
`INPUT_INVALID_TEMPLATE`）；渲染命中时，严格模式（默认）引用“本次未参与
的可选组”报 `COMPUTE_CAPTURE_UNAVAILABLE`，宽松模式渲染为空串。
不含任何可执行/函数语义，也不支持 `\0`/`\g<0>`。

### 3.5 计划绑定与流式应用

* 计划绑定创建时的 `source_id + source_version + sha256(+长度)`。
* 应用前重新计算当前版本与摘要：版本号或摘要任一不符 →
  `STATE_SOURCE_VERSION_MISMATCH`（409），计划保持 `planned`、不产生新版本。
* 应用是生成器：按 `NRP_CHUNK_CHARS` 码点分块产出 UTF-8，
  版本登记与 `planned→applied` 翻转发生在流成功消费完之后。
* 同一计划重复应用 → `STATE_PLAN_ALREADY_APPLIED`（409）。

---

## 4. 错误类别（可区分，测试按 code/category 断言）

| category | 典型 code | HTTP | 含义 |
|---|---|---|---|
| input | `INPUT_TEXT_NOT_UNICODE` | 422 | 非合法 UTF-8 |
| input | `INPUT_INVALID_RULE` / `INPUT_INVALID_TEMPLATE` / `INPUT_INVALID_FLAG` | 422 | 规则/模板/标志非法 |
| input | `INPUT_TEXT_TOO_LARGE` | 413 | 文本超预算 |
| compute | `COMPUTE_REGEX_COMPILE` | 422 | 语法错误或引擎不支持的特性 |
| compute | `COMPUTE_REGEX_PROGRAM_TOO_LARGE` | 413 | RE2 程序超 max_mem |
| compute | `COMPUTE_CAPTURE_UNAVAILABLE` | 422 | 严格模式引用了未参与的可选组 |
| resource | `RESOURCE_EXHAUSTED` | 413 | 运行期命中数等超预算 |
| state | `STATE_SOURCE_NOT_FOUND` / `STATE_PLAN_NOT_FOUND` | 404 | 资源不存在 |
| state | `STATE_SOURCE_VERSION_MISMATCH` | 409 | 源版本/摘要与计划绑定不符 |
| state | `STATE_PLAN_ALREADY_APPLIED` | 409 | 计划已应用（状态机仅一次） |

错误体统一为
`{"error":{"code","category","message","details"}}`，
并通过响应头 `X-Run-Id` 返回可用于取诊断的运行编号。

---

## 5. 测试与参考实现（为什么答案不是“被测核心自己生成”）

* `tests/oracle.py`：独立参考实现，**只 import 标准库**，自己实现了
  bump-along 扫描、多规则消解（朴素 O(n²)，规格等价）、模板解析/渲染、
  字节范围换算与应用拼接。核心与它在零宽/相邻/多规则/多字节场景逐条对账。
* 部分场景另给**手写常量**（如空模式 `"ab"→"-a-b-"`、宽松缺失组
  `"ab9"→"[][ab9]"`）做第二重独立校验。
* 覆盖需求点名场景：
  * 零宽前进 `test_engine.py::test_bump_along_*`、
    `test_planning_oracle.py::test_zero_width_*`、`test_consuming_beats_*`
  * 相邻匹配 `test_adjacent_*`
  * 捕获缺失（严格/宽松）`test_template.py` + `test_..._capture_missing_*`
  * 多规则重叠/优先级 `test_multi_rule_overlap_*`、`test_same_priority_*`
  * 多字节 `tests/test_textutil.py`、`test_..._multibyte_*`
  * 版本不符拒绝 `test_storage_apply.py` / `test_api.py`
  * 输入/状态/资源/计算四类失败 `test_limits.py`、各 `*_error_contract`
* 测试断言**具体位置/文本/字节范围/失败 code**，不是“接口能调用”。

### 测试日志与可重放性

每个用例在 `test-logs/<run_id>/cases/*.jsonl` 写带 `seq` 的事件：
`intermediate_state`（候选、排序、渲染、字节范围等关键中间状态及其重要性）、
`assertion`（判断理由 + expected/actual）、`failure_category`、`case_end`。
`meta.json` 记录运行编号与包版本；`summary.json` 汇总通过/失败。
用 `scripts/replay.py` 重放问题。

---

## 6. 依赖版本（已锁定，见 requirements.txt）

| 包 | 版本 | 角色 |
|---|---|---|
| Python | 3.12（CPython，x86_64 验证） | 运行时 |
| google-re2 | 1.1.20251105 | Google RE2 无回溯引擎（manylinux 预编译 wheel） |
| fastapi | 0.141.1 | HTTP 框架（starlette 1.7.0） |
| pydantic | 2.13.5 | 数据契约 |
| uvicorn | 0.54.0 | ASGI 服务 |
| pytest | 8.4.2 | 测试 |
| httpx | 0.28.1 | 测试客户端底层 |

> 备注：`fastapi.testclient` 与当前 starlette/httpx 组合有便捷方法兼容警告，
> 测试统一改用 `starlette.testclient.TestClient`，功能不受影响。

环境变量（可选）：`NRP_DB_PATH`、`NRP_MAX_TEXT_CHARS`、`NRP_REGEX_MEM`、
`NRP_MAX_PATTERN_CHARS`、`NRP_MAX_TEMPLATE_CHARS`、`NRP_MAX_RULES`、
`NRP_MAX_MATCHES`、`NRP_CHUNK_CHARS`、`NRP_TEST_LOG_DIR`。

---

## 7. 已知边界 / 非目标

* 服务是单进程本地 SQLite；不处理多写者并发协调（写操作以进程内锁串行）。
* 不提供正则教学外的高级特性（条件替换、回调、整串反向引用）。
* 大文本内存：多字节源会保留前缀表与 gzip 正文，预算见 `app/config.py`。
