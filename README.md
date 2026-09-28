# anon-risk — 匿名化等价类风险检查后端

对**合成**表执行 k-匿名 / l-多样性核验，校验泛化层级，并在小规模格点上
穷举给出**保持真实等价类计数**的最优泛化建议。技术栈：Python 3.11+、
FastAPI、SQLite、cryptography。无生产账号、无真实业务数据依赖。

> **指标定位**：k-匿名与 l-多样性只刻画等价类规模与敏感属性多样性，
> **不构成完整隐私保证**（不防御背景知识、相似性、差分/推断攻击等）。
> 每条风险报告都内置该免责声明，前端与文档不得移除。

---

## 1. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt          # 含测试依赖（锁定见 requirements-lock.txt）

# 可选但推荐：固定主密钥与管理令牌（否则开发模式生成一次性临时密钥，健康接口会暴露）
export ANON_RISK_MASTER_KEY="$(.venv/bin/python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
export ANON_RISK_ADMIN_TOKEN="dev-admin-token"

# 启动（数据/日志默认写入 ./data 与 ./logs）
PYTHONPATH=src .venv/bin/python -m anon_risk --host 127.0.0.1 --port 8080

# 另一个终端：完整端到端示例（创建→核验→建议→不可达→审计）
BASE=http://127.0.0.1:8080 ANON_RISK_ADMIN_TOKEN=dev-admin-token bash scripts/example_calls.sh

# 不启服务，直接调用内核
PYTHONPATH=src .venv/bin/python scripts/kernel_demo.py

# 生成可复现的合成大表（固定随机种子，注入少量 NULL）
.venv/bin/python scripts/generate_fixture.py --n 24 --seed 7 --out /tmp/synth.json
```

交互式 OpenAPI 文档：<http://127.0.0.1:8080/docs>。

## 2. 测试（独立参考实现 + 具体结果断言）

```bash
.venv/bin/python -m pytest -q
```

测试不是“接口能调通”级别的：

- `tests/expected.py` 是**手工推导**的 12 个格点判定表与最优解；
- `tests/oracle.py` 是**只用标准库、不导入被测包**的第二份暴力实现，
  独立复算全部向量并与手算表、被测实现三方交叉核对；
- 覆盖：极小群体、QI/敏感列 NULL 保留、阈值不可达（k>n、l 超过不同敏感值数）、
  层级不包含/不完整、格点超限、输出不泄漏原始值、每运行加密隔离、
  审计只追加、失败类别错误码、日志关联身份与判定依据。

## 3. 请求最小示例

`POST /runs`（字段含义见 [`docs/API.md`](docs/API.md)）：

```json
{
  "columns": ["zip", "age", "disease"],
  "quasi_identifiers": ["zip", "age"],
  "sensitive": ["disease"],
  "rows": [["10001","23","Flu"], ["10002","25","Cold"]],
  "hierarchies": {
    "zip": {"levels": [{"rule":"prefix","keep":4}]},
    "age": {"levels": [{"rule":"range","bins":[0,30,120],"labels":["<30","30+"]}]}
  }
}
```

响应返回 `run_id` 与只出现一次的 `access_token`。之后：

- `POST /runs/{id}/evaluate?k=2&l=2`，body `{"levels":{"zip":1,"age":2}}`
  出风险报告（等价类计数、风险类别、检察官风险、判定原因、免责声明）；
- `POST /runs/{id}/suggest`，body `{"k":2,"l":2}` 穷举最优泛化；
  不可达时 HTTP 仍 200，但 `status="UNREACHABLE"` 并给出聚合证据，
  **不伪装成功**；真正的错误返回 4xx + 稳定 `error.code`。

所有数据接口需头 `X-Run-Token`；审计接口需 `X-Admin-Token`；
可用 `X-Correlation-ID` 关联一次调用（响应头与日志都会带回）。

## 4. 工程结构

```
config/default.toml              配置层（TOML + ANON_RISK_ 环境变量覆盖）
src/anon_risk/
  kernel/                        纯函数安全内核（不依赖 Web/DB）
    parser.py                    按规则解析、显式列角色、NULL 规范化与证据
    hierarchy.py                 层级实例化、完整性与包含关系验证
    equivalence.py               真实等价类计数、k/l、风险类别、DM/LM
    optimizer.py                 格点穷举 + 真实计数信息损失最小化
  security/                      Fernet/HKDF/HMAC、出站白名单视图
  storage/                       每运行独立加密 SQLite + 只追加审计库
  service.py                     编排（解析→校验→指标→安全视图→审计）
  api/                           FastAPI 路由/鉴权/错误映射
  config.py / logging_setup.py / errors.py
fixtures/                        tiny.csv / tiny.json 合成夹具
scripts/                         示例调用、内核直调、合成生成器
tests/                           pytest、stdlib oracle、手工期望值
docs/                            架构、API、指标口径、安全模型与限制
```

分层约束：内核不做 I/O；存储不算指标；HTTP 只做鉴权与序列化；
任何出站结构都经 `security/view.py` 白名单——原始 QI/敏感值离开不了进程。

## 5. 关键行为承诺

- **显式声明**：QI 与敏感列必须显式给出，缺失即报错。
- **NULL 不丢行**：空串/空白/`null` 规范化为 NULL，行保留；NULL 在 QI 中
  自成类、不与非 NULL 合并；NULL 不作为敏感值参与 l 多样性计数。三者均有
  专门的计数与测试。
- **层级可验证且保持包含**：覆盖不完整、越界、非数值、prefix 的 keep 未
  严格递减、range 箱不逐级变粗等都有独立错误码。
- **真实计数**：优化目标 DM=Σ(类大小²) 与平局指标 LM 都由真实类大小加权，
  测试用构造案例证明系统不做“均匀分布”假设。
- **规模保护**：格点组合数超过 `kernel.lattice_combo_cap` 直接报
  `LATTICE_TOO_LARGE`，不退回未验证的近似结果。
- **可观测**：JSONL 日志带版本、run_id、correlation_id、穷举进度（`n/total`）
  与判定依据；审计库触发器拒绝 UPDATE/DELETE。
- **静态加密 + 隔离**：每运行独立 SQLite 文件与 HKDF 派生密钥；令牌只存
  SHA-256 摘要；等价类只以 HMAC 指纹对外关联。

## 6. 剩余限制（务必阅读 [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md)）

穷举仅适合小表；指标不含 t-近邻/差分隐私等；审计防 DDL 需部署级文件保护；
多敏感列当前只对第一列出 l 多样性；细节见安全模型文档。
