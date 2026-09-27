# 边界与正确性语义

本文档是**规范**：测试与实现均以此为准。所有行为均有测试覆盖（用例名在
末尾索引）。

## 1. 数据类型与坐标压缩

- 点坐标 `x, y ∈ i64`；点增量/点累计值 `delta, value ∈ i64`。
- 矩形边界 `x_lo, x_hi, y_lo, y_hi ∈ i128`。使用更宽类型是为了让
  `i64::MIN`、`i64::MAX` 处的点可以被“全平面”矩形正常寻址，也允许
  `i128` 量级的越界边界，不产生溢出或半开区间特例。
- 各轴压缩表顺序**固定**：坐标按**升序排序并去重**。同一集合无论输入顺序，
  压缩表完全一致。秩（rank）从 1 开始（Fenwick 下标），稠密网格下标从 0
  开始。
- 注册空间是**笛卡尔积**：`register {xs, ys}` 注册全部 `(x,y)` 组合。

## 2. 矩形边界语义（闭区间）

矩形为 `[x_lo, x_hi] × [y_lo, y_hi]`，**四边包含**：点 `(x,y)` 计入当且仅当

```
x_lo ≤ x ≤ x_hi  且  y_lo ≤ y ≤ y_hi
```

- `x_lo > x_hi` 或 `y_lo > y_hi` 为**空矩形**；空矩形查询结果恒为 `0`，
  响应 `"empty": true`。空矩形是合法输入，不是错误。
- 边界“恰好落在已注册坐标上”的点计入（inclusive），用例
  `boundary-touch-inclusive` 验证。
- 落在坐标间隙、坐标外侧（即使边界本身有效）的区域贡献 0，用例
  `valid-bounds-no-points-x-gap`、`just below min excluded` 验证。

### 2.1 前缀截断（cutoff）定义

对轴上的排序坐标集 `C`，矩形在该轴上映射到两个秩截断：

- `rank_le(hi) = |{c ∈ C : c ≤ hi}|`
- `rank_lt(lo) = |{c ∈ C : c < lo}|`

注意第二个是**严格小于**，而**不是** `rank_prefix(lo − 1)`：当
`lo = i128::MIN` 时 `lo − 1` 会溢出。整数坐标下两者计数相同，但严格小于
形式在任意 `i128` 输入下都安全。查询响应的 `explain.cutoffs` 原样给出
`x_le_hi / x_lt_lo / y_le_hi / y_lt_lo`。

### 2.2 包含–排斥与判定依据

```
sum = P(x≤x_hi, y≤y_hi) − P(x<x_lo, y≤y_hi)
        − P(x≤x_hi, y<y_lo) + P(x<x_lo, y<y_lo)
```

`explain.terms` 给出四项前缀和 `hh / lh / hl / ll`，`explain.sum` 给出
结果。空矩形直接判 0，不依赖四项相减的次序。用例
`fenwick_explain_exposes_steps_and_terms` 对四项具体数值做断言（例如
窗口 `[1,2]×[1,2]`：`7−3−3+3=4`）。

## 3. 更新规则

- 增量语义：每次 `POST /batches` 把 `delta` **累加**到点累计值上；
  `delta` 允许为负数（用例含 `mixed-sign-window`、`point-exact-negative`）。
- **跨批重复**同一坐标是正常累加（批 1 与批 3 都更新 `(-5,-3)`，用例
  `cancelled-corner-zero` 验证先 `+3` 后 `-3` 回到 0）。
- **批内重复**同一坐标被拒绝：`duplicate_in_batch`。调用方需在批内自行
  折叠；这保证批意图唯一、溢出检查精确。
- **未注册坐标拒绝**：任何 `x` 或 `y` 不在当前压缩表中的更新都以
  `unregistered_coordinate` 拒绝，**不会**隐式插入或错位到邻近坐标。
  （用例 `unregistered-x`、`unregistered-y`。）
- 校验顺序固定：非空 → 批内重复 → 已注册 → 溢出。全部通过后才写日志、
  发布版本；任一失败整批丢弃，**不产生新版本、不留下部分效果**
  （用例 `rejected-batch-publishes-nothing` 验证批中即便含有合法点也不会
  部分生效）。

## 4. 溢出

- 点累计值必须落在 `i64`。候选新值在 i128 下计算
  `new = old + delta`，越界则整批 `overflow` 拒绝；用例
  `overflow-max`（`10 + i64::MAX`）、`overflow-min`（`-5 + i64::MIN`）。
- **矩形和不要求落在 i64**：Fenwick 内部累加器与查询结果均为 `i128`，
  大量 `i64::MAX` 点同处一个矩形也能精确求和。
- 服务构造快照发生在日志提交之前；因此“溢出”不可能已落盘，天然回滚。

## 5. 版本、原子发布与历史可查

- 版本号从 1 起稠密递增：`registered`（首次注册）→ 若干 `batch` /
  `rebuild`。
- 每个版本拥有**独立不可变**的压缩表、稠密网格和 Fenwick。发布是版本向量
  上的一次指针加入；读者要么看到旧完整版本、要么看到新完整版本。
  用例 `concurrent_readers_never_see_half_batch`：6 读者 × 300 批并发，
  每次读到的全平面和必须等于某个已发布版本（`8·k`），且版本号与和自洽，
  杜绝半批。
- 查询可指定任意历史版本。坐标重建后旧版本仍以其**当时的表**回答
  （用例 `old-version-still-readable-after-rebuild`、
  `v2 dropped point still present in v2`）。

## 6. 坐标重建（rebuild）

- 重建用新的 `{xs, ys}` 替换当前表；新轴不允许为空（`empty_axis`）。
- 旧表中累计值非零、且其坐标仍存在于新表笛卡尔积中的点，值**逐点原样
  携入**（不涉及加法，因此携值不会溢出）；被移出表的点从 head 消失，但
  在历史版本中仍可查询。
- 新加入的坐标初始值为 0。
- 用例：`rebuild-dropped-point`、`rebuild-carried-negative`、
  `rebuild-whole-plane`。

## 7. 持久化与未知状态

- 日志逐行带 FNV-1a-64 校验和（对排序键后的规范 JSON 计算，与磁盘键序
  无关）；提交含文件 `fsync`，`CURRENT` 经临时文件 `rename` + 目录
  `fsync` 切换。
- 打开时严格重放：seq/version 必须连续、校验和必须匹配、`CURRENT` 必须
  等于日志尾版本。任何不符一律 `persistence_error` 拒绝启动，不猜测、
  不静默修复（用例：载荷位翻转、尾部截断、CURRENT 超前/缺失/非数字）。
- 空目录视为全新未初始化库，正常打开（此时更新/查询返回
  `not_initialized`）。

## 用例索引

| 语义 | Rust 用例 | 夹具/冒烟步骤 |
|---|---|---|
| 固定压缩顺序 | `compression_order_is_fixed_and_deduped` | — |
| 闭区间/空矩形/极端坐标 | `hand_values_extreme_coords_and_empty_rects`、`kernel-random-vs-fullscan` | `handcalc.json` queries、smoke 07–13 |
| 未注册拒绝 | `handcalc_fixture_end_to_end` | smoke 05 |
| 批内重复拒绝 | 同上 | smoke 04 |
| 负权/重复坐标累加 | 同上 | smoke 06–09 |
| 溢出检测 | 同上 | smoke 14 |
| 原子发布 | `concurrent_readers_never_see_half_batch`、`rejected_batch_publishes_nothing` | — |
| 重建与旧版本 | `handcalc_fixture_end_to_end` | `rebuild*` 查询、smoke 16–17 |
| 重放与篡改 | `replay_reconstructs_every_version`、`tampered_log_is_detected`、`appended_after_reopen_continues_version_chain` | verify.sh 重启阶段 |
| 错误归类/请求关联 | `http_error_categories_and_correlation` | smoke 02、15、18 |
