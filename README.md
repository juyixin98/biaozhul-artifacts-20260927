# 大文本非重叠替换规划服务（Non-overlapping Replacement Planning Service）

基于**线性时间、无回溯 RE2 正则引擎**和**受限捕获模板**，在大体积 UTF-8 文本上：

1. 对多组规则计算**非重叠**替换**计划**；
2. 计划携带**原始字节范围**、渲染好的替换字节和**源摘要绑定**；
3. 计划可**流式应用**，并在源版本不符时显式拒绝；
4. 提供规则/计划的持久化版本存储与可重放诊断。

技术栈：Python 3.12 · FastAPI · SQLite（标准库）· [google-re2](https://github.com/google/re2)。
全部数据与外部参与者均为本地合成夹具，不依赖任何生产账号或真实业务数据。

---

## 1. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 运行完整验证（测试 + 打印本次运行编号/摘要）
bash scripts/verify.sh

# 启动服务（内存库，重启即清空）
.venv/bin/uvicorn app.api.app:app --reload
# 或持久化到文件
NRS_DB=/tmp/nrs.db .venv/bin/uvicorn app.api.app:app
```

### 端到端示例

```bash
curl -s -X PUT localhost:8000/sources/doc -H 'content-type: application/json' \
  -d '{"text":"cat catalog catbird"}'
curl -s -X PUT localhost:8000/rulesets/rs -H 'content-type: application/json' \
  -d '{"rules":[
        {"rule_id":"cat","pattern":"cat","template":"FELIX","priority":5},
        {"rule_id":"catalog","pattern":"catalog","template":"LOG","priority":1},
        {"rule_id":"catbird","pattern":"catbird","template":"BIRD","priority":1}]}'
# 计划：cat 在三处均胜出，catalog/catbird 因重叠被丢弃
curl -s -X POST localhost:8000/sources/doc/plans/rs
# 用返回的 plan_id 应用
curl -s -X POST localhost:8000/plans/<plan_id>/apply -d '{}' -H 'content-type: application/json'
# -> {"output":"FELIX FELIXalog FELIXbird", ...}
```

交互式 API 文档：<http://localhost:8000/docs>。

---

## 2. 算法假设与语义（重要）

### 2.1 引擎：RE2（无回溯、线性时间）

* 使用 RE2 把模式编译为有界自动机，匹配耗时关于文本长度为**线性**，不存在指数回溯；
  通过 `Options.max_mem` 设置程序内存预算，超限返回稳定错误码 `pattern_budget_exceeded`。
* RE2 **不支持**回溯型构造：前/后顾 `(?=…)`/`(?<=…)`、反向引用 `\1`、反向引用式条件等。
  使用这些构造在**规则创建时**即被拒绝（`unsupported_syntax`），而不是运行时。
* `\w`/`\d` 等字符类默认 **ASCII 语义**（RE2 默认 `perl_classes=False`），与 Python
  标准库 `re` 的 Unicode 语义不同——这是刻意固定并由测试钉住的差异。需要 Unicode
  字符时直接写字面量或字符类（如 `[世界]`、`€+`）。
* 标志位为显式白名单：`i`（大小写不敏感）、`s`（`.` 跨行）、`m`（`^`/`$` 按行）。
  RE2 默认 `^`/`$` 仅锚定整段文本，`m` 通过内联 `(?m)` 恢复常见按行语义。

### 2.2 匹配选择：最左、零宽前进

扫描器在**原始字节缓冲**上运行 RE2（模式以 bytes 编译，span 即 UTF-8 字节偏移），
语义对齐 Python `re.finditer`（这也是独立参考实现的基准）：

```
pos = 0
while pos <= n:
    m = pattern.search(data, pos=pos)      # pos 起的最左匹配
    记录候选 (m.start, m.end, 各组的字节范围与字节)
    if m 为零宽:
        if m.end == n: 结束                 # 末尾空匹配只触发一次
        pos = 越过 m.end 的下一个【码点边界】  # 跳过整个码点
    else:
        pos = m.end                        # 非空：从匹配末尾继续
```

关键点：RE2 的裸 bytes 扫描在零宽后按 **+1 原始字节**前进，会落到多字节字符内部；
本服务改用 **码点感知前进**（`ByteIndex.advance` 跳过整个 UTF-8 码点），因此多字节
文本中不会产生落在续字节上的非法匹配。所有候选起止与捕获范围在渲染前都经过
UTF-8 边界与整码点完整性校验（`validate_byte_range`）。

* **相邻匹配**（`end_i == start_j`）不冲突：相邻非空匹配各自独立触发，源字节至多被消费一次。
* **替换内容不会在同一轮被重新匹配**：只对**原始缓冲**扫描一次，替换在扫描结束后才生成。
  例如 `b -> bb` 对 `ab` 恒为 `abb`，不会级联膨胀。

### 2.3 多规则同轮优先级与重叠消解

* 每条规则对同一原始缓冲独立扫描；候选按 `(-priority, rule_id)` 处理（priority 大者先；
  同优先级按 rule_id 升序）。结果与规则在输入列表中的先后无关，确定性由该序保证。
* 两个候选冲突当且仅当：
  * **同起点**：起点偏移相同（覆盖零宽↔零宽、零宽↔非空所有组合）——同一位置只允许一个触发；
  * **非空区间字节重叠**：半开区间满足 `cs < e 且 s < ce`。这包含“候选起点落在已占用
    区间内部”的情形（如低优先级 `a` 落在已胜出的 `cat` 区间内会被抑制）；
  * **零宽候选严格位于已占用非空区间内部**（`cs < point < ce`）被抑制；恰好落在某区间
    **尾边界**（`point == ce`）属于相邻，允许触发。
* 接受的编辑最终按文档序输出；零宽编辑在边界处插入、不消费输入。

### 2.4 受限捕获模板

语法刻意很小，不做二次求值、不支持格式规约：

```
$$          -> 字面 '$'
$0..$9      -> 按编号引用捕获（$0 为整段匹配）
${name}     -> 按 Python 标识符名引用命名组
${10}       -> 编号 >= 10 的组
其他字节     -> 原样字面量（$x 中的 $ 为字面量）
```

* `$9`/`${x}` 引用了模式中**不存在**的组：规则创建期静态错误 `unknown_capture`；
  语法破损（未闭合 `${`、空 `${}`、非法标识符）：`invalid_template`。
* 引用了**存在但本次未参与匹配的可选组**：默认策略 `missing_capture="error"` 令计划失败，
  错误码 `capture_missing`（明确暴露锚定不当的规则，而不是静默替换为空）；
  可在规则上显式设置 `missing_capture="empty"` 改为以空字节替换并记录。

### 2.5 计划绑定与流式应用

* 计划不可变，字段含 `source_sha256`、`source_length`、规范化标志、最终替换字节与各编辑
  原始字节范围。捕获引用在构建计划时即完成展开并校验，存储后的计划可脱离引擎重放。
* 应用前强制校验：当前数据摘要必须等于绑定摘要（或调用方给出的 `expected_sha256`），
  不符立即抛 `source_version_mismatch`（HTTP 409），**在产出任何字节之前**失败。
* 应用以生成器按块（默认 64 KiB）产出，原始片段以零拷贝 `memoryview` 透传，峰值内存与
  块大小而非文档大小成比例。同一计划在存储层只允许应用一次（`already_applied`，409）。

### 2.6 资源上限与失败分类（可区分）

| 类别 | 错误码 | 触发 | HTTP |
|---|---|---|---|
| INPUT | `text_not_utf8` / `empty_text` / `text_too_large` | 解码/空/超请求硬上限 | 400 |
| INPUT | `invalid_pattern` / `unsupported_syntax` / `invalid_template` / `unknown_capture` / `invalid_rule` / `invalid_byte_range` | 规则或范围非法 | 400 |
| STATE_CONFLICT | `source_version_mismatch` / `already_applied` | 版本不符 / 重复应用 | 409 |
| NOT_FOUND | `source_not_found` / `ruleset_not_found` / `plan_not_found` | 实体不存在 | 404 |
| RESOURCE | `pattern_budget_exceeded` | RE2 `max_mem`/程序规模超限 | 413 |
| RESOURCE | `text_budget_exceeded` / `match_budget_exceeded` / `output_budget_exceeded` | 文本/候选/编辑/产出超上限 | 413 |
| COMPUTATION | `capture_missing` / `engine_failure` | 可选捕获缺失 / 引擎执行失败 | 422 |

---

## 3. 模块关系与数据/错误契约

```
app/
  errors.py           统一失败分类（AppError 子类：category + 稳定 code + http_status）
  textspec/
    normalize.py      str/bytes -> 严格 UTF-8 归一化（NormalizedText.data 为唯一权威缓冲）
    bytemap.py        ByteIndex（码点边界，array('I')）、码点级前进、字节范围校验、sha256
  engine/
    compiler.py       RE2 编译/标志白名单/max_mem，错误码归一（CompiledPattern 携带组表）
    scanner.py        码点感知、零宽安全的最左扫描（Candidate[GroupSpan]）
  template.py         受限模板解析（静态未知捕获）与渲染（缺失捕获策略）
  planner/
    rules.py          RuleSpec -> CompiledRule（创建即 fail-closed 编译）
    model.py          Edit / Plan（含 to_json/from_json）/ Decision / PlanResult
    planner.py        多规则扫描 + 同轮优先级消解 + 预算 + 字节范围校验 -> PlanResult
    apply.py          摘要绑定校验 + 分块流式应用
  storage/
    database.py       SQLite 连接、WAL、外键、显式 schema_version
    repository.py     唯一 SQL 层；源版本(内容寻址)、规则集、计划、决策、应用记录
  api/
    service.py        用例编排（归一化→索引→编译→计划→持久化→应用）+ 硬上限
    schemas.py        Pydantic 边界模型
    app.py            FastAPI 路由与 AppError -> HTTP 状态码映射
tests/
  oracle.py           独立参考实现（仅 stdlib re + 纯 Python），不导入任何 app 核心代码
  fixtures/samples.py 手写夹具（文本/规则/手工预期）
  test_*.py           8 组测试，断言具体结果与失败类别
  conftest.py         每运行可重放日志（运行编号、CHECK 中间态与判定理由）
scripts/
  verify.sh           一键验证并打印运行编号/摘要（失败即非零退出）
  inspect_runs.py     按编号查看某次运行的摘要/CHECK/失败
```

数据契约要点：所有跨模块传递的文本都是 `NormalizedText.data`（UTF-8 bytes）；偏移一律为
对该缓冲的**原始字节半开区间** `[start, end)`；计划 JSON 是存储与重放的唯一序列化形式。
错误一律为 `app.errors.AppError`，携带稳定 `code`、`category`、`http_status` 与 `details`，
HTTP 层只做分类→状态码映射，不产生新错误。

---

## 4. 测试与可重放性

* **独立参考答案**：`tests/oracle.py` 只用标准库 `re` 重新实现最左/零宽/优先级语义，
  用码点偏移计算再映射为字节偏移；它不 import 任何被测核心，因此预期答案不是被测代码
  自己生成的。
* **确定性随机等价测试**：固定种子（单规则 60 组 × 多次、多规则 30 组），在多字节字母表
  上逐一比较“字节区间 + 规则归属 + 零宽标志 + 最终文本”。跨引擎已知差异（`\b` 的
  ASCII vs Unicode 词符）仅在纯 ASCII 文档上比较；多字节 `\b` 交互由专门手写用例钉住
  服务自身语义。
* 按要求保留的验证场景：零宽匹配、相邻匹配、捕获缺失（严格/宽容）、多规则重叠、多字节
  文本、计划应用与小规模参考一致、拒绝源版本不符、重复应用、资源耗尽分类、约 150 KB
  多字节文档的多块流式与参考一致。
* **失败类别断言**：测试断言具体 `error`/`category`/HTTP 状态，而非“接口能调用”。

每次 `pytest` 运行在 `test-runs/run-<编号>-<时间戳>-<pid>/` 留下：

* `events.jsonl`：`RUN_START`（运行编号、解释器版本）、逐条 `CHECK`（期望值、实际值、
  **判定理由**、关键中间状态如编辑区间/拒绝记录）、`FAILURE`、`RUN_END`；
* `summary.json`：运行编号与通过/失败/跳过的完整用例清单。

```bash
bash scripts/verify.sh                       # 跑全套并打印最新运行编号
.venv/bin/python scripts/inspect_runs.py     # 查看最新运行
.venv/bin/python scripts/inspect_runs.py 1 --checks        # 查看第 1 次运行的关键判定
.venv/bin/python scripts/inspect_runs.py 1 --grep overlap  # 过滤
```

---

## 5. 依赖版本

在 Python 3.12 开发与验证（`requirements.txt` 锁定）：

| 包 | 版本 | 用途 |
|---|---|---|
| google-re2 | 1.1.20251105 | RE2 引擎的 Python 绑定（manylinux cp312 预编译 wheel） |
| fastapi | 0.141.1 | HTTP 边界 |
| starlette | 1.7.0 | FastAPI 底层（随附） |
| pydantic | 2.13.5 | 请求/响应模型（随附） |
| uvicorn | 0.54.0 | 本地 ASGI 服务器 |
| httpx | 0.28.1 | TestClient 传输 |
| pytest | 9.1.1 | 测试框架 |
| anyio | 4.15.1 | 随附 |

SQLite 使用 Python 标准库 `sqlite3`（本地实测版本随解释器）。

## 6. 边界与限制

* JSON 上传的是字符串（必然合法 Unicode）；bytes 严格 UTF-8 入口由服务层直接测试覆盖。
* 单进程本地服务：SQLite 连接开启 `check_same_thread=False` 并以显式事务 + 锁串行化写，
  适合本地与单机并发，不面向多进程/分布式部署。
* 计划是“同一轮一次扫描”的规划结果；跨多轮迭代改写不在本服务范围内（可在外层对新源版本
  重新建计划）。
