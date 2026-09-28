# zcluster — 多维整数列的 Morton 聚簇与范围查询后端

本地列式分块存储：多维整数坐标按 **Morton（Z-order）码**聚簇，范围查询把
多维盒**保守地**分解为一维码区间、用区间扫描写除候选，再用精确的逐坐标
**残余过滤**保证零漏行。区间分解预算耗尽时允许扩大候选集（退化为全域保守
区间），但绝不漏结果。重写（compaction）保留稳定的行身份。

四个承担实际工作的模块 + 独立测试 + 独立参考实现：

| 路径 | 职责 |
|---|---|
| `zcluster/kernel/` | **执行内核**：`coder`（固定位宽/有符号映射/位交错）、`decompose`（盒→保守码区间） |
| `zcluster/format/` | **格式适配**：PyArrow IPC 列式分块读写（uint64 或定长大端二进制码列） |
| `zcluster/meta/` | **元数据事务**：SQLite catalog（dataset/chunks/audit 三表，`BEGIN IMMEDIATE` 事务，先写文件后发布、提交后删旧） |
| `zcluster/api/` | **验证接口**：FastAPI（请求身份关联、错误分类、可解释步骤/版本/位置、不确定性单列） |
| `zcluster/core/store.py` | 编排：入库、两阶段查询、全扫描对照、重写 |
| `zcluster/verification/` | 内建验证套件（边界/负数/薄盒/高维稀疏，对照全扫描，失败分类） |
| `reference/naive.py` | **独立参考答案**：与被测内核不同算法的字符串位交错、逐码枚举盒成员、最小游程覆盖 |
| `tests/` | 独立 pytest 套件，断言具体数值与失败类别 |
| `scripts/verify.py` | 一键验收：pytest + 实时 HTTP 服务冒烟 + 5 万行规模实验，产出 JSON 报告 |

## 核心机制（不是硬编码演示）

- **每维固定位宽与有符号映射**：有符号维使用保序的二进制翻转
  `x ^ (1<<(bits-1))`，负数映射到无符号域后仍保持数值序。
- **交错位不截断高位**：第 d 维第 j 位放在交错位置 `j*ndim + d`；最高交错
  位是 `(wmax-1)*ndim + (ndim-1)`，其上方不发射任何位，其下方逐位发射
  （窄维高位置零）。Python 任意精度整数保证任意宽度精确；`total_bits ≤ 64`
  时代码列用 Arrow `uint64`，超过 64 位（如 4×24=96 位）自动改用定长大端
  二进制列，且仍可区间比较。
- **盒→保守一维区间**：沿 Z 树逐级单 bit 深度优先（每级只切一个 Morton
  位，而不是同时切所有维度），盒内整子树发射一个区间、盒外整子树 O(1)
  剪枝、盒面处继续分裂、叶点显式判盒。
- **预算耗尽不漏结果**：发射满 `interval_budget` 个精确区间后仍在部分节点
  时，退化为单一**全域**非精确区间 `[0, 2^total_bits)`；响应中
  `budget_exhausted=true`、该区间 `exact=false`、`uncertainties[]` 单列说明。
- **精确残余过滤**：对每个候选用原始坐标（无符号盒边）逐维判定，丢弃盒外
  假阳性。统计候选膨胀（false positives、膨胀率）与分块读取量
  （chunks_read/skipped、bytes_read）。
- **稳定行身份**：`row_id` 由 catalog 在一个事务内预留（gap-free uint64），
  重写块时逐字节复制 `row_id` 列，绝不重新分配；元数据在单个事务内交换，
  旧文件提交后才删除（崩溃只可能留下孤儿文件，不会让元数据指向缺失文件）。

## 依赖与版本（Python 3.12 / Linux 实测）

```
pyarrow==18.1.0
fastapi==0.115.6
pydantic==2.10.4
uvicorn==0.34.0
httpx==0.28.1
pytest==8.3.4
```

## 从干净目录复现

```bash
# 1) 虚拟环境 + 安装
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2) 独立测试（断言具体结果与失败类别，参考答案来自 reference/ 独立实现）
python -m pytest -q

# 3) 一键验收：pytest + 内建验证 + 实时 uvicorn 冒烟 + 规模实验
python scripts/verify.py
# 报告写入 reports/run-<UTC时间戳>.json，stdout 同时打印；失败返回非零

# 4) 手动起服务（配置见 config/default.json，可用 ZCLUSTER_CONFIG 覆盖）
uvicorn zcluster.api.app:app --host 127.0.0.1 --port 8000
# 请求样例见 examples/requests.md（schema → ingest → query → full-scan →
# compact → chunks → 审计 → /api/verify）
```

## 实测结果（本环境，2026-09-28）

最新一次 `scripts/verify.py`（报告见 `reports/`，**整体通过、退出码 0**）：

- **pytest：34 passed**。
- **内建验证套件 4/4 通过**：
  - `roundtrip`：边界/负数坐标（如 4 位有符号 -8/7、5 位有符号 -16/15）
    编码-解码往返，并与 `reference.naive.spread_interleave`（不同算法）逐位一致；
    高位不截断（最大角点码的 top 位被置位）。
  - `box_coverage`：3×2 有符号小域穷举所有盒 × 3 个预算，区间并集必须覆盖
    每一个真实码（含负角盒、薄盒、满域、预算 1 的碎片化盒）。
  - `zero_miss`：2D 边界/负数 + 5D 高维稀疏薄超平面，在预算 1/8/256 下
    与全扫描逐 `row_id` 对照，零漏行、零多余行。
  - `rowid_stability`：100 行以小块入库（34 个碎块），重写后行身份逐一相等、
    块数下降，再入库的新行 id 从 100 续号。
- **实时 HTTP 冒烟**：schema/ingest/query/full-scan/compact/verify/审计，
  盒 `x,y∈[-2,1]` 查询 33 行与全扫描完全一致；预算 1 时全域保守、零漏。
- **规模实验**（40,000 行聚簇 3D + 50,000 行稀疏 4D）：

  | 场景 | 预算 | 读取块 | 读取字节 | 候选行 | 假阳性 | 漏行 |
  |---|---|---|---|---|---|---|
  | 聚簇 3D（结果 171） | 1（耗尽） | 20/20 | 252,424 | 40,000 | 39,829 | 0 |
  | 聚簇 3D | 64（耗尽） | 20/20 | 252,424 | 40,000 | 39,829 | 0 |
  | 聚簇 3D | 4096（精确） | **1/20** | **12,578** | 217 | 46 | **0** |
  | 稀疏 4D（结果 61） | 1/256/100000（耗尽） | 25/25 | 609,130 | 50,000 | 49,939 | **0** |

  聚簇数据在充足预算下分块裁剪约 20 倍、候选缩小约 184 倍；稀疏高维盒
  在 Z 序上真实地碎片化，中等预算即耗尽并诚实退化为全域扫描——候选大幅
  膨胀但结果集与全扫描**始终一致**（`zero_miss: true`）。这是被如实报告
  的不确定结论，不是静默的错误。

## 可解释性

- 每个响应（含错误）都带 `request_id`：客户端可用 `X-Request-Id` 指定，
  服务端否则生成 `req-…` 并在响应头回显；同一 id 关联结构化 JSON 日志
  （`logs/`）与 SQLite `audit` 表（`GET /api/requests/{id}`）。
- 查询响应含：有符号→无符号盒边映射、分解区间（含 `exact` 标记）、逐步
  `steps[]`、`stats`（区间数、预算、块读取量、候选/结果/假阳性/膨胀率）、
  `uncertainties[]`（预算耗尽等不确定结论单列）。
- 错误分类为稳定机器可读类别：`not_initialized` / `already_initialized` /
  `schema_validation_error` / `coordinate_out_of_domain` /
  `query_validation_error` / `budget_error`。

## 数据布局

```
<data_root>/
  catalog.sqlite(+wal)     # dataset / chunks / audit
  chunks/<chunk_id>.arrow  # Arrow IPC(zstd): row_id, code, 各维列，按 code 排序
```
