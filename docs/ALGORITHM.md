# 算法假设、模块关系与判定语义

## 1. 核心安全不变量

1. **默认拒绝（default deny）**：没有任何 ALLOW 规则确定命中时，判定为 `DENY`。
2. **显式拒绝优先（explicit deny wins）**：任一 DENY 规则**确定命中**，结果即为 `DENY`，
   无论它与 ALLOW 规则的书写顺序如何。
3. **未知不等于允许（unknown ≠ allow）**：条件涉及无法获知/不可比的值时给出
   `UNKNOWN`；调用方不得把 UNKNOWN 当放行。
4. 策略文本**禁止显式写出 UNKNOWN 效果**（只允许 `ALLOW`/`DENY`），
   UNKNOWN 只能由内核算出来。

判定顺序（伪代码）：

```
对请求 q 逐条求值规则 -> MATCH / NOT_MATCH / POSSIBLE_MATCH
若存在 DENY 且 MATCH           -> DENY   （decided_by=该规则, default_deny=false）
否则若存在 ALLOW MATCH
     且不存在 POSSIBLE 的 DENY -> ALLOW
否则若存在任何 POSSIBLE 规则   -> UNKNOWN
否则                          -> DENY   （default_deny=true）
```

注意“可能命中的 DENY 压过确定 ALLOW”：若 ALLOW 已确定成立、但某条 DENY
因未知属性可能成立，结果仍是 UNKNOWN，而不是 ALLOW。

## 2. 三态条件求值

每条条件返回 `TRUE / FALSE / UNKNOWN`，规则内合取：
`FALSE` 立即使规则 NOT_MATCH；无 FALSE 且有 UNKNOWN → POSSIBLE_MATCH。

关键边界约定（防止“正常输入看着对、边界输入悄悄算错”）：

| 情形 | 结果 |
|---|---|
| 属性缺失 + `Eq/In/Gt/CidrMatch/GlobMatch/NotEq/NotIn/...` | UNKNOWN（**否定算子也一样保守**） |
| 属性缺失 + `Exists` | FALSE；`NotExists` | TRUE |
| `Eq` 两侧类型不同（`"1"` vs `1`、`true` vs `1`） | UNKNOWN，不当作“不等” |
| `Eq` 中 `int` vs `float`（`1` vs `1.0`） | 类型不同 → UNKNOWN |
| 数值算子遇到非数值运行时值（`"high"`） | UNKNOWN，不当作 FALSE |
| CIDR 值畸形（`"garbage"`）或策略 CIDR 非法（解析期拒绝） | 运行时 UNKNOWN / 提交期 SCHEMA_INVALID |
| 前缀 `"logs"` | 解析时规范化为 `"logs/"`，杜绝误匹配 `logs-secret/` |
| `principals: ["*"]` | 必须同时 `anonymous: true`，否则提交期拒绝（避免两处不一致） |
| 规则 `actions: []` | 提交期拒绝：空集表示“不匹配任何操作”，必须显式表达而非留白 |

## 3. 受限请求空间与完备分割

差分只在**显式声明的受限空间**内穷举，空间由四个有限维构成。

### 3.1 资源维：前缀区域（region）

边界集合 `B = 分析范围前缀 ∪ 与范围有交叠的规则前缀`。
任意键 `k` 的区域是 `B` 中作为其前缀的**最长**边界（区域代表键 anchor）。
每个区域用一个真实键作见证：

- anchor 为桶根 `""` → 见证为空键 `""`；
- 否则见证 = `anchor + TAIL`，TAIL 取自字母表中一个非 `/` 字符。

**完备性论证**：区域内任意键对“是否以各规则前缀为前缀”的布尔向量相同——
不同布尔向量只会出现在越过某个边界前缀的位置，而那正是另一个区域。
见证键不以 `/` 结尾、而所有非根边界都以 `/` 结尾，因此它不可能落入
比 anchor 更深的边界区域。`"logs/"` 与 `"logs-secret/"` 因而天然是
两个独立区域（见 `tests/test_universe.py` 的边界性质测试）。

范围外区域（如分析范围是 `logs/` 时的桶根）不产生范围内见证。

### 3.2 其它三维

- **主体**：配置的合成主体 + 匿名 `null` + 一个所有规则都未列举的合成体 `@other`
  （用来暴露“只给部分主体开口”的收窄/加宽）。
- **操作**：两版规则操作 ∪ 分析范围操作，外加 `@other`（未列举操作必须保持拒绝）。
  桶统计只计入分析范围操作；`@other` 等范围外点仍被穷举并计入总转移计数。
- **条件属性**：由两版规则条件的右值自动构造有限取值域，保证 TRUE/FALSE/UNKNOWN
  三分支都能被实际取到：
  - 数值阈值：`t-1, t, t+1`；
  - 集合：每个成员 + 一个集合外字符串；
  - glob：一个匹配例 + 固定不匹配串；
  - CIDR：网内地址、广播地址、网外地址（若存在）、畸形值 `not-a-valid-ip`；
  - 布尔：`true/false`；每个属性都额外加入“缺失”哨兵。

空间大小 = 区域数 × 主体数 × 操作数 × Π(各属性域大小)，
**超过上限（默认 500 000）直接返回 `SCOPE_SPACE_TOO_LARGE`，绝不静默抽样**。
穷举顺序确定（排序后的笛卡尔积），同一空间两次迭代逐点相同。

## 4. 差分桶与总结论

对空间每一点分别用两版策略判定，落入四类桶（仅统计分析范围内）：

| 桶 | old → new | 含义 |
|---|---|---|
| `widened` | DENY → ALLOW | **新增允许：可访问集合扩大** |
| `removed_allow` | ALLOW → DENY | 新增拒绝：集合收窄 |
| `new_unknown` | 确定 → UNKNOWN | 新出现的不确定点 |
| `removed_unknown` | UNKNOWN → 确定 | 被消除的不确定点 |

总结论：`WIDENED / WIDENED_WITH_UNKNOWN / SHRUNK / SHRUNK_WITH_UNKNOWN /
UNSURE_UNKNOWN / EQUIVALENT`。只要分析范围内存在新增 UNKNOWN，
结论就强制带 `_WITH_UNKNOWN`，不允许把含未知的变化报告成无条件加宽。

每个桶输出**具体请求见证**（主体/操作/资源/属性 + 两版判定 + 决定性规则 +
是否默认拒绝 + 所属区域 anchor）。测试会把这些见证重新喂回两版内核复演，
防止见证由被测代码“自说自话”。

## 5. 证据（历史真实观测）对账

证据束 `evidence-bundle/v1`：签名信封 + 顺序记录，每条记录是
`(主体, 操作, 资源, 属性, 真实观测 ALLOW|DENY, 观测规则)`，并参与重算哈希链：

```
base = sha256(canonical(束元数据))
h_i  = sha256(h_{i-1} || canon(记录 i 正文))
```

- 签名缺失/未知公钥/验签失败 → `CRYPTO_MISSING_SIGNATURE / CRYPTO_UNREGISTERED_KEY /
  CRYPTO_BAD_SIGNATURE`（HTTP 422）。
- 签名通过但链重算不一致（改内容/换顺序/改 base_hash）→ `EVIDENCE_TAMPERED`，
  并指出首个失配的 `seq`。
- 证据 `observed` 只允许确定值 ALLOW/DENY；UNKNOWN 记录在解析期拒绝。
- 证据声明范围与分析范围（前缀×操作）无交集 → `SCOPE_NO_OVERLAP`。
- 逐条对账：`CONSISTENT / CONTRADICTION / INCONCLUSIVE / OUT_OF_SCOPE`。
  内核对证据请求给 UNKNOWN 时记 INCONCLUSIVE，**既不算一致也不算矛盾**；
  矛盾单列并在审计写 FAILURE_EVIDENCE。

## 6. 模块关系

```
config.toml ──> config.py
fixtures/keys ─> crypto_verify.py (只持公钥的信任注册表)
                        │
POST 信封 ─> api.py ─> service.py（用例编排）
                          ├── parser.py        严格解析/规范化（只产不可变领域对象）
                          ├── crypto_verify.py 验签
                          ├── evidence.py      解析 + 哈希链重算
                          ├── universe.py      区域分割 + 有限域 + 穷举
                          │      └── 迭代点 ──> kernel.py（三态判定，无 I/O）
                          ├── diffengine.py    分桶/见证/证据对账
                          ├── store.py         SQLite 不可变快照 + 只追加审计
                          └── audit.py         请求身份贯穿 + 失败/不确定单列
```

- `kernel.py` 不 import store/api/evidence，可独立测试；
- 测试中的“参考答案”是 `tests/test_diffengine.py` 里**另写的一遍**
  `reference_decide`（不复用 kernel/diffengine），逐点核对整张转移表；
- `local_signing.py` 仅供本地夹具/演示构造签名，**安全内核不依赖它**；
- 不设用户/角色/账号后台；调用方仅由 `submitted_by` + 已注册公钥和
  `X-Actor` 审计头标识（`/v1/trust` 明确声明 `user_backend: none`）。

## 7. 可解释性

- 每个请求有 `request_id`（客户端可用 `X-Request-Id` 固定），全部日志、
  差分结果、证据对账都带它。
- 内核返回每规则的轨迹步：前缀/操作/主体三个硬门布尔值、每条件三态结果、
  规则结局；API 侧只截断展示长度，底层判定信息完整。
- `/v1/audit` 把 `failures` 与 `inconclusive` 从普通事件中**单列**。
- 失败是结构化类别（`FailureKind`），不是自由文本；HTTP 状态映射：
  SCHEMA_*→400，CRYPTO_*/EVIDENCE_*→422，VERSION_CONFLICT→409，NOT_FOUND→404。
