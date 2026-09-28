# FFG 双签 / 包围投票证据检测器（本地合成版）

一个**完全本地运行**的简化 Casper-FFG 共识违规证据系统：接收验证者投票，
在密码学验签之后检测两类可处罚行为，构造**可独立复核**的证据包，并用明确
简化的最终性规则跟踪链状态。所有密钥与数据均为本地合成夹具，无需任何生产
账号或真实业务数据。

* 语言/框架：Python 3.11+、FastAPI、SQLite、`cryptography`（Ed25519）
* 版本：`1.0.0`
* 证据格式版本：`1`，数据库 schema 版本：`1`

## 检测的违规（精确定义）

设同一验证者有两张已验签投票 `V=(s,t,...)`、`V'=(s',t',...)`：

1. **双签（double_vote）**：`t == t'`（同一目标轮次）但两张票提交的内容不同
   （消息根不同：源/目标轮次或任一区块根不同）。
2. **包围投票（surround_vote）**：严格嵌套 `s < s' < t' < t`，或反向提交的
   对称情形（`s' < s < t < t'`）。**任一端点相等都不算包围**。
3. **重复（duplicate，非违规）**：完全相同的投票（含签名）重传，只记录为
   `duplicate`，不产生证据、不加权、不处罚。

权重始终取**该票目标轮次的时代快照**；证据内嵌两张票各自的时代快照，因此
验证者集合在轮次之间发生变化后，旧证据仍然可以独立复核。

## 安全不变式

* 签名消息通过域分离常量绑定 **链域（chain_id）、验证者公钥、源/目标轮次、
  源/目标区块根**。改动任何一个字段都会导致 Ed25519 验签失败。
* 接收顺序固定为：解析 → 链域 → 轮次顺序 → **验签** → 成员资格 → 去重 →
  冲突扫描 → 最终性折叠。**未通过验签的消息永远不可能创建证据或处罚标记。**
* 处罚标记（slash mark）只在证据构造完成、且其内嵌快照与签名都成立时写入。
* 证据 `evidence_id` 是对全部身份字段规范化序列化后的 SHA-256；独立检查器
  会重算该 id，任何篡改都会被发现。

## 目录结构

```
src/ffg_slash/
  encoding.py     确定性字节编码、域分离、消息根、快照根
  crypto.py       Ed25519 签名/验签（cryptography 原语）、本地合成密钥
  models.py       Vote 模型、JSON 信封严格解析、状态/原因枚举
  registry.py     验证者注册表与按时代的权重快照
  state.py        纯最终性内核（超级多数链接、justified/finalized）
  evidence.py     冲突谓词、证据包构造、规范化 evidence_id
  detector.py     接收管线（验签门禁、冲突扫描、计数、处罚标记）
  storage.py      SQLite 索引存储（快照/投票/证据/权重/事件）
  replay.py       离线回放 JSONL/JSON 输入
  api.py / app.py FastAPI 路由与装配
  config.py       JSON 配置 + 环境变量覆盖
  cli.py / __main__.py  命令行（serve / replay / gen-demo）
independent/      独立证据检查器（不 import ffg_slash，见下）
tests/            40 个测试，断言具体结果与失败类别
scripts/          demo feed 生成
configs/ data/ logs/ docs/
```

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # 或精确锁定版 requirements-lock.txt

# 1) 生成合成配置（5 个标签派生验证者，epoch 3 发生成员轮换）
PYTHONPATH=src python -m ffg_slash gen-demo --out configs/demo.json

# 2) 生成演示投票流（含诚实多数、重复票、伪造签名、真实双签/包围、失效成员、坏行）
PYTHONPATH=src python scripts/generate_demo_feed.py

# 3) 离线回放
PYTHONPATH=src python -m ffg_slash replay \
    --config configs/demo.json --feed data/demo_feed.jsonl --json
```

演示数据的真实运行结果（13 行输入）：`accepted=9, duplicate=1`，
拒绝分类 `invalid_signature=1, malformed=1, validator_inactive=1`，
**真实冲突 `double_vote=1, surround_vote=1`**，最终化推进到
`justified=epoch2 / finalized=epoch1`。

### 启动 HTTP 服务

```bash
PYTHONPATH=src python -m ffg_slash serve --config configs/demo.json --port 18099
```

| 方法/路径 | 说明 | 状态码 |
|---|---|---|
| `GET /health` | 版本、run_id、chain_id | 200 |
| `POST /votes` | 提交一个投票信封 | 201 accepted / 200 duplicate / 400 rejected（带 `reject_reason`）/ 422 malformed |
| `GET /evidences` | 全部已验证证据 | 200 |
| `GET /evidences/{id}` | 单条证据 | 200 / 404 |
| `GET /stats` | 分类计数、可处罚权重、最终性 | 200 |
| `GET /finality` | 当前 justified/finalized | 200 |

示例：

```bash
curl -s -X POST http://127.0.0.1:18099/votes \
  -H 'content-type: application/json' \
  --data @<(head -1 data/demo_feed.jsonl)
```

### 用独立检查器复核证据

`independent/` 目录是**第二套实现**：它不导入 `ffg_slash` 的任何代码，只依据
`docs/protocol.md` 用标准库 + `cryptography` 重新推导编码、验签、快照根与
违规规则。

```bash
# 复核真实证据（退出码 0=有效）
PYTHONPATH=src:. python -m independent.cli data/evidence/evidence_1_double_vote.json
# 篡改/伪造证据 -> 退出码 1，并给出具体失败类别
#   bad_signature / bad_snapshot_root / not_member / rule_mismatch /
#   bad_domain / bad_evidence_id / invalid_format / unknown_type
```

## 运行测试

```bash
. .venv/bin/activate
python -m pytest                 # 40 个测试
```

测试覆盖：编码确定性与签名对每个绑定字段的约束、重复票 vs 双签、非法签名不
产生证据、包围与嵌套边界、成员轮换下的权重、严格 2/3 最终性、独立检查器对
真实/篡改证据的判定与 CLI 退出码、离线回放计数、磁盘重建、HTTP 状态码、日志
run_id 关联。

## 日志

每次运行有独立 `run_id`，写入 `logs/run-<id>.log`（JSON 行，同时输出到
stderr）。每行带 `run_id / seq / validator / step / status / version`，例如
验签成功是 `"step verify_signature: ok"`，伪造票是
`"step invalid_signature: rejected"` 并附 `reject_reason` 与被校验消息根。
异常和未知状态不会被合并成成功：它们分别归类为具名的拒绝原因。

## 明确简化与剩余限制

见 [`docs/limitations.md`](docs/limitations.md)。协议字节级规范见
[`docs/protocol.md`](docs/protocol.md)。
