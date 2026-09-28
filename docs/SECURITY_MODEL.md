# 指标口径与安全模型

## 1. 指标定义（metric_version 1.0.0）

设行集被泛化后的 QI 键划分为等价类 C（真实计数，无均匀假设）。

### k-匿名
∀C: |C| ≥ k。单类不达标即整体 `k_anonymized=false`。

### l-多样性（distinct，保守口径）
∀C: |{非 NULL 敏感值}| ≥ l。

- 敏感值为 NULL 的成员**不计入**不同敏感值种数，只在
  `sensitive_null_members` 单独计数——NULL 不能用来“凑”多样性。
- 多敏感列：当前只对声明的第一列计算 l 多样性（已知限制）。

### 检察官重识别风险
单类 `1/|C|`；报告给最差值与按行数加权平均值。

### 可辨识代价 DM（Discernibility Metric）
`DM = Σ_C |C|²`。它随真实类大小二次增长：两个 `[3,3]` 划分 DM=18，
而 `[4,2]` DM=20，即使类数相同。优化器以 DM 为主目标，因此**必须读真实
计数**，测试 `test_real_class_counts_are_used_not_uniform_assumption`
专门钉住该性质。

### 归一化深度损失 LM
`LM = (1/n) Σ_rows (1/m) Σ_j (level_j / depth_j)`，m 为 QI 列数，
按真实类大小（即行数）加权。DM 平局时用 LM 选更浅泛化，再平局按列顺序
取字典序最小向量，保证结果可复现。

### 风险类别
- HIGH：该类不满足 k 或 l；
- MEDIUM：满足但 |C| < `risk_medium_factor·k`（默认 2），贴近阈值；
- LOW：满足且 |C| ≥ factor·k。

## 2. NULL 语义

| 位置 | 处理 |
|---|---|
| 输入 | `null`/空串/纯空白 → 规范 NULL；**行永不删除** |
| QI | 任意层级上 NULL 保持 NULL，只与其他 NULL 同类；`qi_key_contains_null` 标记 |
| 敏感列 | NULL 成员保留在类内，可贡献类大小（k），但不计入 distinct（l） |
| 证据 | `null_counts`（每列）、`null_qi_rows`、`sensitive_null_members_total` |

这是明确的建模选择：宁可让含 NULL 的类显式暴露风险，也不静默丢弃样本。

## 3. 层级包含关系

- map：第 1 级键集合必须**恰好**覆盖实际原始值域（多键=`UNKNOWN_KEY`，
  缺键=`INCOMPLETE`）；后续级键必须恰好覆盖上一级出现的标签。按构造即
  保持包含（dict 是良定义函数）。
- prefix：`keep` 正整数且逐级严格递减；任何值短于 keep 即 `INCOMPLETE`。
- range：bins 严格递增、覆盖全部数值；每一级在实际值域上的分箱必须是
  上一级的合并——同一上级箱被拆到两个本级箱即 `NOT_CONTAINING`
  （用经验映射冲突检测，见 `test_range_bins_must_be_coarser_at_each_level`）。

## 4. 威胁模型与防护

**防护**：

- 存储：整包 Fernet 加密；每运行 HKDF(主密钥, run_id, 随机salt) 独立子密钥；
  运行文件相互独立；令牌只存 SHA-256，比较用恒定时间函数；run_id 做路径
  穿越校验。
- 输出：白名单视图；原始 QI/敏感值不进任何响应与审计 details；类只给
  HMAC 指纹与计数；指纹跨运行不可关联（派生密钥不同）。
- 审计：只追加（UPDATE/DELETE 触发器），失败与不可达也落审计。
- 密钥：环境变量装载；未配置时仅开发态一次性临时密钥，`/health`
  与 `/version` 明确返回 `key_ephemeral=true`。

**不在防护范围**：

- k/l 不防御背景知识、同质攻击之外的偏斜、相似性/近似匹配、链接外部数据、
  差分攻击或推断攻击；不提供 t-近邻、差分隐私等保证；
- 持有主密钥的进程管理员可以解密（这是设计前提，不是端到端加密）；
- 审计触发器阻止 DML 篡改，但无法阻止有文件权限者 DROP/替换文件，
  生产应由文件权限/WORM/备份覆盖；
- 临时密钥模式下重启后旧运行不可解密。

## 5. 已知限制与后续方向

1. 穷举格点复杂度 `∏(depth+1)`，受 `lattice_combo_cap`（默认 10000）限制；
   大表需要带剪枝/A* 的搜索（会改变“可穷举证明最优”的测试故事，未采用）。
2. 多敏感列只对第一列算 l；entropy/recursive (c,l) 多样性未实现。
3. 没有抑制（suppression）策略：不可达时报告证据而非删行；全泛化标签
   `*` 可表达“整体合并”。
4. SQLite 单机存储，未做多进程写协调/连接池/水平扩展。
5. 时间型侧信道、内存换出（swap/core dump）由运行环境负责。

## 6. 复现实验结论（tiny 合成表，6 行）

k=2,l=2：穷举 12 个向量，最优 `zip=L1(zip4), age=L2(any)`，两个类各 3 行，
DM=18、LM=2/3；可行向量仅 4 个：(1,2)、(2,2)、(3,1)、(3,2)。
k=2,l=3：全合并后非 NULL 敏感值仅 2 种 → 明确 UNREACHABLE。
k=7,l=1：n=6，全泛化最大类仅 6 → UNREACHABLE。
上述数字由手工表（`tests/expected.py`）与独立 stdlib 暴力实现
（`tests/oracle.py`）共同钉住。
