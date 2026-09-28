# 验收执行记录（真实输出）

- 日期：2026-09-28
- 环境：Linux 6.8.0-90-generic，Python 3.12.3
- 复现方式（从干净目录）：
  ```bash
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m pytest -q          # 41 passed
  .venv/bin/python scripts/validate.py   # overall PASS, exit 0
  bash scripts/serve_demo.sh             # 端到端 HTTP 演示，exit 0
  ```

## 1. 独立测试

```
$ .venv/bin/python -m pytest -q
.........................................                                 [100%]
41 passed in 2.89s
```

覆盖（断言具体结果与失败类别，非“接口可调用”）：

- `test_encoding.py`（17）：手写 2×2 位交错表逐码核对；与**独立实现的**
  位交错/反交错在 2D/3D/不等宽小空间穷举一致；zig-zag 字面量
  （-1→1,-2→3,-8→15 等）；越界坐标拒绝；**128 位高位不截断**往返；
  薄盒/点盒的精确区间具体码值；预算从 1→64 全覆盖零漏且随预算收紧；
  exact 区间不含任何盒外点；原始盒→无符号边映射。
- `test_kernel.py`（8）：4×4 网格全部 100 个盒对照独立朴素全扫描逐 id 相等；
  4D 稀疏在预算 1/4/16/256 全部零漏零假；候选随预算单调下降且高预算更紧；
  重写后**稳定 row id**逐行映射回原行；角点数据重写后剪枝 4→1；
  删除 chunk 文件 → `status=degraded` + `chunk_unreadable`；空盒与 limit。
- `test_api.py`（7+）：完整信封/步骤/统计；5 类失败的具体 category 与 HTTP
  状态；schema 冲突 409、PUT 缺失 404、替换清空 chunk；预算耗尽进
  `uncertainties` 而非 error；重写端点保 id；缺文件降级；入站 `X-Request-ID`
  与审计回查。

参考答案来源：朴素逐维比较的 `brute_force`、独立写的 `ref_interleave/
ref_deinterleave`、手算交错表与字面 zig-zag 期望——**均不复用被测内核/解码**。

## 2. 保留验证过程（边界/负数/薄盒/高维稀疏 vs 全扫描）

```
$ .venv/bin/python scripts/validate.py
validation overall: PASS   (exit 0)
报告： results/validation.json
```

4 个场景全部：编码解码往返 0 失败、**零漏行零假行**、重写后 row id 稳定。
下表为关键候选膨胀/读取量（truth=全扫描真实命中，cand=码区间候选，
bloat=(cand-truth)/truth，chunks=选中/总，bytes=物理读取字节）：

| 场景 | 盒 | 预算 | truth | cand | bloat | chunks |
|---|---|---:|---:|---:|---:|---:|
| 2D 16位 | iqr 宽盒 | 1 | 5158 | 20000 | 2.88 | 5/5 |
| 2D 16位 | iqr 宽盒 | 256 | 5158 | 5158 | 0.00 | 4/5 |
| 2D 16位 | x 薄 sliver | 1 | 156 | 20000 | 127.2 | 5/5 |
| 2D 16位 | x 薄 sliver | 256 | 156 | 156 | 0.00 | 2/5 |
| 2D 16位 | 数据点 needle | 16 | 157 | 157 | 0.00 | 1/5 |
| 4D 20位稀疏 | 簇邻域 | 1 | 1500 | 12000 | 7.00 | 6/6 |
| 4D 20位稀疏 | 簇邻域 | 4096 | 1500 | 1740 | 0.16 | 3/6 |
| 4D 20位稀疏 | 数据点 needle | 128 | 18 | 18 | 0.00 | 1/6 |
| 8D **128位** | 簇邻域 | 1 | 48 | 6000 | 124.0 | 4/4 |
| 8D **128位** | 簇邻域 | 4096 | 48 | 406 | 7.46 | 1/4 |
| 8D **128位** | needle | 1 | 1 | 6000 | 5999.0 | 4/4 |
| 8D **128位** | needle | 4096 | 1 | 1 | 0.00 | 1/4 |
| 混合不等宽 | iqr 宽盒 | 1 | 754 | 4000 | 4.31 | 4/4 |
| 混合不等宽 | iqr 宽盒 | 4096 | 754 | 754 | 0.00 | 4/4 |

观察（与设计一致）：预算耗尽只扩大候选、不漏结果；预算提高使候选单调收紧、
膨胀降到 0、chunk 剪枝与读取字节随之下降；高维 needle 对预算最敏感
（8D 从 5999× 膨胀到精确 1 行、只读 1/4 块）。

## 3. HTTP 端到端演示

```
$ bash scripts/serve_demo.sh   (exit 0, 完整 JSON 见 results/demo_output.txt)
```

建 schema→灌 40000 行（5 chunk）→重写成 4096/块（`rows_rewritten=40000`,
`row_id_preserved=true`）→薄盒查询（携带 `X-Request-ID: demo-thin-001`，
预算不足时 `uncertainties[].category=budget_exhausted`，仍零漏行）→
按 request id 回查审计记录，均符合预期。冷启动 `uvicorn` 与 `/health`
手工冒烟通过，结构化日志每行带 `request_id`。

## 4. 过程中发现并修复的问题（如实记录）

1. **Arrow 18 深层 OR 表达式原生栈溢出 (SIGSEGV)**：8D/128 位、几十个区间时，
   `Table.filter(深层 | 嵌套表达式)` 触发 libarrow `Canonicalize/ModifyExpression`
   递归爆栈（gdb 栈确认，非确定性地在连续多查询后崩）。修复：区间下推改为
   对每个区间用扁平 compute kernel 生成布尔掩码、`pc.or_` 归并
   （`kernel.interval_mask_128`），语义等价，压力复现不再崩溃。
2. 分块读取初版用 `memory_map`，返回的 Table 持有已关闭映射；改为 `OSFile`
   打开并完整物化。
3. 重写早期按 row id 排序导致新 chunk 码区间仍重叠、无法剪枝；改为按 Morton
   码全局重排（tie 用稳定 row id），row id 仍逐行保留。
4. 零真命中时膨胀比曾记成候选绝对值（误导为“无限膨胀”）；改为 `null`，
   并在验证报告中显示 `n/a`。
