# Offline Secret Candidate Scanner

离线的代码仓库快照秘密**候选**扫描器：结合结构化规则（正则）与香农熵阈值，
在完全不联网的前提下对本地目录快照做检测。候选只持久化/展示**掩码 + 位置 +
内容指纹**，任何报告都不含完整秘密。

> 本工具只报告**候选**（pattern/entropy match），**不**认定真实泄漏，
> 也**不**验证凭据是否有效或已吊销——它从不发起任何网络请求。

---

## 1. 它做什么 / 不做什么（边界语义）

| 语义 | 含义 |
|---|---|
| 候选（candidate） | 命中结构化规则，或熵超阈值的 token。**不是**已确认泄漏，需人工研判。 |
| 不确定（uncertain） | 仅熵命中（`entropy_heuristic`，low confidence），或规则命中但低于该规则的熵下限。报告中单独成节。 |
| `new` | 本项目首次见到该 `(rule_id, 内容指纹)`。 |
| `active` | 首次之后再次观测到。 |
| `baseline_exempt` | 操作者按**内容指纹**接受的基线项；文件改名/移动不改变豁免。 |
| `known_fixed` | 更早扫描出现、本次扫描**未再观测到**。仅表示“当前快照未见”，**不是**凭据已吊销或问题已修复的证明。 |
| `reintroduced` | 曾被标记 `known_fixed` 的同一内容再次出现。 |
| `too_large` | 文件超过 `scope.max_file_bytes`，**完全不打开**，记入“未扫描”。 |
| `symlink_skipped` | 默认不跟随符号链接。 |
| `unreadable` | stat/打开/读取失败，单列错误，不计入已扫描。 |

**明确不做（无法执行的检查，不应当成已通过）：**

1. **凭据有效性/吊销验证** —— 需要联网，按需求禁止；`known_fixed` 不含此语义。
2. **压缩包/归档内部**（.zip/.tar/.git 对象包等）—— 按文件字节扫描，不做解包；
   压缩后的秘密通常表现为二进制高熵噪声，可能以不确定候选出现，也可能漏掉。
3. **加密内容、图片等富二进制中的语义提取** —— 只做字节级规则/熵检测。
4. **超大文件内容** —— 只记录“未扫描（too_large）”，不做分块/部分扫描，
   因此其中的秘密必然漏报；报告显式列出这类文件。
5. **符号链接目标**（默认配置）。
6. **编码混淆**（Base64 嵌套、十六进制分片、Unicode 同形字等）不做规范化解码，
   可能漏报；这是刻意的边界而非已覆盖能力。
7. **不可读文件**的内容无法检查（权限/IO 错误），报告以 `unreadable` 单列。

---

## 2. 模块划分（安全内核 / 状态隔离 / 审计接口）

```
secretscan/
├── errors.py          # 稳定失败码分类（validation_error/not_found/state_conflict/...）
├── rules.py           # 【规则/证据解析】版本化规则、范围、忽略配置的加载与校验
├── ignore.py          # gitignore 风格忽略匹配（无第三方依赖，自带实现）
├── pathpolicy.py      # 快照根约束、相对路径规范化
├── mask.py            # 不可逆掩码（只留 prefix4/suffix2）
├── fingerprint.py     # HKDF-SHA256 + HMAC-SHA256 内容指纹；文件 sha256
├── entropy.py         # 香农熵与 ASCII token 切分（纯启发式信号）
├── fileclass.py       # 文本/二进制判定
├── scanner.py         # 【安全内核】目录 -> 仅含掩码/指纹/位置的候选，无状态
├── models.py          # 结果模型（RawHit 不出单次扫描；Candidate 永不携带原值）
├── storage.py         # 【状态隔离】registry.db + 每项目独立 project.db 与独立密钥
├── service.py         # 编排：扫描 + 状态机 + 内容指纹基线 + 审计写入（事务串行化）
├── report.py          # 可解释报告：失败、不确定、known-fixed 分节；Markdown 渲染
├── redact.py          # 日志/输出二次脱敏（纵深防御）
├── logging_setup.py   # 带 request_id/actor/step 的 JSON 结构化日志
├── api.py             # 【审计接口】FastAPI：请求身份关联、统一错误信封、响应脱敏中间件
├── cli.py             # 命令行
└── version.py
config/rules.yaml      # 独立、版本化的配置（规则 + 范围 + 忽略 + 熵阈值）
tests/                 # 62 个测试 + 可复用合成夹具 + 手写参考答案
```

设计要点：

- **安全内核无状态**：`Scanner` 不接触数据库和基线，输入目录、输出安全结果，
  因此可以脱离被测系统其余部分独立测试。
- **状态隔离是物理隔离**：每个项目一个目录一个 SQLite 文件
  （`state/projects/<project_id>/project.db`），项目间互不可见；
  候选指纹的 HMAC 主密钥与 salt 也按项目独立随机生成（同内容跨项目指纹不同）。
- **规则/忽略版本化**：`schema_version` + 日期版 `rules_version` +
  `classification_version`；每次扫描持久化规则版本与**配置内容 sha256 摘要**，
  候选可追溯到产生它的确切配置。
- **基线绑定内容指纹**：豁免键是 `(rule_id, HMAC(匹配内容))`，
  移动/重命名文件只改变位置，不会消除也不会复制候选。
- **只存掩码与位置**：数据库列里没有任何原始秘密列；
  报告与 HTTP 响应再经一次正则脱敏中间件。

---

## 3. 安装

需要 Python ≥ 3.11。依赖版本固定在 `requirements.txt`。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

一键验证（建夹具 + 62 个测试 + CLI 冒烟；除可选 pip 外不联网）：

```bash
./verify.sh            # 使用已有 .venv
./verify.sh --install  # 首次：创建 venv 并安装固定版本依赖
```

---

## 4. 命令行用法

```bash
# 扫描本地快照
python -m secretscan scan /path/to/snapshot --note "release-2026.09"

# 最近一次扫描的报告（json / md）
python -m secretscan report /path/to/snapshot --format md
python -m secretscan report proj_xxxxxxxxxxxxxxxx --scan-id scan_xxx

# 项目 / 候选 / 审计
python -m secretscan projects
python -m secretscan candidates proj_xxx --state new
python -m secretscan audit proj_xxx --limit 20

# 内容指纹基线（用报告里的 rule_id + fingerprint）
python -m secretscan baseline list  proj_xxx
python -m secretscan baseline accept proj_xxx --rule-id aws_access_key_id \
       --fingerprint <hex> --note "committed known fake test key"
python -m secretscan baseline revoke proj_xxx --rule-id ... --fingerprint ...
```

环境变量：`SECRETSCAN_STATE`（状态目录，默认 `./.secretscan_state`）、
`SECRETSCAN_ACTOR`（操作者，默认当前系统用户）。

每条命令输出都带 `request_id`；JSON 日志写 stderr，含 `request_id/actor/step`。

## 5. HTTP 审计接口

```bash
export SECRETSCAN_STATE=/tmp/secretscan_state SECRETSCAN_CONFIG=config/rules.yaml
uvicorn local_asgi:app --uds /tmp/scan.sock      # 或 --host 127.0.0.1 --port 8080

curl -s --unix-socket /tmp/scan.sock http://localhost/health
curl -s --unix-socket /tmp/scan.sock -X POST http://localhost/projects/scan \
  -H 'content-type: application/json' \
  -H 'x-request-id: req-42' -H 'x-actor: alice' \
  -d '{"root":"/path/to/snapshot","note":"..."}'
```

主要端点：`POST /projects/scan`、`GET /projects/{id}`、
`GET /projects/{id}/scans`、`GET /projects/{id}/scans/{sid}/report?format=md`、
`GET /projects/{id}/candidates?state=`、
`GET/POST/DELETE /projects/{id}/baseline`、
`POST /projects/{id}/candidates/triage`、`GET /projects/{id}/audit`。

- 请求身份：`x-request-id`（缺省自动生成并回传响应头）+ `x-actor`（默认 anonymous）。
- 统一信封：成功 `{ok, request_id, data}`；失败 `{ok, request_id, error:{code,message}}`，
  失败码稳定（`validation_error`→400、`not_found`→404、`state_conflict`→409 等）。
- 响应经过脱敏中间件；所有写操作和预期错误都落项目审计事件。

---

## 6. 配置（独立版本化）

`config/rules.yaml` 是唯一配置文件，包含：

- `schema_version` / `rules_version`（`YYYY.MM.DD` 形式）/ `classification_version`
- `scope.max_file_bytes`（超限文件不打开，记 too_large）、`scope.follow_symlinks`
- `ignore.patterns`：`dir/`、`*.x`、`a/b`、`a/**/b`
- `entropy.shannon_threshold`、`entropy.min_token_length`
- `rules[]`：每条规则必须用命名组 `(?P<secret>...)` 标出秘密子串；
  可选 `min_entropy`（低于则标记不确定）；置信度 high/medium/low。

加载时强校验（非法正则、缺命名组、坏版本号、规则熵下限高于全局阈值等都会
以 `config_invalid` 失败），测试 `tests/test_rules_config.py` 覆盖各类失败。

---

## 7. 测试与夹具

```bash
python tests/build_fixtures.py   # 确定性重建夹具（固定随机种子）
python -m pytest                 # 62 个测试
```

夹具 `tests/fixtures/snapshot/`（**全部是人工假密钥，无真实凭据**）：

- 人工假密钥：`AKIAFAKE…`、`ghp_0123…`、`APP_TOKEN=…`、PGP 假私钥块；
- **普通高熵文本负对照**：64 位重复模式十六进制串（香农熵 4.0 < 4.5，不应报）；
- 已删除候选 / 移动文件：由 `test_service_state.py`、`test_scanner_e2e.py`
  在夹具副本上动态制造；
- 二进制嵌入 token（`data/blob.bin`）、超大文件（含诱饵秘密，必须不被读到）、
  不可读文件（mode 000）、不跟随的符号链接、被忽略目录（`.git/`、`vendor/`、`*.example.env`）。

**参考答案不由被测核心生成**：`tests/fixtures/expected_scan_v1.json` 是手写的，
其中指纹用独立的 HKDF/HMAC 参考实现（固定测试密钥）预算后硬编码，
掩码、行列位置、置信度、失败类别逐项断言；测试还直接用文件字节切片
复核二进制命中位置。测试断言具体结果与失败类别（too_large / symlink_skipped /
unreadable / state_conflict / not_found / config_invalid），不是“接口能调用”。

---

## 8. 安全说明

- 掩码：`len>=8 → 前4…后2`，其余更短；不可逆，仅用于人工关联。
- 指纹：`HMAC-SHA256(HKDF(master, salt), 匹配字节)`；主密钥 32 字节、
  项目 salt 16 字节，存于项目 SQLite（请按部署需要加密磁盘/限制目录权限）。
- 日志：应用层不记录候选原值；另有 `Redactor` 对已知形态做正则掩码作为兜底，
  测试断言日志行不含原值且带 request_id。
- 本仓库状态目录（`state_dir`）应视为敏感资产（含指纹、掩码、审计），
  但其中不存在可还原的秘密值。
